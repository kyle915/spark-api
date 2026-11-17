"""Read-only: what the recap "number fixes" change, per brand.

Three changes ship together and each moves client-facing numbers:

1. **Approved only.** The client campaign report, its share link / PDF, and
   the client Home headline tiles counted recaps nobody had approved (and
   approved recaps later archived). They now count approved, unarchived
   recaps on live requests only.
2. **Give-away is not sold.** A field worded as product given away
   ("TOTAL - how many cans did you give away?", "Cans sampled") matched the
   SOLD vocabulary because it says "cans". It no longer counts as sold.
3. **One number reader.** "1,234" read as 1234 on some screens and 1 on
   others; "12.5" read as 125; "sampled 50, sold 20" read as 5020. Every
   path now uses :func:`recaps.numbers.parse_count`.

This module measures each one separately for a tenant, so Kyle can see what
moves before and after merge. The "before" rules are reproduced here (the
old digit-strip and first-digits readers, and the SOLD matcher with the
give-away exclusion turned off), so the audit reads the same after deploy.

Writes nothing. Reports field NAMES, counts and short numeric answers only:
no ambassador, consumer or contact names.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import timedelta

from django.db.models import Q, Sum
from django.utils import timezone

MAX_EXAMPLES = 8
MAX_VALUE_CHARS = 24


# ── the "before" readers ────────────────────────────────────────────────
def old_strip_parse(value) -> int | None:
    """Pre-fix ``recaps.types._parse_recap_int``: strip every non-digit /
    non-'-' character, then read a leading integer. Fed the recap list,
    Program KPIs, the dashboard hero and the precheck."""
    if value is None:
        return None
    cleaned = re.sub(r"[^\d-]", "", str(value))
    match = re.match(r"-?\d+", cleaned)
    return int(match.group(0)) if match else None


def old_first_digits(value) -> int | None:
    """Pre-fix ``_leading_int``: the first run of digits. Fed the campaign
    report, its PDF, the cans / packs split and the field export."""
    if value is None:
        return None
    m = re.search(r"\d+", str(value))
    return int(m.group()) if m else None


def _short(value) -> str:
    text = str(value or "").strip().replace("\n", " ")
    return text if len(text) <= MAX_VALUE_CHARS else text[: MAX_VALUE_CHARS - 1] + "…"


def _visible(approved, archived_at) -> bool:
    return bool(approved) and archived_at is None


# ── 1. approved only ────────────────────────────────────────────────────
def _approval_section(tenant_id: int) -> dict:
    from recaps import report_service
    from recaps.models import ConsumerEngagements, CustomRecap, Recap
    from recaps.tenant_overview import _event_date_expr
    from recaps.types import _consumers_sampled_from_fields

    live_request = Q(event__request__isnull=True) | Q(
        event__request__deleted_at__isnull=True
    )
    custom = CustomRecap.objects.filter(live_request, tenant_id=tenant_id)
    legacy = Recap.objects.filter(live_request, event__tenant_id=tenant_id)

    def _states(qs):
        rows = list(qs.values_list("approved", "archived_at"))
        return {
            "total": len(rows),
            "not_approved": sum(1 for a, _ in rows if not a),
            "approved_but_archived": sum(1 for a, z in rows if a and z is not None),
        }

    # The campaign report, summed over every live request: what a client saw
    # (every recap) versus what they see now (approved, unarchived).
    before = defaultdict(int)
    after = defaultdict(int)
    requests_changed = 0
    keys = ("recaps", "consumers_reached", "products_sold", "cans_sold", "packs_sold",
            "samples_distributed", "willing_to_purchase")
    now = timezone.now().isoformat()
    for request_obj in report_service.report_request_queryset().filter(tenant_id=tenant_id):
        b = report_service.build_campaign_report(request_obj, generated_at=now).kpis
        a = report_service.build_campaign_report(
            request_obj, generated_at=now, approved_only=True
        ).kpis
        moved = False
        for key in keys:
            before[key] += getattr(b, key)
            after[key] += getattr(a, key)
            moved = moved or getattr(b, key) != getattr(a, key)
        requests_changed += int(moved)

    # Client Home tiles: the last 30 days by event date (the tile's window).
    start = (timezone.now() - timedelta(days=30)).date()
    hidden_custom = (
        custom.filter(Q(approved=False) | Q(archived_at__isnull=False), exclude_from_aggregates=False)
        .annotate(_evtdate=_event_date_expr("event__"))
        .filter(_evtdate__date__gte=start)
        .prefetch_related("custom_field_value__custom_field")
    )
    hidden_consumers = 0
    hidden_recaps = 0
    for recap in hidden_custom:
        hidden_recaps += 1
        pairs = [
            (getattr(v.custom_field, "name", None), v.value)
            for v in recap.custom_field_value.all()
        ]
        hidden_consumers += _consumers_sampled_from_fields(pairs) or 0
    hidden_legacy = (
        legacy.filter(Q(approved=False) | Q(archived_at__isnull=False))
        .annotate(_evtdate=_event_date_expr("event__"))
        .filter(_evtdate__date__gte=start)
    )
    hidden_recaps += hidden_legacy.count()
    hidden_consumers += (
        ConsumerEngagements.objects.filter(recap__in=hidden_legacy).aggregate(
            n=Sum("total_consumer", default=0)
        )["n"]
        or 0
    )

    return {
        "custom_recaps": _states(custom),
        "legacy_recaps": _states(legacy),
        "campaign_reports_all_requests": {
            "requests_that_change": requests_changed,
            "before": dict(before),
            "after": dict(after),
        },
        "client_home_last_30_days": {
            "recaps_no_longer_counted": hidden_recaps,
            "consumers_no_longer_counted": int(hidden_consumers),
        },
    }


# ── 2 + 3. field-level: SOLD wording and number parsing ─────────────────
def _field_sections(tenant_id: int) -> tuple[dict, dict]:
    from recaps import metric_mapping
    from recaps.models import CustomFieldValue
    from recaps.numbers import parse_count
    from recaps.tenant_overview import _CUSTOM_KPI_NAME_RE
    from recaps.types import _sold_unit_lines

    rows = list(
        CustomFieldValue.objects.filter(
            Q(custom_recap__event__request__isnull=True)
            | Q(custom_recap__event__request__deleted_at__isnull=True),
            custom_recap__tenant_id=tenant_id,
            custom_recap__exclude_from_aggregates=False,
        ).values_list(
            "custom_recap_id",
            "custom_field_id",
            "custom_field__name",
            "value",
            "custom_recap__approved",
            "custom_recap__archived_at",
        )
    )
    confirmed = metric_mapping.confirmed_metrics(field_ids={r[1] for r in rows})

    # SOLD, per recap, before vs after the give-away exclusion. Both sides
    # use the new number reader, so this isolates the wording change.
    by_recap: dict = defaultdict(list)
    visible_recap: dict = {}
    for recap_id, field_id, name, value, approved, archived_at in rows:
        by_recap[recap_id].append((metric_mapping.tag_name(name, field_id, confirmed), value))
        visible_recap[recap_id] = _visible(approved, archived_at)
    dropped: dict = defaultdict(lambda: {"recaps": 0, "units": 0})
    sold_before = sold_after = 0
    sold_before_all = sold_after_all = 0
    recaps_moved = 0
    for recap_id, pairs in by_recap.items():
        old_lines = _sold_unit_lines(pairs, exclude_giveaway=False)
        new_lines = _sold_unit_lines(pairs)
        b = sum(n for _, n in old_lines)
        a = sum(n for _, n in new_lines)
        sold_before_all += b
        sold_after_all += a
        if visible_recap[recap_id]:
            sold_before += b
            sold_after += a
        if b != a:
            recaps_moved += 1
            kept = {str(n) for n, _ in new_lines}
            for name, n in old_lines:
                if str(name) not in kept:
                    dropped[str(name)]["recaps"] += 1
                    dropped[str(name)]["units"] += n
    sold = {
        "recaps_that_change": recaps_moved,
        "fields_no_longer_sold": [
            {"field": name, **counts}
            for name, counts in sorted(dropped.items(), key=lambda kv: -kv[1]["units"])
        ],
        "units_sold_client_visible": {"before": sold_before, "after": sold_after},
        "units_sold_all_recaps": {"before": sold_before_all, "after": sold_after_all},
    }

    # Parsing: every answer on a KPI-named field (the fields any total reads).
    checked = changed = 0
    delta_strip = delta_first = 0
    examples: list[dict] = []
    seen_values: set = set()
    by_field: dict = defaultdict(int)
    for _recap_id, _field_id, name, value, approved, archived_at in rows:
        if not name or not _CUSTOM_KPI_NAME_RE.search(name):
            continue
        if value is None or str(value).strip() == "":
            continue
        checked += 1
        new = parse_count(value)
        strip = old_strip_parse(value)
        first = old_first_digits(value)
        if new == strip and new == first:
            continue
        changed += 1
        by_field[name] += 1
        if _visible(approved, archived_at):
            delta_strip += (new or 0) - (strip or 0)
            delta_first += (new or 0) - (first or 0)
        key = _short(value)
        if key not in seen_values and len(examples) < MAX_EXAMPLES:
            seen_values.add(key)
            examples.append({"field": name, "value": key, "old_strip": strip,
                             "old_first_digits": first, "new": new})
    parsing = {
        "answers_checked": checked,
        "answers_read_differently": changed,
        "by_field": dict(sorted(by_field.items(), key=lambda kv: -kv[1])),
        # Net change on client-visible recaps, versus each old reader. The
        # strip reader fed the recap list / Program KPIs / dashboard; the
        # first-digits reader fed the campaign report and its cans / packs.
        "net_change_vs_strip_reader": delta_strip,
        "net_change_vs_first_digits_reader": delta_first,
        "examples": examples,
    }
    return sold, parsing


def build_number_fixes_audit(tenant_id: int, *, include_reports: bool = True) -> dict:
    """The three sections for one tenant. ``include_reports=False`` skips the
    per-request campaign report rebuild (the slow part on a big tenant)."""
    sold, parsing = _field_sections(tenant_id)
    out = {"tenant_id": tenant_id, "sold_wording": sold, "number_parsing": parsing}
    if include_reports:
        out["approved_only"] = _approval_section(tenant_id)
    return out
