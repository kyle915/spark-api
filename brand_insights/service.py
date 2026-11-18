"""Write a brand's findings, store them, and read them back.

Pipeline: approved visits (:mod:`brand_insights.facts`) -> findings worked
out in code (:mod:`brand_insights.detectors`) -> the model may reword each
sentence -> stored as a DRAFT -> Ignite staff publish it -> the brand sees it.

Wording: one model call per brand per run at most, through
:func:`recaps.explain_text._ask_model` (``utils.ai_text`` underneath). A
reworded sentence is kept only when every number in it is one of that
finding's own numbers, it uses no number or ratio word nobody supplied, and
it does not say a drop was a rise (:func:`recaps.explain_text.accept`).
Otherwise the model's draft is dropped and the sentence built in code is
used, so a finding never carries a number the recaps do not hold.

Runs are safe to repeat. A finding that staff published or hid is never
rewritten. A draft is rewritten only when its numbers changed, and a draft
a later run no longer finds is marked stale (never shown, never deleted).

Nothing in this module sends anything to anyone.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from brand_insights import detectors
from brand_insights.detectors import Finding
from brand_insights.facts import gather_visits

logger = logging.getLogger(__name__)

# A brand sees recommendations from a run at most this old, and alerts about
# a week that ended at most this long ago.
RECOMMENDATION_MAX_AGE_DAYS = 21
ALERT_MAX_AGE_DAYS = 14


def auto_publish_recommendations() -> bool:
    """Off: staff publish recommendations too. One setting turns it on, the
    way weekly summaries publish as soon as they are written."""
    return bool(getattr(settings, "BRAND_INSIGHTS_AUTO_PUBLISH_RECOMMENDATIONS", False))


def ops_today() -> date:
    from events.live_board import _ops_today

    return _ops_today()


def run_periods(today: date) -> dict[str, tuple[date, date]]:
    """``{kind: (period_start, period_end)}`` for a run on ``today``.

    Recommendations belong to the week the run is in (Monday to today), so a
    daily run refreshes the same set and a new week starts a new one. Alerts
    are about the last finished week."""
    from recaps.weekly_narration import previous_complete_week

    monday = today - timedelta(days=today.weekday())
    return {
        detectors.KIND_RECOMMENDATION: (monday, today),
        detectors.KIND_ALERT: previous_complete_week(today),
    }


def find(tenant_id: int, today: date) -> list[tuple[Finding, date, date]]:
    """Every finding for one brand on ``today``, with its period."""
    periods = run_periods(today)
    rec_start = today - timedelta(days=detectors.LOOKBACK_DAYS - 1)
    week_start, week_end = periods[detectors.KIND_ALERT]
    alert_start = week_end - timedelta(days=detectors.DROP_DAYS * 2 - 1)
    visits = gather_visits(tenant_id, min(rec_start, alert_start), today)
    recent = [v for v in visits if v.day >= rec_start]
    up_to_week = [v for v in visits if alert_start <= v.day <= week_end]
    out = []
    for finding in detectors.recommendations(recent, today):
        out.append((finding, *periods[detectors.KIND_RECOMMENDATION]))
    for finding in detectors.alerts(up_to_week, week_start, week_end):
        out.append((finding, week_start, week_end))
    return out


def _phrase(findings: list[Finding], *, use_model: bool) -> dict[str, tuple[str, str]]:
    """``{scope id: (text, source)}``: the model's wording when it passes the
    number check, else the template."""
    from recaps.explain_text import SOURCE_MODEL, SOURCE_TEMPLATE, Draft, _ask_model, accept

    drafts = [
        Draft(
            key=f"{f.code}|{f.scope_key}",
            template=f.template,
            facts=f.facts_text,
            directions=f.directions,
        )
        for f in findings
    ]
    answers = _ask_model(drafts) if (use_model and drafts) else {}
    out: dict[str, tuple[str, str]] = {}
    for draft in drafts:
        text = answers.get(draft.key)
        if accept(text, draft):
            out[draft.key] = (" ".join(str(text).split()), SOURCE_MODEL)
        else:
            out[draft.key] = (draft.template, SOURCE_TEMPLATE)
    return out


def generate_for_tenant(tenant_id: int, today: date | None = None, *, use_model: bool = True) -> dict:
    """Find, word and store one brand's findings. Returns counts."""
    from brand_insights.models import BrandInsight as Row

    today = today or ops_today()
    found = find(tenant_id, today)
    now = timezone.now()
    periods = run_periods(today)

    existing = {
        (r.kind, r.code, r.scope_key, r.period_start): r
        for r in Row.objects.filter(
            tenant_id=tenant_id,
            period_start__in={start for start, _end in periods.values()},
        )
    }
    to_write: list[tuple[Finding, date, date, Row | None]] = []
    seen: set[tuple] = set()
    for finding, start, end in found:
        key = (finding.kind, finding.code, finding.scope_key, start)
        seen.add(key)
        row = existing.get(key)
        if row is not None and row.status in (Row.STATUS_PUBLISHED, Row.STATUS_HIDDEN):
            continue
        if (
            row is not None
            and row.status == Row.STATUS_DRAFT
            and row.facts_text == finding.facts_text
            and row.template == finding.template
        ):
            continue
        to_write.append((finding, start, end, row))

    words = _phrase([f for f, *_rest in to_write], use_model=use_model)
    auto = auto_publish_recommendations()
    counts = {"found": len(found), "written": 0, "stale": 0}
    with transaction.atomic():
        for finding, start, end, row in to_write:
            text, source = words[f"{finding.code}|{finding.scope_key}"]
            publish = auto and finding.kind == Row.KIND_RECOMMENDATION
            values = {
                "period_end": end,
                "rank": finding.rank,
                "subject": finding.subject[:200],
                "template": finding.template,
                "body": text,
                "source": source,
                "facts_text": finding.facts_text,
                "facts": finding.facts,
                "status": Row.STATUS_PUBLISHED if publish else Row.STATUS_DRAFT,
                "generated_at": now,
                "reviewed_by": None,
                "reviewed_at": now if publish else None,
            }
            Row.objects.update_or_create(
                tenant_id=tenant_id,
                kind=finding.kind,
                code=finding.code,
                scope_key=finding.scope_key,
                period_start=start,
                defaults=values,
            )
            counts["written"] += 1
        for key, row in existing.items():
            if key not in seen and row.status == Row.STATUS_DRAFT:
                row.status = Row.STATUS_STALE
                row.save(update_fields=["status", "updated_at"])
                counts["stale"] += 1
    return counts


def tenants_with_recent_recaps(today: date) -> list[int]:
    """Active brands with an approved recap in the recommendation window."""
    from recaps.weekly_summary import _recap_counts
    from recaps.tenant_overview import _inclusive_dates_to_window
    from tenants.models import Tenant

    start = today - timedelta(days=detectors.LOOKBACK_DAYS - 1)
    counts = _recap_counts(_inclusive_dates_to_window(start, today), approved=True)
    return list(
        Tenant.active().filter(id__in=set(counts)).order_by("id").values_list("id", flat=True)
    )


def generate_all(
    today: date | None = None, *, tenant_id: int | None = None, apply: bool = False
) -> dict:
    """The daily job. Dry run (the default) works out the findings and
    stores nothing and calls no model. Sends nothing."""
    from tenants.models import Tenant

    today = today or ops_today()
    ids = tenants_with_recent_recaps(today)
    if tenant_id is not None:
        ids = [i for i in ids if i == tenant_id]
    names = dict(Tenant.objects.filter(id__in=ids).values_list("id", "name"))
    results = []
    for tid in ids:
        entry = {"tenant_id": tid, "tenant_name": names.get(tid, "")}
        try:
            if apply:
                entry.update(generate_for_tenant(tid, today))
            else:
                found = find(tid, today)
                entry["found"] = len(found)
                entry["codes"] = sorted({f.code for f, *_r in found})
        except Exception:  # noqa: BLE001 — one brand must not stop the rest
            logger.exception("brand insights failed tenant=%s", tid)
            entry["error"] = "failed"
        results.append(entry)
    return {
        "date": today.isoformat(),
        "mode": "apply" if apply else "dry_run",
        "brands": len(results),
        "written": sum(int(r.get("written") or 0) for r in results),
        "emails_sent": 0,
        "tenants": results,
    }


# ─── Reading ────────────────────────────────────────────────────────


def visible_statuses(include_drafts: bool) -> tuple[str, ...]:
    from brand_insights.models import BrandInsight as Row

    if include_drafts:
        return (Row.STATUS_PUBLISHED, Row.STATUS_DRAFT)
    return (Row.STATUS_PUBLISHED,)


def brand_view(tenant_id: int, *, include_drafts: bool, today: date | None = None) -> dict:
    """What a brand's dashboard shows: ``{"recommendations": [...],
    "alerts": [...]}``. A brand user (``include_drafts=False``) gets
    published rows only; staff also get drafts, marked as such."""
    from brand_insights.models import BrandInsight as Row

    today = today or ops_today()
    statuses = visible_statuses(include_drafts)
    recs = Row.objects.filter(
        tenant_id=tenant_id,
        kind=Row.KIND_RECOMMENDATION,
        status__in=statuses,
        period_start__gte=today - timedelta(days=RECOMMENDATION_MAX_AGE_DAYS),
    )
    latest = recs.order_by("-period_start").values_list("period_start", flat=True).first()
    rec_rows = (
        list(recs.filter(period_start=latest).order_by("code", "rank", "id")) if latest else []
    )
    order = {code: i for i, code in enumerate(detectors.GO_BACK_CODES + detectors.FIX_CODES)}
    rec_rows.sort(key=lambda r: (order.get(r.code, 99), r.rank, r.id))
    alert_rows = list(
        Row.objects.filter(
            tenant_id=tenant_id,
            kind=Row.KIND_ALERT,
            status__in=statuses,
            period_end__gte=today - timedelta(days=ALERT_MAX_AGE_DAYS),
        ).order_by("-period_start", "rank", "id")
    )
    return {"recommendations": rec_rows, "alerts": alert_rows}


def review_rows(today: date | None = None) -> list[dict]:
    """Every brand with a recent finding, for staff: drafts, published and
    hidden rows of the current runs (never stale ones)."""
    from brand_insights.models import BrandInsight as Row
    from tenants.models import Tenant

    today = today or ops_today()
    since = today - timedelta(days=RECOMMENDATION_MAX_AGE_DAYS)
    rows = (
        Row.objects.filter(period_start__gte=since)
        .exclude(status=Row.STATUS_STALE)
        .select_related("reviewed_by")
        .order_by("tenant_id", "kind", "-period_start", "code", "rank", "id")
    )
    by_tenant: dict[int, list] = {}
    for row in rows:
        by_tenant.setdefault(row.tenant_id, []).append(row)
    out = []
    for tenant in Tenant.objects.filter(id__in=list(by_tenant)).order_by("name"):
        items = by_tenant[tenant.id]
        out.append(
            {
                "tenant_id": tenant.id,
                "tenant_name": tenant.name or "",
                "drafts": sum(1 for r in items if r.status == Row.STATUS_DRAFT),
                "items": items,
            }
        )
    # Brands with something to review first.
    out.sort(key=lambda r: (r["drafts"] == 0, r["tenant_name"].casefold()))
    return out


def set_status(row_id: int, status: str, user) -> object | None:
    from brand_insights.models import BrandInsight as Row

    if status not in (Row.STATUS_DRAFT, Row.STATUS_PUBLISHED, Row.STATUS_HIDDEN):
        raise ValueError("bad status")
    row = Row.objects.filter(id=row_id).exclude(status=Row.STATUS_STALE).first()
    if row is None:
        return None
    row.status = status
    if status == Row.STATUS_DRAFT:
        row.reviewed_by = None
        row.reviewed_at = None
    else:
        row.reviewed_by_id = getattr(user, "pk", None)
        row.reviewed_at = timezone.now()
    row.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])
    return Row.objects.select_related("reviewed_by").get(id=row.id)


def publish_drafts(tenant_id: int, user, ids: list[int] | None = None) -> int:
    """Publish a brand's current drafts (or just ``ids`` of them). One click."""
    from brand_insights.models import BrandInsight as Row

    today = ops_today()
    rows = Row.objects.filter(
        tenant_id=tenant_id,
        status=Row.STATUS_DRAFT,
        period_start__gte=today - timedelta(days=RECOMMENDATION_MAX_AGE_DAYS),
    )
    if ids is not None:
        rows = rows.filter(id__in=ids)
    return rows.update(
        status=Row.STATUS_PUBLISHED,
        reviewed_by_id=getattr(user, "pk", None),
        reviewed_at=timezone.now(),
        updated_at=timezone.now(),
    )
