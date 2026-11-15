"""A written summary of a brand's week, drafted for Ignite staff.

Each brand gets a few short paragraphs about the last finished week,
Monday to Sunday by event date: what ran, the headline numbers, what
consumers said, and anything the brand should look at. Staff read the
draft, edit it, copy it and mark it reviewed. Nothing in this module sends
anything to anyone.

How it differs from :mod:`recaps.weekly_narration` (the ops paragraph on the
dashboard): that one is written for Ignite, about FILED recaps, from the
insight buckets. This one is written to be passed on to the brand, so it is
built only from what the brand is shown:

* the numbers are the dashboard's own roll-ups over the week
  (:func:`recaps.tenant_overview._tenant_kpi_totals_window`,
  :func:`recaps.consumer_breakdown.tenant_consumer_breakdown`,
  :func:`recaps.tenant_overview.tenant_conversion_kpis`), handed to the
  model as facts. It is told to copy them, and a draft holding a number
  that is not in the facts is thrown away
  (:func:`recaps.weekly_narration.numbers_are_grounded`);
* the comments are those of approved, unarchived recaps under a live
  request, with ambassador names, emails and phone numbers taken out
  (:func:`recaps.feedback_themes.gather_comments`).

When the model cannot be reached no draft is stored and the caller is told
it is not available.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta

import strawberry
from asgiref.sync import sync_to_async
from django.db.models import Count
from django.utils import timezone

from recaps.consumer_breakdown import KIND_NAMED, tenant_consumer_breakdown
from recaps.feedback_themes import KIND_DECLINE, KIND_QUESTION, gather_comments
from recaps.filed import custom_filed_q, legacy_filed_q
from recaps.models import CustomRecap, Recap
from recaps.tenant_overview import (
    _approved_only,
    _filter_event_window,
    _inclusive_dates_to_window,
    _tenant_event_recap_counts_window,
    _tenant_kpi_totals_window,
    tenant_conversion_kpis,
)
from recaps.weekly_narration import numbers_are_grounded, previous_complete_week
from utils.ai_text import AiUnavailable, generate_summary
from utils.graphql.mixins import resolve_id_to_int
from utils.graphql.permissions import StrictIsAuthenticated

logger = logging.getLogger(__name__)

MAX_STORES = 5
MAX_COMMENTS = 40
MAX_EDIT_CHARS = 6000

REASON_UNAVAILABLE = "The summary could not be written right now."
REASON_NO_RECAPS = "No approved recaps for this week."
REASON_UNGROUNDED = (
    "The draft used a number that is not in the approved recaps, so it was not kept."
)
REASON_NOT_FOUND = "Summary not found."

_SYSTEM = (
    "You write a short summary of one week of a brand's in-store sampling "
    "program. Ignite staff will check it and pass it on to the brand, so "
    "write to the brand in plain, short sentences. Use ONLY what is in "
    "FACTS. Copy every number exactly as given, with the label it has; never "
    "add, subtract, average, round or estimate, and never write a number "
    "that is not in FACTS. Write three or four short paragraphs separated by "
    "a blank line, in this order: what ran (events and where), the headline "
    "numbers, what consumers said, and then anything the brand should look "
    "at, such as reasons people gave for not buying, questions people asked, "
    "or a change from the previous week. Leave a paragraph out when FACTS "
    "has nothing for it. Do not name any brand ambassador or shopper. No "
    "greeting, no sign-off, no headings, no bullet points, no markdown, no "
    "exclamation marks, no sales language. Stay under 170 words."
)


def week_bounds(raw: str | None, today: date | None = None) -> tuple[date, date] | None:
    """Monday to Sunday for ``raw`` (YYYY-MM-DD, any day of that week), or
    the last finished week when ``raw`` is empty. None when unreadable."""
    if not raw:
        return previous_complete_week(today)
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        return None
    start = day - timedelta(days=day.weekday())
    return start, start + timedelta(days=6)


def _ops_week(today: date | None = None) -> tuple[date, date]:
    if today is None:
        from events.live_board import _ops_today

        today = _ops_today()
    return previous_complete_week(today)


def _week_label(start: date, end: date) -> str:
    return f"{start.strftime('%b %-d')} to {end.strftime('%b %-d, %Y')}"


def week_facts(tenant_id: int, start: date, end: date) -> dict:
    """Everything the draft may say, from approved recaps of that week."""
    from tenants.models import Tenant

    window = _inclusive_dates_to_window(start, end)
    events, recaps = _tenant_event_recap_counts_window(tenant_id, window)
    kpis = _tenant_kpi_totals_window(tenant_id, window)
    breakdown = tenant_consumer_breakdown(tenant_id, start, end)

    facts: dict = {
        "brand": Tenant.objects.filter(id=tenant_id).values_list("name", flat=True).first()
        or "",
        "week": _week_label(start, end),
        "events_run": events,
        "approved_recaps": recaps,
        "consumers_sampled": int(breakdown["total"]),
    }
    for label, value in (
        ("samples_handed_out", kpis.samples_distributed),
        ("units_sold", kpis.products_sold),
        ("first_time_consumers", kpis.first_time_consumers),
    ):
        if value:
            facts[label] = int(value)

    conversion = tenant_conversion_kpis(tenant_id, start, end)
    if conversion.get("pct") is not None and conversion.get("sold"):
        facts["conversion"] = {
            "conversion_percent": conversion["pct"],
            "units_sold": conversion["sold"],
            "sampled_at_those_events": conversion["engagements"],
            "note": "Conversion counts retail and on-premise events only.",
        }

    stores = [r for r in breakdown["by_store"] if r["kind"] == KIND_NAMED][:MAX_STORES]
    if stores:
        facts["stores"] = [
            {"store": r["label"], "consumers_sampled": r["consumers"]} for r in stores
        ]
        facts["store_count"] = breakdown["store_count"]
    states = [r for r in breakdown["by_state"] if r["kind"] == KIND_NAMED]
    if states:
        facts["states"] = [
            {"state": r["label"], "consumers_sampled": r["consumers"]} for r in states
        ]

    prev_start, prev_end = start - timedelta(days=7), end - timedelta(days=7)
    prev_events, _prev_recaps = _tenant_event_recap_counts_window(
        tenant_id, _inclusive_dates_to_window(prev_start, prev_end)
    )
    if prev_events:
        facts["previous_week"] = {
            "week": _week_label(prev_start, prev_end),
            "events_run": prev_events,
            "consumers_sampled": int(
                tenant_consumer_breakdown(tenant_id, prev_start, prev_end)["total"]
            ),
        }

    comments, _cut = gather_comments(tenant_id, start, end)
    kinds = {KIND_DECLINE: "reason given for not buying", KIND_QUESTION: "question asked"}
    facts["consumer_comments"] = [
        {"type": kinds.get(c.kind, "comment"), "text": c.text}
        for c in comments[:MAX_COMMENTS]
    ]
    return facts


def _tidy(text: str) -> str:
    """Plain paragraphs: no markdown marks, one blank line between."""
    paragraphs = []
    for block in (text or "").replace("\r", "").split("\n\n"):
        line = " ".join(block.replace("**", "").replace("__", "").split())
        line = line.lstrip("#-*• ").strip()
        if line:
            paragraphs.append(line)
    return "\n\n".join(paragraphs)


def compose_summary(facts: dict) -> tuple[str | None, str | None]:
    """``(body, None)``, or ``(None, reason)`` when no draft can be kept."""
    if not int(facts.get("approved_recaps") or 0):
        return None, REASON_NO_RECAPS
    user = "FACTS:\n" + json.dumps(facts, indent=2, ensure_ascii=False)
    # One more try when a draft holds a number that is not in the facts;
    # after that it is not kept.
    for _attempt in range(2):
        try:
            text = generate_summary(_SYSTEM, user)
        except AiUnavailable:
            return None, REASON_UNAVAILABLE
        except Exception:  # noqa: BLE001
            logger.exception("weekly summary: model call failed")
            return None, REASON_UNAVAILABLE
        body = _tidy(text or "")
        if not body:
            return None, REASON_UNAVAILABLE
        if numbers_are_grounded(body, facts):
            return body, None
    return None, REASON_UNGROUNDED


def generate_summary_draft(tenant_id: int, start: date, end: date):
    """Write (or rewrite) the draft for one brand and week.

    Returns ``(row, None)`` or ``(None, reason)``. A rewrite replaces the
    draft, clears any edit and puts it back to "draft"; the caller asks
    first. When no draft can be written the stored row is left as it was.
    """
    from tenants.models import TenantWeeklySummary

    facts = week_facts(tenant_id, start, end)
    body, reason = compose_summary(facts)
    if body is None:
        return None, reason
    row, _created = TenantWeeklySummary.objects.update_or_create(
        tenant_id=tenant_id,
        week_start=start,
        defaults={
            "week_end": end,
            "body": body,
            "edited_body": "",
            "status": TenantWeeklySummary.STATUS_DRAFT,
            "facts": facts,
            "generated_at": timezone.now(),
            "reviewed_by": None,
            "reviewed_at": None,
        },
    )
    return row, None


def _recap_counts(window, *, approved: bool) -> dict[int, int]:
    """Recaps per tenant in the window. ``approved=False`` is the filed
    recaps still waiting for review, which no draft includes."""
    legacy = _filter_event_window(Recap.objects.all(), "event__", window)
    custom = _filter_event_window(CustomRecap.objects.all(), "event__", window)
    if approved:
        legacy, custom = _approved_only(legacy, ""), _approved_only(custom, "")
    else:
        legacy = legacy.filter(approved=False, archived_at__isnull=True).filter(
            legacy_filed_q()
        )
        custom = custom.filter(approved=False, archived_at__isnull=True).filter(
            custom_filed_q()
        )
    counts: dict[int, int] = {}
    for tenant_id, n in (
        legacy.order_by().values_list("event__tenant_id").annotate(n=Count("id"))
    ):
        counts[tenant_id] = counts.get(tenant_id, 0) + n
    for tenant_id, n in (
        custom.order_by().values_list("tenant_id").annotate(n=Count("id"))
    ):
        counts[tenant_id] = counts.get(tenant_id, 0) + n
    return counts


def week_rows(start: date, end: date) -> list[dict]:
    """One row per brand that ran something that week or has a draft."""
    from tenants.models import Tenant, TenantWeeklySummary

    window = _inclusive_dates_to_window(start, end)
    approved = _recap_counts(window, approved=True)
    pending = _recap_counts(window, approved=False)
    drafts = {
        row.tenant_id: row
        for row in TenantWeeklySummary.objects.filter(week_start=start).select_related(
            "reviewed_by"
        )
    }
    wanted = set(approved) | set(pending) | set(drafts)
    rows = []
    for tenant in Tenant.active().filter(id__in=wanted).order_by("name"):
        rows.append(
            {
                "tenant_id": tenant.id,
                "tenant_name": tenant.name or "",
                "approved_recaps": approved.get(tenant.id, 0),
                "pending_recaps": pending.get(tenant.id, 0),
                "summary": drafts.get(tenant.id),
            }
        )
    return rows


def generate_week_drafts(
    start: date, end: date, *, tenant_id: int | None = None, apply: bool = False
) -> dict:
    """The Monday job: a draft for every brand with approved recaps that
    week and no draft yet. An existing draft is never overwritten here, so
    staff edits are safe. Sends nothing."""
    from tenants.models import Tenant, TenantWeeklySummary

    window = _inclusive_dates_to_window(start, end)
    approved = _recap_counts(window, approved=True)
    existing = set(
        TenantWeeklySummary.objects.filter(week_start=start).values_list(
            "tenant_id", flat=True
        )
    )
    tenants = Tenant.active().filter(id__in=set(approved))
    if tenant_id is not None:
        tenants = tenants.filter(id=tenant_id)
    results = []
    for tenant in tenants.order_by("id"):
        entry = {"tenant_id": tenant.id, "tenant_name": tenant.name}
        if tenant.id in existing:
            entry["result"] = "exists"
        elif not apply:
            entry["result"] = "would_write"
        else:
            try:
                row, reason = generate_summary_draft(tenant.id, start, end)
            except Exception:  # noqa: BLE001
                logger.exception("weekly summary failed tenant=%s", tenant.id)
                row, reason = None, REASON_UNAVAILABLE
            entry["result"] = "written" if row is not None else "not_written"
            if reason:
                entry["reason"] = reason
        results.append(entry)
    return {
        "week_start": start.isoformat(),
        "week_end": end.isoformat(),
        "mode": "apply" if apply else "dry_run",
        "written": sum(1 for r in results if r["result"] == "written"),
        "emails_sent": 0,
        "tenants": results,
    }


# ─── GraphQL (Ignite staff only) ────────────────────────────────────


@strawberry.type
class WeeklyClientSummary:
    """One brand's stored draft. ``text`` is the edit when there is one,
    otherwise the draft as written."""

    id: strawberry.ID
    week_start: str
    week_end: str
    body: str
    edited_body: str
    text: str
    status: str
    generated_at: str
    reviewed_by_name: str | None
    reviewed_at: str | None


@strawberry.type
class WeeklyClientSummaryRow:
    tenant_id: strawberry.ID
    tenant_name: str
    approved_recaps: int
    # Filed that week and still waiting for review: not in the draft.
    pending_recaps: int
    summary: WeeklyClientSummary | None


@strawberry.type
class WeeklyClientSummaryWeek:
    week_start: str
    week_end: str
    rows: list[WeeklyClientSummaryRow]


@strawberry.type
class WeeklyClientSummaryResult:
    ok: bool
    reason: str | None
    summary: WeeklyClientSummary | None


def _person(user) -> str | None:
    if user is None:
        return None
    name = " ".join(
        part for part in (getattr(user, "first_name", ""), getattr(user, "last_name", "")) if part
    ).strip()
    return name or (getattr(user, "email", None) or None)


def _summary_type(row) -> WeeklyClientSummary:
    return WeeklyClientSummary(
        id=strawberry.ID(f"{_ID_PREFIX}{row.id}"),
        week_start=row.week_start.isoformat(),
        week_end=row.week_end.isoformat(),
        body=row.body,
        edited_body=row.edited_body,
        text=row.edited_body.strip() or row.body,
        status=row.status,
        generated_at=row.generated_at.isoformat(),
        reviewed_by_name=_person(row.reviewed_by),
        reviewed_at=row.reviewed_at.isoformat() if row.reviewed_at else None,
    )


@strawberry.type
class TenantWeeklySummary:
    """The summary shown on a brand's dashboard. ``is_draft`` is only ever
    true for Ignite staff: a brand user is never given an unreviewed one."""

    week_start: str
    week_end: str
    text: str
    is_draft: bool
    reviewed_at: str | None


def latest_summary(tenant_id: int, *, include_drafts: bool):
    """The newest stored summary for a brand: reviewed ones only, unless
    ``include_drafts`` (Ignite staff)."""
    from tenants.models import TenantWeeklySummary as Row

    rows = Row.objects.filter(tenant_id=tenant_id)
    if not include_drafts:
        rows = rows.filter(status=Row.STATUS_REVIEWED)
    return rows.order_by("-week_start").first()


def _fail(reason: str) -> WeeklyClientSummaryResult:
    return WeeklyClientSummaryResult(ok=False, reason=reason, summary=None)


async def _staff_user(info: strawberry.Info):
    """The caller when they are Ignite staff, else None. Decided from the
    database (``events.staffing_board._accessible_tenants``), never from the
    role on the request user."""
    from events.staffing_board import _accessible_tenants
    from utils.graphql.mixins import SparkGraphQLMixin

    user = await SparkGraphQLMixin().get_user(info)
    is_admin, _allowed = await _accessible_tenants(user)
    return user if is_admin else None


# The web client keys its cache on ``id`` across every type, so a bare
# row number could collide with another type's. The prefix keeps it apart.
_ID_PREFIX = "WeeklyClientSummary:"


def _row_id(raw) -> int | None:
    text = str(raw or "").strip().removeprefix(_ID_PREFIX)
    return int(text) if text.isdigit() else None


@strawberry.type
class WeeklySummaryQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def tenant_weekly_summary(
        self, info: strawberry.Info, tenant_id: strawberry.ID
    ) -> TenantWeeklySummary | None:
        """The latest weekly summary for a brand's dashboard, or null.

        A brand user reads their own tenant whatever id is passed, and only
        a summary staff have marked reviewed. Ignite staff may pass any
        tenant and also get the latest unreviewed draft (``isDraft``).
        Reads a stored row; never calls the model.
        """
        from recaps.report_types import _CampaignReportService

        service = _CampaignReportService()
        try:
            target = await service.resolve_target_tenant_id(info, tenant_id)
            staff = await service.is_staff_caller(info)
        except Exception:  # noqa: BLE001
            return None
        if target is None:
            return None
        try:
            row = await sync_to_async(latest_summary, thread_sensitive=True)(
                target, include_drafts=staff
            )
        except Exception:  # noqa: BLE001
            logger.exception("tenantWeeklySummary failed tenant=%s", target)
            return None
        if row is None:
            return None
        return TenantWeeklySummary(
            week_start=row.week_start.isoformat(),
            week_end=row.week_end.isoformat(),
            text=row.edited_body.strip() or row.body,
            is_draft=row.status != "reviewed",
            reviewed_at=row.reviewed_at.isoformat() if row.reviewed_at else None,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def weekly_client_summaries(
        self, info: strawberry.Info, week_start: str | None = None
    ) -> WeeklyClientSummaryWeek | None:
        """Every brand's draft for one week. Ignite staff only: anyone else
        gets null. Reads stored rows; never calls the model."""
        if await _staff_user(info) is None:
            return None
        bounds = week_bounds(week_start) if week_start else _ops_week()
        if bounds is None:
            return None
        start, end = bounds

        def _build():
            return [
                WeeklyClientSummaryRow(
                    tenant_id=strawberry.ID(str(r["tenant_id"])),
                    tenant_name=r["tenant_name"],
                    approved_recaps=r["approved_recaps"],
                    pending_recaps=r["pending_recaps"],
                    summary=_summary_type(r["summary"]) if r["summary"] else None,
                )
                for r in week_rows(start, end)
            ]

        try:
            rows = await sync_to_async(_build, thread_sensitive=True)()
        except Exception:  # noqa: BLE001
            logger.exception("weeklyClientSummaries failed")
            return None
        return WeeklyClientSummaryWeek(
            week_start=start.isoformat(), week_end=end.isoformat(), rows=rows
        )


@strawberry.type
class WeeklySummaryMutations:
    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def generate_weekly_client_summary(
        self, info: strawberry.Info, tenant_id: strawberry.ID, week_start: str
    ) -> WeeklyClientSummaryResult:
        """Write the draft for one brand and week, replacing any stored one
        (and its edit). Staff only. Sends nothing."""
        if await _staff_user(info) is None:
            return _fail("Not available.")
        bounds = week_bounds(week_start)
        if bounds is None:
            return _fail("Pick a week.")
        try:
            tid = resolve_id_to_int(str(tenant_id).strip())
        except Exception:  # noqa: BLE001
            return _fail("Pick a brand.")
        start, end = bounds

        def _build():
            from tenants.models import Tenant

            if not Tenant.objects.filter(id=tid).exists():
                return None, "Pick a brand."
            row, reason = generate_summary_draft(tid, start, end)
            return (_summary_type(row) if row is not None else None), reason

        try:
            summary, reason = await sync_to_async(_build, thread_sensitive=True)()
        except Exception:  # noqa: BLE001
            logger.exception("generateWeeklyClientSummary failed tenant=%s", tid)
            return _fail(REASON_UNAVAILABLE)
        if summary is None:
            return _fail(reason or REASON_UNAVAILABLE)
        return WeeklyClientSummaryResult(ok=True, reason=None, summary=summary)

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def save_weekly_client_summary(
        self, info: strawberry.Info, id: strawberry.ID, edited_body: str
    ) -> WeeklyClientSummaryResult:
        """Keep staff's wording. The generated draft is left as it was; an
        edit equal to the draft is stored as no edit. Staff only."""
        if await _staff_user(info) is None:
            return _fail("Not available.")
        rid = _row_id(id)
        if rid is None:
            return _fail(REASON_NOT_FOUND)
        text = (edited_body or "").strip()[:MAX_EDIT_CHARS]

        def _save():
            from tenants.models import TenantWeeklySummary

            row = (
                TenantWeeklySummary.objects.select_related("reviewed_by")
                .filter(id=rid)
                .first()
            )
            if row is None:
                return None
            row.edited_body = "" if text == row.body.strip() else text
            row.save(update_fields=["edited_body", "updated_at"])
            return _summary_type(row)

        summary = await sync_to_async(_save, thread_sensitive=True)()
        if summary is None:
            return _fail(REASON_NOT_FOUND)
        return WeeklyClientSummaryResult(ok=True, reason=None, summary=summary)

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def set_weekly_client_summary_reviewed(
        self, info: strawberry.Info, id: strawberry.ID, reviewed: bool
    ) -> WeeklyClientSummaryResult:
        """Mark a draft reviewed (recording who and when) or put it back to
        draft. Staff only. Marking it reviewed sends nothing."""
        user = await _staff_user(info)
        if user is None:
            return _fail("Not available.")
        rid = _row_id(id)
        if rid is None:
            return _fail(REASON_NOT_FOUND)

        def _save():
            from tenants.models import TenantWeeklySummary

            row = TenantWeeklySummary.objects.filter(id=rid).first()
            if row is None:
                return None
            if reviewed:
                row.status = TenantWeeklySummary.STATUS_REVIEWED
                row.reviewed_by_id = user.pk
                row.reviewed_at = timezone.now()
            else:
                row.status = TenantWeeklySummary.STATUS_DRAFT
                row.reviewed_by = None
                row.reviewed_at = None
            row.save(
                update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"]
            )
            row = (
                TenantWeeklySummary.objects.select_related("reviewed_by")
                .filter(id=rid)
                .first()
            )
            return _summary_type(row)

        summary = await sync_to_async(_save, thread_sensitive=True)()
        if summary is None:
            return _fail(REASON_NOT_FOUND)
        return WeeklyClientSummaryResult(ok=True, reason=None, summary=summary)
