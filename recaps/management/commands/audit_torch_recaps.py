"""Torch THC recap audit — READ-ONLY.

Walks every Torch recap (CustomRecap + legacy Recap) and reports, per recap:
store, activation bucket, status, consumers sampled, units purchased, SKUs,
photo buckets, the conversion each recap contributes, and data-quality flags
(missing data, duplicates, impossible values, wrong activation type, stuck in
Needs review, no store #). Then pools conversion by month / store / BA, next
to what Insights' ``tenant_conversion_kpis`` reports for the same windows.

Never writes. Run via ``/internal/cron/audit-torch-recaps`` or the
"Audit Torch recaps" GitHub Action; the report + CSV land in the run log.

    python manage.py audit_torch_recaps
    python manage.py audit_torch_recaps --since 2026-09-30 --until 2026-10-02
"""

from __future__ import annotations

import csv
import datetime
import io
import json
import re
from collections import Counter, defaultdict
from statistics import median

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

DEFAULT_TENANT = "keee-torch-thc"

_RETAIL_RE = re.compile(r"retail", re.I)
_ONPREM_RE = re.compile(r"on[-\s]?prem|\bbar\b|venue", re.I)
_EVENT_RE = re.compile(r"event|activation|festival|pop[-\s]?up", re.I)
_SEEDING_RE = re.compile(r"product\s*seeding|\bseeding\b", re.I)

_NUM = r"(\d{1,5}(?:,\d{3})?)"
_SOLD_NOTE_RES = (
    re.compile(
        r"\b(?:sold|purchased|bought|moved)\s+(?:about\s+|around\s+|roughly\s+|~|over\s+)?"
        + _NUM
        + r"\b",
        re.I,
    ),
    re.compile(
        _NUM
        + r"\s+(?:units?|cans?|packs?|4[-\s]?packs?|four[-\s]?packs?|cases?|bottles?|items?|products?|sales?)"
        r"\s+(?:were\s+|was\s+)?(?:sold|purchased|bought)",
        re.I,
    ),
    re.compile(
        _NUM
        + r"\s+(?:people|consumers|customers|guests|shoppers|purchases?|sales?)\s+"
        r"(?:purchased|bought|made\s+a\s+purchase)",
        re.I,
    ),
)
_SAMPLED_NOTE_RES = (
    re.compile(
        r"\b(?:sampled|sampling)\s+(?:about\s+|around\s+|roughly\s+|~|over\s+)?"
        + _NUM
        + r"\s+(?:people|consumers|customers|guests|shoppers)",
        re.I,
    ),
    re.compile(
        _NUM
        + r"\s+(?:people|consumers|customers|guests|shoppers)\s+(?:sampled|tried|tasted)",
        re.I,
    ),
)
_STORE_NO_RE = re.compile(r"#\s*(\d+)\b")
# BA says nobody actually tasted product (dry demo / no samples on hand).
_DRY_DEMO_RE = re.compile(
    r"\bdry[- ]?(demo|sampl|tasting|educational)|did a dry|was dry|dry sampler"
    r"|didn.?t have (any )?samples|did not have (any )?samples|no samples were available"
    r"|could not sample anything|no items to actually physically sample|no product to sample"
    r"|forgot to leave samples|wasn.?t able to do sampling|did not receive my samples"
    r"|couldn.?t (sample|taste)|no samples\b|zero sampling|no actually sampling",
    re.I,
)


def _bucket(name: str | None) -> str:
    text = name or ""
    if not text.strip():
        return ""
    if _SEEDING_RE.search(text):
        return "seeding"
    if _RETAIL_RE.search(text):
        return "retail"
    if _ONPREM_RE.search(text):
        return "onprem"
    if _EVENT_RE.search(text):
        return "event"
    return "other"


def _parse_date(raw: str | None, label: str) -> datetime.date | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError as exc:
        raise CommandError(f"Bad --{label} {raw!r}: {exc}") from exc


def _local_date(value) -> datetime.date | None:
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        if timezone.is_aware(value):
            return timezone.localtime(value).date()
        return value.date()
    return value


def _norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _pct(sold: int, sampled: int) -> float | None:
    if sampled <= 0:
        return None
    return round(sold / sampled * 100, 1)


def _fmt_pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}%"


class Command(BaseCommand):
    help = "READ-ONLY audit of every Torch THC recap (conversion math + data quality)."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", default=DEFAULT_TENANT)
        parser.add_argument("--since", default="", help="YYYY-MM-DD inclusive (default: all-time)")
        parser.add_argument("--until", default="", help="YYYY-MM-DD inclusive (default: today)")
        parser.add_argument(
            "--focus-since",
            default="2026-09-30",
            help="Extra window called out separately (default 2026-09-30 → --until).",
        )
        parser.add_argument("--no-csv", action="store_true")

    # ------------------------------------------------------------------ helpers
    def _tenant(self, ident: str):
        from tenants.models import Tenant

        ident = (ident or DEFAULT_TENANT).strip()
        if ident.isdigit():
            tenant = Tenant.objects.filter(id=int(ident)).first()
        else:
            tenant = (
                Tenant.objects.filter(slug=ident).order_by("id").first()
                or Tenant.objects.filter(request_url_name=ident).order_by("id").first()
            )
        if tenant is None:
            raise CommandError(f"tenant-not-found: {ident}")
        return tenant

    def handle(self, *args, **opts):
        from recaps.filed import custom_filed_q
        from recaps.models import CustomField, CustomRecap, Recap
        from recaps.tenant_overview import tenant_conversion_kpis
        from recaps.types import (
            _account_spend_from_fields,
            _consumers_sampled_from_fields,
            _parse_recap_int,
            _samples_given_from_fields,
            _sold_units_from_fields,
        )

        tenant = self._tenant(opts["tenant"])
        since = _parse_date(opts.get("since"), "since")
        until = _parse_date(opts.get("until"), "until") or timezone.localdate()
        focus_since = _parse_date(opts.get("focus_since"), "focus-since")
        w = self.stdout.write

        w("=" * 78)
        w(f"TORCH RECAP AUDIT (read-only)  tenant=[{tenant.id}] {tenant.name!r} slug={tenant.slug!r}")
        w(f"Window: {since or 'all-time'} → {until}   focus: {focus_since} → {until}")
        w(f"Generated: {timezone.now().isoformat()}")
        w("=" * 78)

        # ---------------------------------------------------------- template fields
        w("\n## Template fields (field-change timeline)")
        fields = list(
            CustomField.objects.filter(custom_recap_template__tenant=tenant)
            .select_related("custom_recap_template", "recap_section", "custom_field_type")
            .order_by("custom_recap_template_id", "recap_section__order", "order", "id")
        )
        filed_ids = set(
            CustomRecap.objects.filter(tenant=tenant).filter(custom_filed_q()).values_list("id", flat=True)
        )

        recaps = list(
            CustomRecap.objects.filter(tenant=tenant)
            .select_related(
                "event",
                "event__request",
                "event__request__request_type",
                "event__event_type",
                "event__retailer",
                "event__state",
                "custom_recap_template",
                "custom_recap_template__event_type",
                "ambassador__user",
                "retailer",
                "created_by",
                "updated_by",
                "approved_by",
            )
            .prefetch_related(
                "custom_field_value__custom_field__custom_field_type",
                "custom_recap_files__file_recap_category",
                "custom_recap_product_sample__product",
            )
            .order_by("id")
        )
        legacy = list(
            Recap.objects.filter(event__tenant=tenant)
            .select_related("event", "event__request", "event__request__request_type")
            .order_by("id")
        )

        field_usage: dict[int, list[datetime.date]] = defaultdict(list)
        rows: list[dict] = []

        for r in recaps:
            ev = r.event
            req = getattr(ev, "request", None) if ev else None
            if req is not None and req.deleted_at is not None:
                req_deleted = True
            else:
                req_deleted = False
            ev_date = None
            if ev is not None:
                ev_date = _local_date(ev.date or ev.start_time or (req.date if req else None))
            date = ev_date or _local_date(r.submitted_at) or _local_date(r.created_at)

            values = list(r.custom_field_value.all())
            pairs: list[tuple[str | None, str | None]] = []
            kpi: dict[str, str] = {}
            notes: list[str] = []
            missing_required: list[str] = []
            present_field_ids = set()
            for v in values:
                cf = v.custom_field
                name = getattr(cf, "name", None)
                pairs.append((name, v.value))
                if (v.value or "").strip():
                    present_field_ids.add(v.custom_field_id)
                    if date:
                        field_usage[v.custom_field_id].append(date)
                ftype = (getattr(getattr(cf, "custom_field_type", None), "name", "") or "").lower()
                val = (v.value or "").strip()
                if ftype in ("longtext", "text", "textarea") and val and _parse_recap_int(val) is None:
                    notes.append(f"{name}: {val}")
                elif val and len(val) > 40:
                    notes.append(f"{name}: {val}")
                if val and len(val) <= 40:
                    kpi[name or f"field{v.custom_field_id}"] = val
            for f in fields:
                if (
                    f.required
                    and f.custom_recap_template_id == r.custom_recap_template_id
                    and f.id not in present_field_ids
                    and (f.custom_field_type is None or (f.custom_field_type.name or "").lower() not in ("file", "photo", "image"))
                ):
                    missing_required.append(f.name)

            sold = _sold_units_from_fields(pairs)
            consumers = _consumers_sampled_from_fields(pairs)
            samples_given = _samples_given_from_fields(pairs)
            spend = _account_spend_from_fields(pairs)

            skus = [
                (getattr(s.product, "name", "?"), int(s.quantity or 0))
                for s in r.custom_recap_product_sample.all()
            ]
            files = Counter()
            for f in r.custom_recap_files.all():
                if not (str(f.url or "")).strip():
                    continue
                cat = getattr(f.file_recap_category, "name", None) or "(uncategorized)"
                files[cat] += 1

            note_text = " | ".join(notes)
            sold_mentions = sorted(
                {int(m.group(1).replace(",", "")) for rx in _SOLD_NOTE_RES for m in rx.finditer(note_text)}
            )
            sampled_mentions = sorted(
                {int(m.group(1).replace(",", "")) for rx in _SAMPLED_NOTE_RES for m in rx.finditer(note_text)}
            )

            req_type = (getattr(getattr(req, "request_type", None), "name", "") or "") if req else ""
            ev_type = (getattr(getattr(ev, "event_type", None), "name", "") or "") if ev else ""
            tmpl = getattr(r.custom_recap_template, "name", "") or ""
            tmpl_type = getattr(getattr(r.custom_recap_template, "event_type", None), "name", "") or ""
            bucket_insights = _bucket(req_type) or _bucket(ev_type) or _bucket(tmpl)  # tenant_conversion_kpis
            bucket_audit = _bucket(req_type) or _bucket(ev_type) or _bucket(tmpl) or _bucket(tmpl_type) or "other"
            tmpl_bucket = _bucket(tmpl) or _bucket(tmpl_type)

            if r.archived_at is not None:
                status = "archived"
            elif r.approved:
                status = "approved"
            elif r.id in filed_ids:
                status = "needs_review"
            else:
                status = "draft_unfiled"

            ba = ""
            amb = getattr(r, "ambassador", None)
            if amb is not None and getattr(amb, "user", None) is not None:
                ba = f"{amb.user.first_name or ''} {amb.user.last_name or ''}".strip()
            if not ba:
                ba = (r.external_ba_name or "").strip()

            store_name = (r.name or "").strip() or (r.typed_store_name or "").strip() or (ev.name if ev else "")
            store_no = ""
            m = _STORE_NO_RE.search(store_name or "")
            if m:
                store_no = m.group(1)
            elif req is not None and (req.store_number or "").strip():
                store_no = (req.store_number or "").strip()

            if r.is_third_party:
                source = "agency (TH-AGENCY)"
            elif req is None:
                source = "walk-up (no request)"
            else:
                source = "request-linked"
            creator = getattr(r, "created_by", None)
            if creator is not None and amb is not None and getattr(amb, "user_id", None) != creator.id:
                source += " · filed by other user"

            conv = _pct(sold or 0, consumers or 0) if consumers else None
            dry_demo = bool(_DRY_DEMO_RE.search(note_text))

            flags: list[str] = []
            counts_toward = bucket_audit in ("retail", "onprem")
            if status in ("approved", "needs_review", "archived"):
                if consumers is None:
                    flags.append("missing_consumers_sampled")
                if counts_toward and sold is None:
                    flags.append("missing_purchases")
                if counts_toward and not skus:
                    flags.append("missing_skus")
                photo_total = sum(n for c, n in files.items() if not re.search(r"spend|receipt", c, re.I))
                if photo_total == 0:
                    flags.append("no_photos")
                if counts_toward and not any(re.search(r"spend|receipt", c, re.I) for c in files):
                    flags.append("no_product_spend_receipt")
                if missing_required:
                    flags.append("missing_required:" + ";".join(missing_required))
                if (sold is not None and sold < 0) or (consumers is not None and consumers < 0):
                    flags.append("negative_value")
                if sold is not None and consumers and sold > consumers:
                    flags.append("purchases_gt_sampled")
                if consumers is not None and consumers == 0 and counts_toward:
                    flags.append("zero_sampled")
                if sold_mentions and sold is not None and sold not in sold_mentions:
                    flags.append(f"notes_sold_mismatch(notes={sold_mentions},field={sold})")
                if sold_mentions and sold is None:
                    flags.append(f"notes_sold_but_field_blank(notes={sold_mentions})")
                if sampled_mentions and consumers is not None and consumers not in sampled_mentions:
                    flags.append(f"notes_sampled_mismatch(notes={sampled_mentions},field={consumers})")
                if (
                    r.total_engagements is not None
                    and consumers is not None
                    and r.total_engagements == sold
                    and sold not in (None, 0)
                ):
                    flags.append("engagements_equals_purchases")
                if consumers is not None and sold is not None and consumers == sold and sold > 0:
                    flags.append("sampled_equals_purchases")
                if not store_no:
                    flags.append("no_store_number")
                if not (store_name or "").strip():
                    flags.append("no_store_name")
                if tmpl_bucket and req_type and _bucket(req_type) and _bucket(req_type) != tmpl_bucket:
                    flags.append(f"activation_mismatch(request={req_type!r},template={tmpl!r})")
                if ev_type and tmpl_type and ev_type != tmpl_type:
                    flags.append(f"event_type_mismatch(event={ev_type!r},template={tmpl_type!r})")
                if req is None:
                    flags.append("no_request")
                if req_deleted:
                    flags.append("request_soft_deleted")
                if ev is not None and ev.exclude_from_dashboard:
                    flags.append("event_excluded_from_dashboard")
                if status == "needs_review":
                    age = (timezone.localdate() - (_local_date(r.submitted_at) or _local_date(r.created_at))).days
                    flags.append(f"needs_review_{age}d")
                if bucket_insights != bucket_audit and counts_toward:
                    flags.append("insights_excludes_from_conv")
                if dry_demo:
                    flags.append("dry_demo_no_tasting")
                for label, key in (("first_time", "first time"), ("knew_brand", "knew about"), ("willing", "would be willing")):
                    val = next((_parse_recap_int(v) for n, v in pairs if n and key in n.lower()), None)
                    if val is not None and consumers is not None and val > consumers:
                        flags.append(f"{label}_gt_sampled({val}>{consumers})")
                if samples_given is not None and consumers is not None and samples_given != consumers:
                    flags.append(f"insights_uses_samples_given({samples_given})_not_sampled")

            rows.append(
                {
                    "kind": "custom",
                    "id": r.id,
                    "uuid": str(r.uuid),
                    "event_id": getattr(ev, "id", None),
                    "request_id": getattr(req, "id", None),
                    "date": date.isoformat() if date else "",
                    "month": date.strftime("%Y-%m") if date else "",
                    "submitted_at": r.submitted_at.isoformat() if r.submitted_at else "",
                    "created_at": r.created_at.isoformat() if r.created_at else "",
                    "status": status,
                    "approved": bool(r.approved),
                    "archived": r.archived_at is not None,
                    "archive_reason": r.archive_reason or "",
                    "template": tmpl,
                    "template_event_type": tmpl_type,
                    "request_type": req_type,
                    "event_type": ev_type,
                    "bucket_insights": bucket_insights or "(none)",
                    "bucket_audit": bucket_audit,
                    "source": source,
                    "store": store_name,
                    "store_number": store_no,
                    "typed_store": r.typed_store_name or "",
                    "address": (r.typed_store_address or (ev.address if ev else "") or "").replace("\n", " "),
                    "state": getattr(getattr(ev, "state", None), "code", "") if ev else "",
                    "ba": ba,
                    "total_engagements": r.total_engagements,
                    "consumers_sampled": consumers,
                    "samples_given_field": samples_given,
                    "purchases": sold,
                    "skus": "; ".join(f"{n} x{q}" for n, q in skus),
                    "sku_qty_total": sum(q for _, q in skus),
                    "files": "; ".join(f"{c}={n}" for c, n in sorted(files.items())),
                    "account_spend": spend,
                    "conv_pct": conv,
                    "flags": " | ".join(flags),
                    "note_sold_mentions": ",".join(map(str, sold_mentions)),
                    "note_sampled_mentions": ",".join(map(str, sampled_mentions)),
                    "kpi_fields": json.dumps(kpi, ensure_ascii=False),
                    "notes": note_text[:1500],
                    "dry_demo": dry_demo,
                    "updated_at": r.updated_at.isoformat() if r.updated_at else "",
                    "updated_by": getattr(getattr(r, "updated_by", None), "email", "") or "",
                    "approved_at": r.approved_at.isoformat() if r.approved_at else "",
                    "approved_by": getattr(getattr(r, "approved_by", None), "email", "") or "",
                    "data_quality_flags": r.data_quality_flags or "",
                }
            )

        for lr in legacy:
            ev = lr.event
            req = getattr(ev, "request", None) if ev else None
            date = _local_date((ev.date or ev.start_time) if ev else None) or _local_date(lr.created_at)
            rows.append(
                {
                    "kind": "legacy",
                    "id": lr.id,
                    "uuid": str(lr.uuid),
                    "event_id": getattr(ev, "id", None),
                    "request_id": getattr(req, "id", None),
                    "date": date.isoformat() if date else "",
                    "month": date.strftime("%Y-%m") if date else "",
                    "submitted_at": lr.submited_at.isoformat() if lr.submited_at else "",
                    "created_at": lr.created_at.isoformat() if lr.created_at else "",
                    "status": "archived" if lr.archived_at else ("approved" if lr.approved else "needs_review"),
                    "approved": bool(lr.approved),
                    "archived": lr.archived_at is not None,
                    "request_type": getattr(getattr(req, "request_type", None), "name", "") if req else "",
                    "bucket_insights": _bucket(getattr(getattr(req, "request_type", None), "name", "") if req else ""),
                    "bucket_audit": _bucket(getattr(getattr(req, "request_type", None), "name", "") if req else "") or "other",
                    "store": lr.name or "",
                    "total_engagements": lr.total_engagements,
                    "purchases": lr.products_sold,
                    "flags": "legacy_recap_model",
                }
            )

        # Duplicates: same BA + store + date among filed recaps.
        dup_key: dict[tuple, list[int]] = defaultdict(list)
        for row in rows:
            if row.get("status") not in ("approved", "needs_review", "archived"):
                continue
            key = (_norm(row.get("ba")), _norm(row.get("store_number") or row.get("store")), row.get("date"))
            dup_key[key].append(row["id"])
        dup_ids = {}
        for key, ids in dup_key.items():
            if len(ids) > 1:
                for rid in ids:
                    dup_ids[rid] = [i for i in ids if i != rid]
        same_event_ba: dict[tuple, list[int]] = defaultdict(list)
        for row in rows:
            if row.get("status") in ("approved", "needs_review", "archived") and row.get("event_id"):
                same_event_ba[(row["event_id"], _norm(row.get("ba")))].append(row["id"])
        for ids in same_event_ba.values():
            if len(ids) > 1:
                for rid in ids:
                    dup_ids.setdefault(rid, [i for i in ids if i != rid])

        # Outliers vs the retail median.
        retail_sampled = [
            r["consumers_sampled"]
            for r in rows
            if r.get("bucket_audit") in ("retail", "onprem") and r.get("consumers_sampled")
        ]
        retail_sold = [
            r["purchases"]
            for r in rows
            if r.get("bucket_audit") in ("retail", "onprem") and r.get("purchases")
        ]
        med_s = median(retail_sampled) if retail_sampled else 0
        med_p = median(retail_sold) if retail_sold else 0
        for row in rows:
            extra = []
            if row["id"] in dup_ids:
                extra.append(f"duplicate_of({','.join(map(str, dup_ids[row['id']]))})")
            cs = row.get("consumers_sampled")
            if cs and med_s and cs >= max(5 * med_s, 300):
                extra.append(f"outlier_sampled({cs} vs median {med_s:g})")
            pu = row.get("purchases")
            if pu and med_p and pu >= max(5 * med_p, 60):
                extra.append(f"outlier_purchases({pu} vs median {med_p:g})")
            if extra:
                row["flags"] = " | ".join([f for f in [row.get("flags", "")] if f] + extra)

        def _in(row, lo, hi):
            d = row.get("date")
            if not d:
                return False
            dd = datetime.date.fromisoformat(d)
            return (lo is None or dd >= lo) and (hi is None or dd <= hi)

        rows_w = [r for r in rows if _in(r, since, until)]

        # ---------------------------------------------------------- field timeline
        for f in fields:
            used = field_usage.get(f.id, [])
            w(
                f"  [{f.id}] tmpl={f.custom_recap_template.name!r} sec={getattr(f.recap_section, 'name', '')!r} "
                f"type={getattr(f.custom_field_type, 'name', '')} req={f.required} created={_local_date(f.created_at)} "
                f"used={len(used)} first={min(used) if used else '-'} last={max(used) if used else '-'} :: {f.name!r}"
            )

        # ---------------------------------------------------------- counts
        w("\n## Recap counts by month × status (event date)")
        by_month = defaultdict(Counter)
        for r in rows_w:
            by_month[r["month"] or "(no date)"][r["status"]] += 1
        statuses = ["approved", "needs_review", "archived", "draft_unfiled"]
        w("  month     " + "  ".join(f"{s:>13}" for s in statuses) + "   total")
        for mth in sorted(by_month):
            c = by_month[mth]
            w(f"  {mth:9} " + "  ".join(f"{c[s]:>13}" for s in statuses) + f"   {sum(c.values()):>5}")
        tot = Counter(r["status"] for r in rows_w)
        w("  TOTAL     " + "  ".join(f"{tot[s]:>13}" for s in statuses) + f"   {sum(tot.values()):>5}")
        w("  by bucket (filed): " + json.dumps(Counter(r["bucket_audit"] for r in rows_w if r["status"] != "draft_unfiled")))
        w("  by source (filed): " + json.dumps(Counter(r.get("source", "") for r in rows_w if r["status"] != "draft_unfiled")))
        w("  by template (filed): " + json.dumps(Counter(r.get("template", "") for r in rows_w if r["status"] != "draft_unfiled")))
        w("  by request type (filed): " + json.dumps(Counter(r.get("request_type", "") or "(none)" for r in rows_w if r["status"] != "draft_unfiled")))
        w("  by event type (filed): " + json.dumps(Counter(r.get("event_type", "") or "(none)" for r in rows_w if r["status"] != "draft_unfiled")))

        # ---------------------------------------------------------- conversion
        def _pool(subset, require_sold=False):
            sold = sampled = n = 0
            for r in subset:
                cs = r.get("consumers_sampled")
                if not cs or cs <= 0:
                    continue
                if require_sold and r.get("purchases") is None:
                    continue
                sold += max(0, r.get("purchases") or 0)
                sampled += cs
                n += 1
            return sold, sampled, n

        def _conv_rows(subset):
            return [
                r
                for r in subset
                if r.get("bucket_audit") in ("retail", "onprem") and r.get("status") == "approved"
            ]

        def _conv_line(label, subset):
            rows_c = _conv_rows(subset)
            s, b, n = _pool(rows_c)
            ls, lb, ln = _pool([r for r in rows_c if not r.get("dry_demo")])
            ds, db, dn = _pool([r for r in rows_c if r.get("dry_demo")])
            return (
                f"  {label:24} approved retail/on-prem: sold {s} ÷ sampled {b} = {_fmt_pct(_pct(s, b))} "
                f"(n={n}) | live-sampled only: {ls}/{lb} = {_fmt_pct(_pct(ls, lb))} (n={ln}) "
                f"| dry demos: {ds}/{db} = {_fmt_pct(_pct(ds, db))} (n={dn})"
            )

        w("\n## Conversion — audited basis (approved, non-archived, Retail + On-Premise by request→event→template type; purchases ÷ consumers sampled)")
        w(_conv_line("WINDOW", rows_w))
        if focus_since:
            w(_conv_line(f"FOCUS {focus_since}→{until}", [r for r in rows if _in(r, focus_since, until)]))
        for mth in sorted(m for m in by_month if m != "(no date)"):
            w(_conv_line(mth, [r for r in rows_w if r["month"] == mth]))

        w("\n## Conversion incl. Needs-review (what it would be if pending recaps are approved as-is)")
        def _conv_line_nr(label, subset):
            sub = [r for r in subset if r.get("bucket_audit") in ("retail", "onprem") and r.get("status") in ("approved", "needs_review")]
            s, b, n = _pool(sub)
            return f"  {label:24} sold {s} ÷ sampled {b} = {_fmt_pct(_pct(s, b))} (n={n})"
        w(_conv_line_nr("WINDOW", rows_w))
        if focus_since:
            w(_conv_line_nr(f"FOCUS {focus_since}→{until}", [r for r in rows if _in(r, focus_since, until)]))

        w("\n## Conversion — what Insights shows today (tenant_conversion_kpis)")
        dated = [datetime.date.fromisoformat(r["date"]) for r in rows if r.get("date")]
        first = since or (min(dated) if dated else until)
        windows = [("WINDOW", first, until)]
        if focus_since:
            windows.append((f"FOCUS {focus_since}→{until}", focus_since, until))
        for mth in sorted(m for m in by_month if m != "(no date)"):
            y, mo = map(int, mth.split("-"))
            lo = datetime.date(y, mo, 1)
            hi = (datetime.date(y + (mo == 12), mo % 12 + 1, 1) - datetime.timedelta(days=1))
            windows.append((mth, lo, min(hi, until)))
        for label, lo, hi in windows:
            k = tenant_conversion_kpis(tenant.id, start=lo, end=hi)
            w(f"  {label:24} sold {k['sold']} ÷ base {k['engagements']} = {_fmt_pct(k['pct'])}")

        w("\n## Per store (approved retail/on-prem, window)")
        by_store = defaultdict(list)
        for r in _conv_rows(rows_w):
            by_store[r.get("store") or "(none)"].append(r)
        store_lines = []
        for store, sub in by_store.items():
            s, b, n = _pool(sub)
            store_lines.append((b, f"  {_fmt_pct(_pct(s, b)):>7}  sold {s:>4} / sampled {b:>5}  n={n:<3} {store}"))
        for _, line in sorted(store_lines, reverse=True):
            w(line)

        w("\n## Per BA (approved retail/on-prem, window)")
        by_ba = defaultdict(list)
        for r in _conv_rows(rows_w):
            by_ba[r.get("ba") or "(unknown)"].append(r)
        ba_lines = []
        for ba, sub in by_ba.items():
            s, b, n = _pool(sub)
            ba_lines.append((b, f"  {_fmt_pct(_pct(s, b)):>7}  sold {s:>4} / sampled {b:>5}  n={n:<3} {ba}"))
        for _, line in sorted(ba_lines, reverse=True):
            w(line)

        # ---------------------------------------------------------- issues
        w("\n## Issues by type (filed recaps in window)")
        issue_ids: dict[str, list[int]] = defaultdict(list)
        for r in rows_w:
            for flag in [f.strip() for f in (r.get("flags") or "").split("|") if f.strip()]:
                key = re.sub(r"\(.*", "", flag)
                key = re.sub(r"_\d+d$", "", key)
                key = key.split(":")[0]
                issue_ids[key].append(r["id"])
        for key in sorted(issue_ids, key=lambda k: -len(issue_ids[k])):
            ids = issue_ids[key]
            w(f"  {key:40} {len(ids):>4}  ids={','.join(map(str, ids[:400]))}")

        w("\n## Recap detail (window, filed only)")
        for r in rows_w:
            if r["status"] == "draft_unfiled":
                continue
            w(
                f"- #{r['id']} {r['date']} [{r['status']}] {r.get('bucket_audit')}/{r.get('bucket_insights')} "
                f"src={r.get('source', '')} store={r.get('store', '')!r} ba={r.get('ba', '')!r} "
                f"eng={r.get('total_engagements')} sampled={r.get('consumers_sampled')} "
                f"given={r.get('samples_given_field')} purchased={r.get('purchases')} conv={_fmt_pct(r.get('conv_pct'))} "
                f"skus=[{r.get('skus', '')}] files=[{r.get('files', '')}] spend={r.get('account_spend')}"
            )
            w(
                f"    updated={r.get('updated_at', '')} by {r.get('updated_by', '')!r} "
                f"approved={r.get('approved_at', '')} by {r.get('approved_by', '')!r}"
            )
            w(f"    kpi={r.get('kpi_fields', '')}")
            if r.get("flags"):
                w(f"    flags: {r['flags']}")
            if r.get("notes"):
                w(f"    notes: {r['notes'][:700]}")

        if not opts.get("no_csv"):
            cols = [
                "kind", "id", "uuid", "date", "month", "status", "approved", "archived", "archive_reason",
                "bucket_audit", "bucket_insights", "template", "template_event_type", "request_type",
                "event_type", "source", "event_id", "request_id", "store", "store_number", "typed_store",
                "address", "state", "ba", "total_engagements", "consumers_sampled", "samples_given_field",
                "purchases", "conv_pct", "skus", "sku_qty_total", "files", "account_spend", "flags",
                "dry_demo", "note_sold_mentions", "note_sampled_mentions", "kpi_fields", "notes", "submitted_at",
                "created_at", "updated_at", "updated_by", "approved_at", "approved_by",
                "data_quality_flags",
            ]
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
            writer.writeheader()
            for r in rows_w:
                writer.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in cols})
            w("\n===CSV-BEGIN===")
            w(buf.getvalue().rstrip("\n"))
            w("===CSV-END===")
