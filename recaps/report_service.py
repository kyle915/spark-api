"""Aggregation engine for the per-Request "Client Campaign Report".

A campaign report rolls up everything that happened under one
:class:`events.models.Request` (a brand's activation program) into a
single client-facing deliverable: headline KPIs, the event roster, a
photo gallery, the BA roster, and a handful of consumer quotes.

It is READ-ONLY — no model, no migration, pure aggregation over the
existing recap tables. It must handle BOTH recap shapes:

* **Legacy recaps** (:class:`recaps.models.Recap`) — KPIs live in typed
  columns (``total_engagements``, ``products_sold``,
  ``total_cans_sold`` …) plus a one-row ``consumer_engagements`` child
  (``total_consumer``, ``first_time_consumers`` …) and
  ``product_samples`` quantities.
* **Custom-template recaps** (:class:`recaps.models.CustomRecap`) — most
  KPIs live as free-text ``CustomFieldValue`` rows keyed by the custom
  field NAME. We mirror the exact label/parse rules the rest of the app
  uses (``recaps.types._sold_units_from_fields`` /
  ``_consumers_sampled_from_fields`` and ``recaps.pdf._custom_engagement_totals``)
  so the report's numbers match the recap cards, dashboard, and the
  existing multi-recap PDF byte-for-byte.

The single entry point is :func:`build_campaign_report`, which returns a
plain :class:`CampaignReportData` dataclass. The GraphQL resolver, the
public REST views, and the PDF builder all consume that one dataclass so
the three surfaces never drift.

Everything here is synchronous Django ORM — callers that live in the
async request loop (the GraphQL resolver, the share-token surfaces) wrap
the call in ``sync_to_async``.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from events.models import Request
from recaps.types import (
    _samples_given_from_fields,
    _consumers_sampled_from_fields,
    _is_image_url,
    _sold_units_from_fields,
)
from utils.gcs import extract_blob_name_from_url, public_url

# Cap the photo gallery so a 200-event campaign with thousands of recap
# files doesn't render a 400-page PDF / a multi-MB JSON payload. The web
# report shows a gallery preview; deeper drill-down lives on the recap
# detail pages.
MAX_PHOTOS = 60

# Cap the highlight quotes — a "best of" reel, not every comment.
MAX_HIGHLIGHTS = 15

# A consumer-feedback blob can be a paragraph or a single sentence. Trim
# absurdly long entries so one rambling note doesn't dominate the section.
MAX_QUOTE_CHARS = 400


# ---------------------------------------------------------------------------
# Plain data containers. Deliberately framework-free (no Strawberry /
# Django types) so the same objects feed GraphQL, JSON, and the PDF.
# ---------------------------------------------------------------------------
@dataclass
class CampaignReportKpis:
    events: int = 0
    recaps: int = 0
    consumers_reached: int = 0
    samples_distributed: int = 0
    products_sold: int = 0
    cans_sold: int = 0
    packs_sold: int = 0
    total_engagements: int = 0
    first_time_consumers: int = 0
    brand_aware_consumers: int = 0
    willing_to_purchase: int = 0


# Plausibility thresholds for a SINGLE event's parsed KPIs. Shared by the
# submit-time guard (recaps.mutations, fires the moment a recap is filed)
# and the weekly audit (audit_recap_data_health) so both judge "suspect
# numbers" identically. Single source of truth — do NOT duplicate the rules.
DEFAULT_MAX_CONSUMERS = 1000
DEFAULT_MAX_UNITS = 5000


def implausibility_reasons(
    kpis: CampaignReportKpis,
    *,
    max_consumers: int = DEFAULT_MAX_CONSUMERS,
    max_units: int = DEFAULT_MAX_UNITS,
) -> list[str]:
    """Human-readable reasons the parsed KPIs can't be right for one event
    (empty list = looks fine). Catches the failure modes from the SHB
    digit-mash incident: impossible conversion, or counts far above what a
    single sampling event could produce."""
    reasons: list[str] = []
    if kpis.consumers_reached and kpis.willing_to_purchase > kpis.consumers_reached:
        reasons.append(
            f"conversion >100% (willing {kpis.willing_to_purchase} > "
            f"consumers {kpis.consumers_reached})"
        )
    if kpis.consumers_reached > max_consumers:
        reasons.append(f"consumers {kpis.consumers_reached} > {max_consumers}")
    if kpis.cans_sold > max_units:
        reasons.append(f"cans {kpis.cans_sold} > {max_units}")
    if kpis.packs_sold > max_units:
        reasons.append(f"packs {kpis.packs_sold} > {max_units}")
    return reasons


@dataclass
class CampaignReportPhoto:
    url: str
    caption: str | None = None


@dataclass
class CampaignReportEventRow:
    id: str
    name: str | None
    date: str | None
    location_name: str | None
    city: str | None
    state: str | None
    coordinates: str | None
    recap_count: int = 0


@dataclass
class CampaignReportBa:
    name: str
    is_external: bool
    event_count: int


@dataclass
class CampaignReportQuote:
    text: str
    source: str | None = None


@dataclass
class CampaignReportData:
    request_id: int
    brand_name: str
    title: str
    date_range: str | None
    generated_at: str
    kpis: CampaignReportKpis = field(default_factory=CampaignReportKpis)
    events: list[CampaignReportEventRow] = field(default_factory=list)
    photos: list[CampaignReportPhoto] = field(default_factory=list)
    ambassadors: list[CampaignReportBa] = field(default_factory=list)
    highlights: list[CampaignReportQuote] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Custom-field KPI extraction. Mirrors recaps.pdf._custom_engagement_totals
# label matching + recaps.types._sold_units_from_fields / _consumers_sampled
# so the report agrees with the recap cards, dashboard, and multi-recap PDF.
# ---------------------------------------------------------------------------
def _custom_field_pairs(custom_recap) -> list[tuple[str | None, str | None]]:
    """(field_name, value) pairs for a CustomRecap's CustomFieldValue rows."""
    pairs: list[tuple[str | None, str | None]] = []
    for cfv in _all(custom_recap, "custom_field_value"):
        cf = getattr(cfv, "custom_field", None)
        pairs.append((getattr(cf, "name", None), getattr(cfv, "value", None)))
    return pairs


def _leading_int(value) -> int | None:
    """First non-negative integer in a free-text value ('70 cans' -> 70)."""
    if value is None:
        return None
    m = re.search(r"\d+", str(value))
    return int(m.group()) if m else None


def _custom_engagement_totals(pairs) -> dict[str, int]:
    """Map a custom recap's free-text engagement fields onto the four
    consumer metrics by label. Same matching as
    ``recaps.pdf._custom_engagement_totals``.
    """
    out = {
        "total_consumer": 0,
        "first_time_consumers": 0,
        "brand_aware_consumers": 0,
        "willing_to_purchase_consumers": 0,
    }
    from recaps.types import _SAMPLED_TOTAL_RE

    demographic_sampled = 0
    for name, value in pairs:
        label = (name or "").lower()
        val = _leading_int(value)
        if val is None:
            continue
        if "consumers sampled" in label:
            out["total_consumer"] += val
        elif _SAMPLED_TOTAL_RE.search(label):
            # Girl Beer style: "Men/Women who sampled (Total)" demographics
            demographic_sampled += val
        elif "first time" in label:
            out["first_time_consumers"] += val
        elif "knew about" in label:
            out["brand_aware_consumers"] += val
        elif "willing to purchase" in label and "not" not in label:
            out["willing_to_purchase_consumers"] += val
    if out["total_consumer"] == 0 and demographic_sampled:
        out["total_consumer"] = demographic_sampled
    return out


def _all(obj, attr: str) -> list:
    """Materialize a related manager / iterable to a list, tolerating both
    a prefetched reverse manager (``.all()``) and a plain list.
    """
    relation = getattr(obj, attr, None)
    if relation is None:
        return []
    if hasattr(relation, "all"):
        return list(relation.all())
    return list(relation)


# ---------------------------------------------------------------------------
# Per-recap KPI rollups.
# ---------------------------------------------------------------------------
def _accumulate_legacy(recap, kpis: CampaignReportKpis) -> None:
    """Fold one legacy :class:`recaps.models.Recap` into the KPI totals."""
    kpis.total_engagements += int(getattr(recap, "total_engagements", 0) or 0)
    kpis.products_sold += int(getattr(recap, "products_sold", 0) or 0)
    kpis.cans_sold += int(getattr(recap, "total_cans_sold", 0) or 0)
    kpis.packs_sold += int(getattr(recap, "total_packs_sold", 0) or 0)

    engagements = _all(recap, "consumer_engagements")
    if engagements:
        eng = engagements[0]
        kpis.consumers_reached += int(getattr(eng, "total_consumer", 0) or 0)
        kpis.first_time_consumers += int(
            getattr(eng, "first_time_consumers", 0) or 0
        )
        kpis.brand_aware_consumers += int(
            getattr(eng, "brand_aware_consumers", 0) or 0
        )
        kpis.willing_to_purchase += int(
            getattr(eng, "willing_to_purchase_consumers", 0) or 0
        )

    for sample in _all(recap, "product_samples"):
        kpis.samples_distributed += int(getattr(sample, "quantity", 0) or 0)


def _accumulate_custom(custom_recap, kpis: CampaignReportKpis) -> None:
    """Fold one :class:`recaps.models.CustomRecap` into the KPI totals.

    ``total_engagements`` is a typed column on CustomRecap. The four
    consumer numbers + sold units come from the free-text CustomFieldValue
    rows (label-matched, parsed). ``samplesDistributed`` prefers the
    structured ``custom_recap_product_sample`` quantities, falling back to
    the "consumers sampled" custom field (which the recap cards treat as
    the sampling headline) when no structured samples exist.
    """
    kpis.total_engagements += int(getattr(custom_recap, "total_engagements", 0) or 0)

    pairs = _custom_field_pairs(custom_recap)

    eng = _custom_engagement_totals(pairs)
    consumers_sampled = _consumers_sampled_from_fields(pairs)
    # consumersReached: the "consumers sampled" headline if present, else
    # the label-matched total_consumer (same value in practice; the
    # explicit helper guards templates that name the field differently).
    kpis.consumers_reached += int(
        consumers_sampled
        if consumers_sampled is not None
        else eng["total_consumer"]
    )
    kpis.first_time_consumers += int(eng["first_time_consumers"])
    kpis.brand_aware_consumers += int(eng["brand_aware_consumers"])
    kpis.willing_to_purchase += int(eng["willing_to_purchase_consumers"])

    # Sold units from cans/packs custom fields. We add the whole sum to
    # productsSold (the generic "units moved" headline) and, when the
    # template separates cans vs packs, split them onto cansSold/packsSold.
    sold = _sold_units_from_fields(pairs)
    if sold is not None:
        kpis.products_sold += int(sold)
    for name, value in pairs:
        label = (name or "").lower()
        parsed = _leading_int(value)
        if parsed is None:
            continue
        if re.search(r"\bcans?\b", label):
            kpis.cans_sold += int(parsed)
        elif re.search(r"\bpacks?\b", label):
            kpis.packs_sold += int(parsed)

    structured_samples = sum(
        int(getattr(s, "quantity", 0) or 0)
        for s in _all(custom_recap, "custom_recap_product_sample")
    )
    samples_given = _samples_given_from_fields(pairs)
    if structured_samples:
        kpis.samples_distributed += structured_samples
    elif samples_given is not None:
        # Free-text "Total Samples Given Out" headline (Girl Beer).
        kpis.samples_distributed += int(samples_given)
    elif consumers_sampled is not None:
        kpis.samples_distributed += int(consumers_sampled)


# ---------------------------------------------------------------------------
# Photo gallery — public URLs for recap file blobs (legacy + custom).
# Mirrors RecapFile.file_url / CustomRecapFile.url_str + hero-image isImage.
# ---------------------------------------------------------------------------
def _file_public_url(file_obj, attr: str) -> str | None:
    """Resolve a FileField blob to a public URL (no signing, no GCS I/O)."""
    field_file = getattr(file_obj, attr, None)
    if not field_file:
        return None
    try:
        blob = field_file.name
    except Exception:
        blob = str(field_file)
    return public_url(extract_blob_name_from_url(blob))


def _collect_photos(legacy_recaps, custom_recaps) -> list[CampaignReportPhoto]:
    """Image-typed recap files across every recap, capped at MAX_PHOTOS.

    Only browser-renderable image URLs are kept (jpg/jpeg/png/webp/gif) —
    the same ``isImage`` rule the recap hero-image picker uses — so the
    gallery never points an ``<img>`` at a PDF or a HEIC the browser can't
    decode.
    """
    photos: list[CampaignReportPhoto] = []

    def _emit(files, attr: str, caption: str | None):
        for f in files:
            if len(photos) >= MAX_PHOTOS:
                return
            url = _file_public_url(f, attr)
            if url and _is_image_url(url):
                photos.append(CampaignReportPhoto(url=url, caption=caption))

    for recap in legacy_recaps:
        if len(photos) >= MAX_PHOTOS:
            break
        ev = getattr(recap, "event", None)
        _emit(_all(recap, "recap_files"), "file", getattr(ev, "name", None))
    for custom_recap in custom_recaps:
        if len(photos) >= MAX_PHOTOS:
            break
        ev = getattr(custom_recap, "event", None)
        _emit(
            _all(custom_recap, "custom_recap_files"),
            "url",
            getattr(ev, "name", None),
        )
    return photos


# ---------------------------------------------------------------------------
# BA roster — real Spark ambassadors + external typed names, with the
# number of distinct events each worked under this request.
# ---------------------------------------------------------------------------
def _ba_display_name(ambassador) -> str:
    user = getattr(ambassador, "user", None)
    if user is not None:
        full = " ".join(
            filter(
                None,
                [
                    (getattr(user, "first_name", "") or "").strip(),
                    (getattr(user, "last_name", "") or "").strip(),
                ],
            )
        ).strip()
        if full:
            return full
        email = getattr(user, "email", None)
        if email:
            return email
    return "(ambassador)"


def _collect_ambassadors(
    legacy_recaps, custom_recaps
) -> list[CampaignReportBa]:
    """Roster of BAs credited on this campaign's recaps.

    A real Spark Ambassador FK takes precedence; when it's null we fall
    back to the free-text ``external_ba_name`` (flagged ``isExternal``) —
    same display rule the recap PDF uses. ``eventCount`` is the number of
    distinct events the BA filed a recap for under this request.
    """
    # key -> {"name", "is_external", "event_ids": set}
    roster: dict[str, dict] = {}

    def _credit(ambassador, external_name, event_id):
        if ambassador is not None:
            key = f"ba:{ambassador.id}"
            name = _ba_display_name(ambassador)
            is_external = False
        else:
            name = (external_name or "").strip()
            if not name:
                return
            key = f"ext:{name.lower()}"
            is_external = True
        entry = roster.setdefault(
            key, {"name": name, "is_external": is_external, "event_ids": set()}
        )
        if event_id is not None:
            entry["event_ids"].add(event_id)

    for recap in legacy_recaps:
        _credit(
            getattr(recap, "ambassador", None),
            getattr(recap, "external_ba_name", None),
            getattr(recap, "event_id", None),
        )
    for custom_recap in custom_recaps:
        _credit(
            getattr(custom_recap, "ambassador", None),
            getattr(custom_recap, "external_ba_name", None),
            getattr(custom_recap, "event_id", None),
        )

    rows = [
        CampaignReportBa(
            name=entry["name"],
            is_external=entry["is_external"],
            event_count=len(entry["event_ids"]),
        )
        for entry in roster.values()
    ]
    # Most-active first, then alphabetical for stable ties.
    rows.sort(key=lambda r: (-r.event_count, r.name.lower()))
    return rows


# ---------------------------------------------------------------------------
# Highlights — consumer quotes / positive stories pulled from recaps.
# ---------------------------------------------------------------------------
def _clean_quote(text: str | None) -> str | None:
    if not text:
        return None
    cleaned = " ".join(str(text).split()).strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_QUOTE_CHARS:
        cleaned = cleaned[: MAX_QUOTE_CHARS - 1].rstrip() + "…"
    return cleaned


def _collect_highlights(legacy_recaps, custom_recaps) -> list[CampaignReportQuote]:
    """Consumer quotes + positive stories, capped at MAX_HIGHLIGHTS.

    Legacy recaps store these on ``consumer_feedback`` (``quotes`` /
    ``positive_stories``). Custom recaps keep them as free-text custom
    fields whose NAME mentions "quote", "story", "highlight", "feedback",
    or "testimonial". Dedupes on the cleaned text and attributes each to
    its event name.
    """
    out: list[CampaignReportQuote] = []
    seen: set[str] = set()

    def _add(text: str | None, source: str | None):
        if len(out) >= MAX_HIGHLIGHTS:
            return
        cleaned = _clean_quote(text)
        if not cleaned:
            return
        key = cleaned.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(CampaignReportQuote(text=cleaned, source=source or None))

    for recap in legacy_recaps:
        if len(out) >= MAX_HIGHLIGHTS:
            break
        ev = getattr(recap, "event", None)
        source = getattr(ev, "name", None)
        for feedback in _all(recap, "consumer_feedback"):
            _add(getattr(feedback, "quotes", None), source)
            _add(getattr(feedback, "positive_stories", None), source)

    quote_name_re = re.compile(
        r"quote|story|stories|highlight|feedback|testimonial", re.IGNORECASE
    )
    for custom_recap in custom_recaps:
        if len(out) >= MAX_HIGHLIGHTS:
            break
        ev = getattr(custom_recap, "event", None)
        source = getattr(ev, "name", None)
        for name, value in _custom_field_pairs(custom_recap):
            if name and quote_name_re.search(name):
                _add(value, source)

    return out


# ---------------------------------------------------------------------------
# Event roster + date range.
# ---------------------------------------------------------------------------
def _event_row(event, recap_count: int) -> CampaignReportEventRow:
    location = getattr(event, "location", None)
    state = getattr(event, "state", None)
    coordinates = getattr(event, "coordinates", None)
    coord_str = None
    if coordinates and len(coordinates) >= 2:
        coord_str = f"{coordinates[0]},{coordinates[1]}"
    date_val = getattr(event, "date", None)
    return CampaignReportEventRow(
        id=str(event.id),
        name=getattr(event, "name", None),
        date=date_val.isoformat() if date_val else None,
        location_name=getattr(location, "name", None) if location else None,
        city=getattr(location, "name", None) if location else None,
        state=getattr(state, "name", None) if state else None,
        coordinates=coord_str,
        recap_count=recap_count,
    )


def _format_date_range(events) -> str | None:
    """"Apr 1 – Apr 30, 2026"-style label over the events' min/max date."""
    dates = [getattr(e, "date", None) for e in events]
    dates = [d for d in dates if d]
    if not dates:
        return None
    lo, hi = min(dates), max(dates)
    if lo.date() == hi.date():
        return lo.strftime("%b %-d, %Y")
    if lo.year == hi.year:
        return f"{lo.strftime('%b %-d')} – {hi.strftime('%b %-d, %Y')}"
    return f"{lo.strftime('%b %-d, %Y')} – {hi.strftime('%b %-d, %Y')}"


# ---------------------------------------------------------------------------
# Queryset + entry point.
# ---------------------------------------------------------------------------
def report_request_queryset():
    """Base Request queryset with every relation the report walks.

    Excludes soft-deleted requests (matches the standard requests
    resolver) and prefetches the events + both recap trees + their child
    rows so the whole report renders without an N+1 over a multi-event
    campaign. Mirrors the prefetch posture of the ``requests`` resolver,
    extended with the recap children the aggregation reads.
    """
    return (
        Request.objects.filter(deleted_at__isnull=True)
        .select_related("tenant")
        .prefetch_related(
            "event_set",
            "event_set__location",
            "event_set__state",
            # Legacy recaps + their KPI children.
            "event_set__recaps",
            "event_set__recaps__consumer_engagements",
            "event_set__recaps__product_samples",
            "event_set__recaps__consumer_feedback",
            "event_set__recaps__recap_files",
            "event_set__recaps__ambassador__user",
            # Custom-template recaps + their KPI children.
            "event_set__custom_recap",
            "event_set__custom_recap__custom_field_value__custom_field",
            "event_set__custom_recap__custom_recap_product_sample",
            "event_set__custom_recap__custom_recap_files",
            "event_set__custom_recap__ambassador__user",
        )
    )


def get_report_request(request_id, tenant_id: int | None = None):
    """Fetch one Request for the report, optionally tenant-scoped.

    ``request_id`` may be the request's UUID (the handle the web app routes
    by — what the admin report view passes) OR its numeric pk (what the
    signed share token carries). Returns ``None`` when the request doesn't
    exist or is out of the caller's tenant scope (``tenant_id`` set +
    mismatch) — the resolver and public surfaces translate that to a
    not-found response.
    """
    qs = report_request_queryset()
    if tenant_id is not None:
        qs = qs.filter(tenant_id=tenant_id)
    raw = str(request_id).strip()
    try:
        uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError):
        # Not a uuid → treat it as a numeric pk.
        try:
            return qs.filter(id=int(raw)).first()
        except (ValueError, TypeError):
            return None
    return qs.filter(uuid=raw).first()


def build_campaign_report(
    request_obj: Request, *, generated_at: str
) -> CampaignReportData:
    """Aggregate one Request into a :class:`CampaignReportData`.

    ``request_obj`` should come from :func:`report_request_queryset` (or an
    equivalently prefetched queryset) so this runs without per-row queries.
    ``generated_at`` is passed in (ISO-8601) so the caller controls the
    clock — keeps the function pure and deterministic in tests.
    """
    events = sorted(
        _all(request_obj, "event_set"),
        key=lambda e: (getattr(e, "date", None) is None, getattr(e, "date", None) or 0),
    )

    kpis = CampaignReportKpis()
    kpis.events = len(events)

    legacy_recaps: list = []
    custom_recaps: list = []
    event_rows: list[CampaignReportEventRow] = []

    for event in events:
        ev_legacy = _all(event, "recaps")
        ev_custom = _all(event, "custom_recap")
        legacy_recaps.extend(ev_legacy)
        custom_recaps.extend(ev_custom)
        event_rows.append(_event_row(event, len(ev_legacy) + len(ev_custom)))

    kpis.recaps = len(legacy_recaps) + len(custom_recaps)
    for recap in legacy_recaps:
        _accumulate_legacy(recap, kpis)
    for custom_recap in custom_recaps:
        _accumulate_custom(custom_recap, kpis)

    tenant = getattr(request_obj, "tenant", None)
    brand_name = getattr(tenant, "name", None) or ""

    return CampaignReportData(
        request_id=request_obj.id,
        brand_name=brand_name,
        title=getattr(request_obj, "name", None) or "",
        date_range=_format_date_range(events),
        generated_at=generated_at,
        kpis=kpis,
        events=event_rows,
        photos=_collect_photos(legacy_recaps, custom_recaps),
        ambassadors=_collect_ambassadors(legacy_recaps, custom_recaps),
        highlights=_collect_highlights(legacy_recaps, custom_recaps),
    )


def report_to_dict(data: CampaignReportData) -> dict:
    """Serialize a report to camelCase JSON (public REST shape).

    Intentionally omits ``shareToken`` — the public payload is reached
    *via* the token, so re-emitting it is redundant. Keys match the
    GraphQL field names so the web client can share rendering code.
    """
    return {
        "requestId": str(data.request_id),
        "brandName": data.brand_name,
        "title": data.title,
        "dateRange": data.date_range,
        "generatedAt": data.generated_at,
        "kpis": {
            "events": data.kpis.events,
            "recaps": data.kpis.recaps,
            "consumersReached": data.kpis.consumers_reached,
            "samplesDistributed": data.kpis.samples_distributed,
            "productsSold": data.kpis.products_sold,
            "cansSold": data.kpis.cans_sold,
            "packsSold": data.kpis.packs_sold,
            "totalEngagements": data.kpis.total_engagements,
            "firstTimeConsumers": data.kpis.first_time_consumers,
            "brandAwareConsumers": data.kpis.brand_aware_consumers,
            "willingToPurchase": data.kpis.willing_to_purchase,
        },
        "events": [
            {
                "id": row.id,
                "name": row.name,
                "date": row.date,
                "locationName": row.location_name,
                "city": row.city,
                "state": row.state,
                "coordinates": row.coordinates,
                "recapCount": row.recap_count,
            }
            for row in data.events
        ],
        "photos": [
            {"url": p.url, "caption": p.caption} for p in data.photos
        ],
        "ambassadors": [
            {
                "name": ba.name,
                "isExternal": ba.is_external,
                "eventCount": ba.event_count,
            }
            for ba in data.ambassadors
        ],
        "highlights": [
            {"text": q.text, "source": q.source} for q in data.highlights
        ],
    }
