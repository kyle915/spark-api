"""Tenant-wide, FIELD-LEVEL recap export — one row per recap, every captured
field as its own column, plus a link to every photo and receipt.

Why this exists (and why it isn't `campaign-to-date`): that endpoint returns
per-Request *aggregates* only — 11 computed KPIs plus events/photos/ambassadors
arrays. It never carries individual ``CustomFieldValue`` rows, so it cannot be
widened into "every answer a BA actually typed". `dump_custom_recap` does read
field-level data but only for ONE recap by UUID. This module is the tenant-wide
generalisation: the raw captured data, not a rollup of it.

Shape (see :func:`build_recap_field_export`):

  * **Identity columns** — recap uuid/id, request id, event, event date,
    store/location, city, state, retailer, BA, submitted-at, approval status,
    total engagements, data-quality flags.
  * **One column per template field**, in template → section.order →
    field.order sequence, headed by the field's real name. Columns come from
    the tenant's ``CustomRecapTemplate``(s), so they're stable across rows even
    when a given recap left the field blank. Fields sharing a normalized name
    across templates collapse into ONE column (that's how the values are keyed).
  * **One column per ``FileRecapCategory``** holding that category's URLs, plus
    a total-file-count column, plus a flat per-file list (``files``) for a
    clickable detail tab.

Deliberate omissions, because a zero reads as a measured result: nothing is
synthesised for metrics the tenant's form doesn't collect. Girl Beer's form asks
for no cans-sold / first-time / brand-aware / willing-to-purchase figures, so
those columns are simply ABSENT here rather than present-and-zero (which is what
the aggregate KPI workbook necessarily shows).

Never conflate "samples distributed" with "consumers reached" — they are
different measures collected by different fields, and reconcile to very
different totals. This export keeps each field under its own name so the
distinction survives.

Date scoping is by EVENT date (the period the activity belongs to), never
created/imported date.

Reuses the battle-tested extractors from :mod:`recaps.recap_sheet_export` (the
Google-Sheets export of the same underlying data) so the two never drift:
``_ordered_fields`` / ``_values_by_field_name`` / ``_recap_meta`` /
``_retailer_inferrer`` / ``_normalize``. Rendering to XLSX/CSV lives in the
Django-free :mod:`utils.workbook` so a CI runner can render without a database.

READ-ONLY: no writes, no email.
"""
from __future__ import annotations

import re
from datetime import date, datetime

from recaps.heic_conversion import display_blob_name
from recaps.models import CustomRecap, FileRecapCategory
from recaps.pdf import _event_date, _event_state
from recaps.recap_sheet_export import (
    _event_name,
    _fmt_mdy,
    _name_of,
    _normalize,
    _ordered_fields,
    _recap_meta,
    _retailer_inferrer,
    _store_location,
    _values_by_field_name,
)
from utils.gcs import extract_blob_name_from_url, public_url

# Files have a history of landing in the WRONG category — Girl Beer receipts
# filed themselves under a photo category until the positional-sentinel
# resolver was fixed and `backfill-girlbeer-receipts` ran. So we never trust
# the category blindly: we group by it, then REPORT how many files look
# receipt-ish by blob path/filename while sitting in a non-receipt category
# (and vice versa). Callers surface this before anyone builds a deck on it.
_RECEIPT_HINT_RE = re.compile(r"receipt|invoice|expense|purchase", re.IGNORECASE)

# Internal test/demo events seeded for staff walkthroughs (Girl Beer has seven,
# named "H-E-B (Internal Demo) — <city>"). Counted so nobody ships them to the
# client inside an otherwise-real workbook. Never filtered automatically —
# "all the recap data" means all of it; this only makes them visible.
_INTERNAL_DEMO_RE = re.compile(r"internal\s*demo|\(demo\)|test\s*event", re.IGNORECASE)


def _consumers_sampled_value(recap):
    """The recap's "consumers sampled" answer as an int, or None.

    Uses the same ``_CONSUMERS_SAMPLED_RE`` vocabulary the KPI matchers use, so
    this agrees with what the dashboard counts rather than inventing a second
    definition. Free-text/prose answers that merely mention the phrase parse to
    None via ``_leading_int`` and are skipped.
    """
    from recaps.report_service import _leading_int
    from recaps.types import _CONSUMERS_SAMPLED_RE

    for cfv in recap.custom_field_value.all():
        cf = getattr(cfv, "custom_field", None)
        name = (getattr(cf, "name", "") or "") if cf else ""
        if not name or not _CONSUMERS_SAMPLED_RE.search(name):
            continue
        parsed = _leading_int(cfv.value)
        if parsed is not None:
            return int(parsed)
    return None

# Column groups, used by the renderer for banding/labels.
GROUP_IDENTITY = "Identity"
GROUP_STRUCTURED = "Samples & Sales"
GROUP_FILES = "Files"

UNCATEGORIZED_LABEL = "Uncategorized files"


def _structured_samples(recap) -> tuple[str, str]:
    """The recap's ``CustomRecapProductSample`` rows → (readable, total).

    These are per-product sampled QUANTITIES and they are the primary source
    for the dashboard's ``samplesDistributed`` KPI
    (``report_service`` sums ``custom_recap_product_sample.quantity`` before
    falling back to any "samples given" text field). They live in their own
    table, NOT in ``CustomFieldValue``, so a template-field-only export would
    silently omit them — which for Girl Beer would drop the entire basis of
    that KPI. Rendered as "Hazy IPA x12; Lager x8".
    """
    parts, total = [], 0
    for s in recap.custom_recap_product_sample.all():
        qty = int(getattr(s, "quantity", 0) or 0)
        total += qty
        parts.append(f"{_name_of(getattr(s, 'product', None))} x{qty}")
    return "; ".join(parts), (str(total) if parts else "")


def _sale_performance(recap) -> str:
    """The recap's ``CustomRecapSalePerformance`` rows → readable string.

    Per-product price by type-of-good ("Hazy IPA - 6-Pack @ $12.99"), also a
    separate table from ``CustomFieldValue``. Included for the same reason as
    the sample rows: it's captured recap data, so "all the recap data" has to
    carry it.
    """
    parts = []
    for row in recap.custom_recap_sale_performance.all():
        product = _name_of(getattr(row, "product", None))
        good = _name_of(getattr(row, "type_of_good", None))
        price = getattr(row, "price", None)
        label = " - ".join(p for p in (product, good) if p)
        if price is not None:
            label = f"{label} @ ${price:.2f}" if label else f"${price:.2f}"
        if label:
            parts.append(label)
    return "; ".join(parts)


def _as_date(value):
    """Coerce a date/datetime (or None) to a plain ``date``."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _parse_ymd(raw):
    """"YYYY-MM-DD" → date. Empty/None → None. Raises ValueError otherwise."""
    if raw in (None, ""):
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    return datetime.strptime(str(raw).strip(), "%Y-%m-%d").date()


def _fmt_dt(value) -> str:
    if not value:
        return ""
    try:
        return value.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(value)


def _city(recap) -> str:
    """The event's city.

    There is no City model — the reporting layer treats ``event.location.name``
    as the city (see ``recaps.report_service._event_row``), so we match it for
    consistency. This FK is unreliable for some tenants (nulls, and stores
    mis-geocoded to the wrong metro); the Event NAME often carries the real
    city, which is why both columns ship side by side. Judge, don't assume.
    """
    event = getattr(recap, "event", None)
    location = getattr(event, "location", None) if event else None
    return str(getattr(location, "name", "") or "") if location else ""


def _looks_like_receipt(blob: str, filename: str) -> bool:
    return bool(_RECEIPT_HINT_RE.search(f"{blob} {filename}"))


def _file_rows_for(recap, *, resolve_heic: bool = True) -> list[dict]:
    """Every file attached to one recap, with a public URL.

    ``CustomRecapFile.url`` stores the blob NAME, not a full URL, so it goes
    through ``extract_blob_name_from_url`` → ``public_url`` — the same pair the
    check-in page uses to build display URLs. Never hand-concatenate a bucket
    path; the helper owns that.

    HEIC blobs resolve to their converted ``.jpg`` sibling when one exists
    (``display_blob_name``), so the link opens in a browser instead of
    downloading an unviewable file. That costs one cheap HEAD per HEIC blob —
    pass ``resolve_heic=False`` to skip it on very large exports.

    Returns ``(rows, unlinkable_count)``. A file we cannot turn into a URL is
    COUNTED rather than silently skipped: ``public_url`` returns None when
    ``settings.GS_BUCKET_NAME`` is unset, which would drop every photo from the
    deliverable without a word. A client noticing missing photos before we do
    is much worse than an export that reports the gap.
    """
    out: list[dict] = []
    unlinkable = 0
    for f in recap.custom_recap_files.all():
        raw = getattr(f, "url", None)
        stored = (str(raw) if raw else "").strip()
        if not stored:
            unlinkable += 1
            continue
        blob = extract_blob_name_from_url(stored)
        if resolve_heic:
            try:
                blob = display_blob_name(blob) or blob
            except Exception:
                # A GCS hiccup must never sink the export — fall back to the
                # original blob, which still resolves for non-HEIC files.
                pass
        url = public_url(blob)
        if not url:
            unlinkable += 1
            continue
        category = getattr(f, "file_recap_category", None)
        filename = (getattr(f, "name", None) or "").strip() or (blob or "").rsplit("/", 1)[-1]
        out.append(
            {
                "id": f.id,
                "category": _name_of(category) if category is not None else "",
                "name": filename,
                "url": url,
                "blob": blob,
                "approved": bool(getattr(f, "approved", False)),
                "looks_like_receipt": _looks_like_receipt(blob or "", filename),
            }
        )
    return out, unlinkable


def _identity_columns() -> list[dict]:
    return [
        {"key": "recap_uuid", "header": "Recap ID", "group": GROUP_IDENTITY, "kind": "text"},
        {"key": "recap_id", "header": "Recap #", "group": GROUP_IDENTITY, "kind": "text"},
        {"key": "request_id", "header": "Request ID", "group": GROUP_IDENTITY, "kind": "text"},
        {"key": "event_name", "header": "Event", "group": GROUP_IDENTITY, "kind": "text"},
        {"key": "event_date", "header": "Event Date", "group": GROUP_IDENTITY, "kind": "text"},
        {"key": "store", "header": "Store/Location", "group": GROUP_IDENTITY, "kind": "text"},
        {"key": "city", "header": "City", "group": GROUP_IDENTITY, "kind": "text"},
        {"key": "state", "header": "State", "group": GROUP_IDENTITY, "kind": "text"},
        {"key": "retailer", "header": "Retailer", "group": GROUP_IDENTITY, "kind": "text"},
        {"key": "ba", "header": "Brand Ambassador", "group": GROUP_IDENTITY, "kind": "text"},
        {"key": "submitted_at", "header": "Submitted At", "group": GROUP_IDENTITY, "kind": "text"},
        {"key": "status", "header": "Approval Status", "group": GROUP_IDENTITY, "kind": "text"},
        {
            "key": "total_engagements",
            "header": "Total Engagements",
            "group": GROUP_IDENTITY,
            "kind": "text",
        },
        {
            "key": "data_quality_flags",
            "header": "Data Quality Flags",
            "group": GROUP_IDENTITY,
            "kind": "text",
        },
    ]


def _field_columns(tenant) -> tuple[list[dict], dict]:
    """Template field columns in section order, deduped by normalized name.

    ``_ordered_fields`` walks every template the tenant owns (template id →
    section.order → field.order), excluding image-type fields — those hold a
    blob path, not an answer, and their content is reported properly in the
    file columns instead. Two templates can define the same field name; since
    values are keyed by normalized name, such fields MUST collapse to one
    column or the second would silently duplicate the first.
    """
    columns: list[dict] = []
    seen: dict[str, dict] = {}
    duplicates: list[str] = []
    templates: dict = {}
    for f in _ordered_fields(tenant):
        name = (getattr(f, "name", "") or "").strip()
        if not name:
            continue
        key = _normalize(name)
        tpl = getattr(f, "custom_recap_template", None)
        if tpl is not None:
            templates[tpl.id] = getattr(tpl, "name", "") or f"template {tpl.id}"
        if key in seen:
            duplicates.append(name)
            continue
        section = getattr(f, "recap_section", None)
        col = {
            "key": f"field::{key}",
            "header": name,
            "group": (getattr(section, "name", "") or "Fields").strip() or "Fields",
            "kind": "text",
            "field_id": f.id,
            "field_type": _name_of(getattr(f, "custom_field_type", None)),
        }
        seen[key] = col
        columns.append(col)
    return columns, {
        "templates": templates,
        "duplicate_field_names": sorted(set(duplicates)),
    }


def _structured_columns() -> list[dict]:
    """Columns for the two structured child tables (samples + sale rows)."""
    return [
        {
            "key": "product_samples",
            "header": "Products Sampled (qty)",
            "group": GROUP_STRUCTURED,
            "kind": "text",
        },
        {
            "key": "product_samples_total",
            "header": "Samples Distributed (total)",
            "group": GROUP_STRUCTURED,
            "kind": "text",
        },
        {
            "key": "sale_performance",
            "header": "Sale Performance (product / price)",
            "group": GROUP_STRUCTURED,
            "kind": "text",
        },
    ]


def _file_columns(tenant, *, include_uncategorized: bool) -> list[dict]:
    """One column per tenant ``FileRecapCategory``, in the tenant's own
    creation order (which matches the seeded default order), then an
    uncategorized bucket if any file needs one, then the total count."""
    cats = FileRecapCategory.objects.filter(tenant=tenant).order_by("id")
    columns = [
        {
            "key": f"filecat::{_normalize(c.name)}",
            "header": c.name,
            "group": GROUP_FILES,
            "kind": "links",
            "category_id": c.id,
        }
        for c in cats
    ]
    if include_uncategorized:
        columns.append(
            {
                "key": "filecat::__none__",
                "header": UNCATEGORIZED_LABEL,
                "group": GROUP_FILES,
                "kind": "links",
                "category_id": None,
            }
        )
    columns.append(
        {"key": "file_total", "header": "Total Files", "group": GROUP_FILES, "kind": "number"}
    )
    return columns


def _tenant_recaps(tenant):
    """Every custom recap for the tenant, with the joins the row builder needs.

    Widens ``recap_sheet_export._tenant_recaps`` with the event/location/state/
    request joins and the file prefetch, so building N rows stays a fixed
    number of queries rather than N×5.
    """
    return (
        CustomRecap.objects.filter(tenant=tenant)
        .select_related(
            "event",
            "event__location",
            "event__state",
            "event__retailer",
            "event__request",
            "ambassador",
            "ambassador__user",
            "state",
            "retailer",
        )
        .prefetch_related(
            "custom_field_value__custom_field",
            "custom_recap_files__file_recap_category",
            # The two structured child tables — sampled quantities and sale
            # rows. Prefetched so N recaps stay a fixed query count.
            "custom_recap_product_sample__product",
            "custom_recap_sale_performance__product",
            "custom_recap_sale_performance__type_of_good",
        )
        .order_by("id")
    )


def build_recap_field_export(
    tenant,
    *,
    start=None,
    end=None,
    resolve_heic: bool = True,
) -> dict:
    """Build the whole field-level export payload for one tenant.

    Grain is ONE ROW PER RECAP — not per event. Events that never received a
    recap are counted and reported (``diagnostics.events_without_recap``) but
    contribute no row, because a blank row in a "captured data" export reads as
    a measured zero. The row count is labelled in the rendered workbook.

    ``start`` / ``end`` are inclusive ``YYYY-MM-DD`` bounds on the EVENT date.
    A recap whose event has no resolvable date is EXCLUDED when a window is
    given (it cannot be placed in the period) and included when it isn't; both
    counts are reported.

    Returns a JSON-safe dict: ``columns`` (ordered, with group + kind),
    ``rows`` (cells positionally aligned to ``columns``; a "links" cell is a
    list of URL strings), ``files`` (flat per-file rows for a clickable detail
    tab), ``meta`` and ``diagnostics``. Never raises on a single bad file.
    """
    start_d = _parse_ymd(start)
    end_d = _parse_ymd(end)

    recaps = list(_tenant_recaps(tenant))
    infer_retailer = _retailer_inferrer(tenant)

    field_cols, field_info = _field_columns(tenant)

    # ── Pass 1: collect per-recap files so we know whether an uncategorized
    #    bucket is needed before the column list is frozen.
    kept: list[tuple] = []
    undated = 0
    out_of_window = 0
    needs_uncategorized = False
    unlinkable_files = 0
    for recap in recaps:
        ev_date = _as_date(_event_date(recap))
        if start_d or end_d:
            if ev_date is None:
                undated += 1
                continue
            if start_d and ev_date < start_d:
                out_of_window += 1
                continue
            if end_d and ev_date > end_d:
                out_of_window += 1
                continue
        elif ev_date is None:
            undated += 1
        files, unlinkable = _file_rows_for(recap, resolve_heic=resolve_heic)
        unlinkable_files += unlinkable
        if any(not f["category"] for f in files):
            needs_uncategorized = True
        kept.append((recap, ev_date, files))

    file_cols = _file_columns(tenant, include_uncategorized=needs_uncategorized)
    structured_cols = _structured_columns()
    columns = _identity_columns() + field_cols + structured_cols + file_cols

    # ── Pass 2: rows.
    rows: list[list] = []
    all_files: list[dict] = []
    category_counts: dict[str, int] = {}
    receipts_outside_receipt_categories: list[dict] = []
    non_receipts_in_receipt_categories: list[dict] = []
    non_empty = {c["key"]: 0 for c in columns}
    consumers_over_engagements: list[dict] = []
    drafts = 0
    internal_demo_rows = 0
    structured_sample_total = 0

    for recap, ev_date, files in kept:
        meta = _recap_meta(recap, infer_retailer)
        # Keyed by NORMALIZED field name, with the multiselect-JSON → comma-list
        # formatting already applied, so this export and the Sheets export
        # render an answer identically.
        by_name = _values_by_field_name(recap)

        by_category: dict[str, list[str]] = {}
        for f in files:
            cat_key = f"filecat::{_normalize(f['category'])}" if f["category"] else "filecat::__none__"
            by_category.setdefault(cat_key, []).append(f["url"])
            label = f["category"] or UNCATEGORIZED_LABEL
            category_counts[label] = category_counts.get(label, 0) + 1
            cat_is_receipt = bool(_RECEIPT_HINT_RE.search(label))
            if f["looks_like_receipt"] and not cat_is_receipt:
                receipts_outside_receipt_categories.append(
                    {"recap": str(recap.uuid), "category": label, "name": f["name"]}
                )
            elif cat_is_receipt and not f["looks_like_receipt"]:
                non_receipts_in_receipt_categories.append(
                    {"recap": str(recap.uuid), "category": label, "name": f["name"]}
                )
            all_files.append(
                {
                    "recap_uuid": str(recap.uuid),
                    "recap_id": recap.id,
                    "event_name": _event_name(recap),
                    "event_date": _fmt_mdy(ev_date),
                    "ba": meta.get("ba", ""),
                    "category": label,
                    "name": f["name"],
                    "url": f["url"],
                    "approved": f["approved"],
                }
            )

        samples_label, samples_total = _structured_samples(recap)
        if samples_total:
            structured_sample_total += int(samples_total)

        # Data-quality check the submit-time guard does NOT make: a recap
        # cannot sample more consumers than it engaged. Seven Girl Beer recaps
        # do exactly that, overstating consumers by 107 — surfaced here so the
        # number is questioned before it reaches a deck, not after.
        engagements = getattr(recap, "total_engagements", None)
        consumers = _consumers_sampled_value(recap)
        if engagements is not None and consumers is not None and consumers > engagements:
            consumers_over_engagements.append(
                {
                    "recap": str(recap.uuid),
                    "event": _event_name(recap),
                    "ba": meta.get("ba", ""),
                    "engagements": engagements,
                    "consumers_sampled": consumers,
                    "excess": consumers - engagements,
                }
            )
        if not getattr(recap, "approved", False):
            drafts += 1
        if _INTERNAL_DEMO_RE.search(_event_name(recap) or ""):
            internal_demo_rows += 1

        identity = {
            "recap_uuid": str(recap.uuid),
            "recap_id": str(recap.id),
            "request_id": str(getattr(getattr(recap, "event", None), "request_id", "") or ""),
            "event_name": _event_name(recap),
            "event_date": _fmt_mdy(ev_date),
            "store": _store_location(recap),
            "city": _city(recap),
            "state": _name_of(_event_state(recap)),
            "retailer": meta.get("retailer", ""),
            "ba": meta.get("ba", ""),
            "submitted_at": _fmt_dt(getattr(recap, "submitted_at", None)),
            "status": meta.get("status", ""),
            "total_engagements": (
                str(recap.total_engagements) if recap.total_engagements is not None else ""
            ),
            "data_quality_flags": (getattr(recap, "data_quality_flags", "") or "").strip(),
        }

        row: list = []
        for col in columns:
            key = col["key"]
            if key in identity:
                value = identity[key]
            elif key.startswith("field::"):
                value = by_name.get(key[len("field::"):], "")
            elif key == "product_samples":
                value = samples_label
            elif key == "product_samples_total":
                value = samples_total
            elif key == "sale_performance":
                value = _sale_performance(recap)
            elif key == "file_total":
                value = len(files)
            elif key.startswith("filecat::"):
                value = by_category.get(key, [])
            else:  # pragma: no cover - defensive
                value = ""
            row.append(value)
            if value not in ("", [], 0, None):
                non_empty[key] += 1
        rows.append(row)

    # Events with no recap at all — the coverage gap, reported not rowed.
    recap_event_ids = {getattr(r, "event_id", None) for r in recaps}
    events_without_recap = _events_without_recap(tenant, recap_event_ids, start_d, end_d)

    empty_columns = [
        c["header"] for c in columns if non_empty[c["key"]] == 0 and c["key"].startswith("field::")
    ]

    return {
        "tenant": {
            "id": getattr(tenant, "id", None),
            "name": getattr(tenant, "name", None),
            "slug": getattr(tenant, "request_url_name", None),
        },
        "grain": "recap",
        "window": {
            "start": start_d.isoformat() if start_d else None,
            "end": end_d.isoformat() if end_d else None,
            "scoped_by": "event date",
        },
        "columns": columns,
        "rows": rows,
        "files": all_files,
        "meta": {
            "row_count": len(rows),
            "column_count": len(columns),
            "file_count": len(all_files),
            "identity_column_count": len(_identity_columns()),
            "field_column_count": len(field_cols),
            "file_column_count": len(file_cols),
            "templates": field_info["templates"],
        },
        "diagnostics": {
            "recaps_total_for_tenant": len(recaps),
            "recaps_excluded_out_of_window": out_of_window,
            "recaps_without_event_date": undated,
            "events_without_recap": events_without_recap,
            "files_by_category": category_counts,
            # Non-zero means attached files exist that we could NOT turn into a
            # URL (missing blob, or GS_BUCKET_NAME unset). Never ship the export
            # without checking this — it's the difference between "no photos"
            # and "photos we failed to link".
            "files_without_a_link": unlinkable_files,
            # Structured sampled quantities — the basis of the dashboard's
            # samplesDistributed KPI. Compare the two before quoting either.
            "structured_samples_total": structured_sample_total,
            # Physically impossible rows: more consumers sampled than total
            # engagements. The submit-time guard checks conversion >100% but
            # not this, so it reaches reports unflagged.
            "consumers_exceeding_engagements": {
                "count": len(consumers_over_engagements),
                "total_excess": sum(r["excess"] for r in consumers_over_engagements),
                "rows": consumers_over_engagements[:20],
            },
            # Rows a client deliverable probably should not include. Reported,
            # never auto-dropped.
            "unapproved_or_draft_rows": drafts,
            "internal_demo_rows": internal_demo_rows,
            "duplicate_field_names_collapsed": field_info["duplicate_field_names"],
            "field_columns_entirely_empty": empty_columns,
            "receipt_looking_files_outside_receipt_categories": {
                "count": len(receipts_outside_receipt_categories),
                "samples": receipts_outside_receipt_categories[:15],
            },
            "non_receipt_looking_files_in_receipt_categories": {
                "count": len(non_receipts_in_receipt_categories),
                "samples": non_receipts_in_receipt_categories[:15],
            },
        },
    }


def _events_without_recap(tenant, recap_event_ids, start_d, end_d) -> int:
    """How many of the tenant's events in-window carry no custom recap.

    Reported so the coverage gap is visible without padding the grid with
    blank rows that would read as measured zeros.
    """
    from events.models import Event

    count = 0
    for event in (
        Event.objects.filter(tenant=tenant).only("id", "date", "tenant_id").iterator()
    ):
        if event.id in recap_event_ids:
            continue
        ev_date = _as_date(getattr(event, "date", None))
        if start_d or end_d:
            if ev_date is None:
                continue
            if start_d and ev_date < start_d:
                continue
            if end_d and ev_date > end_d:
                continue
        count += 1
    return count
