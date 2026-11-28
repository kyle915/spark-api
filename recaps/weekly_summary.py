"""A written summary of a brand's week, drafted for Ignite staff.

Each brand gets a few short paragraphs about the last finished week,
Monday to Sunday by event date: what ran, the headline numbers, what
consumers said, and anything the brand should look at. Staff read the
draft, edit it, mark it reviewed, and can email the branded HTML to brand
recipients via ``send_weekly_client_summary`` (stamps ``sent_at`` /
``sent_by`` / ``sent_to``).

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
import re
from datetime import date, timedelta

import strawberry
from asgiref.sync import sync_to_async
from django.db.models import Count
from django.utils import timezone

from recaps.consumer_breakdown import (
    KIND_NAMED,
    KIND_VENUE,
    LABELED_KINDS,
    tenant_consumer_breakdown,
)
from recaps.feedback_themes import (
    KIND_DECLINE,
    KIND_QUESTION,
    STATUS_READY,
    gather_comments,
    get_feedback_themes,
)
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
REASON_NO_RECIPIENTS = "Add at least one recipient email."
REASON_BAD_EMAIL = "One of the recipient emails does not look valid."
REASON_TOO_MANY = "Send to up to 20 recipients at a time."
REASON_EMPTY_BODY = "Write or save the summary before sending."
REASON_SEND_FAILED = "Could not send the summary email. Try again."
MAX_SEND_RECIPIENTS = 20
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

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


PERIOD_WEEK = "week"
PERIOD_MONTH = "month"
PERIODS = (PERIOD_WEEK, PERIOD_MONTH)

_SYSTEM_MONTH = (
    "You draft a short monthly email from Ignite to a brand about its "
    "in-store sampling program. Ignite staff will check it before anyone "
    "sees it, so write to the brand in plain, short sentences. Use ONLY "
    "what is in FACTS. Copy every number exactly as given, with the label "
    "it has; never add, subtract, average, round or estimate, and never "
    "write a number that is not in FACTS. Start with the line 'Hi team,' "
    "then three or four short paragraphs separated by a blank line, in this "
    "order: what ran in the month (events and where), the headline numbers, "
    "what consumers said (use feedback_themes and their recap counts when "
    "present), and then anything the brand should look at, such as reasons "
    "people gave for not buying, questions people asked, or a change from "
    "the previous month. Leave a paragraph out when FACTS has nothing for "
    "it. Do not name any brand ambassador or shopper. No sign-off, no "
    "subject line, no headings, no bullet points, no markdown, no "
    "exclamation marks, no sales language. Stay under 220 words."
)


def month_bounds(raw: str | None, today: date | None = None) -> tuple[date, date] | None:
    """First to last day of the month holding ``raw`` (YYYY-MM-DD), or of
    the last finished month when ``raw`` is empty. None when unreadable."""
    if raw:
        try:
            first = date.fromisoformat(raw).replace(day=1)
        except ValueError:
            return None
    else:
        if today is None:
            from events.live_board import _ops_today

            today = _ops_today()
        first = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    following = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first, following - timedelta(days=1)


def period_bounds(
    period: str, raw: str | None, today: date | None = None
) -> tuple[date, date] | None:
    """The window for a period: a Monday-to-Sunday week or a calendar month."""
    if period == PERIOD_MONTH:
        return month_bounds(raw, today)
    if period != PERIOD_WEEK:
        return None
    if raw:
        return week_bounds(raw)
    return _ops_week(today)


def _previous_period(period: str, start: date, end: date) -> tuple[date, date]:
    if period == PERIOD_MONTH:
        prev_end = start - timedelta(days=1)
        return prev_end.replace(day=1), prev_end
    return start - timedelta(days=7), end - timedelta(days=7)


def _period_label(period: str, start: date, end: date) -> str:
    if period == PERIOD_MONTH:
        return start.strftime("%B %Y")
    return _week_label(start, end)


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


def week_facts(
    tenant_id: int, start: date, end: date, period: str = PERIOD_WEEK
) -> dict:
    """Everything the draft may say, from approved recaps of that week or
    month. A month also carries the feedback themes for its dates."""
    from tenants.models import Tenant

    window = _inclusive_dates_to_window(start, end)
    events, recaps = _tenant_event_recap_counts_window(tenant_id, window)
    kpis = _tenant_kpi_totals_window(tenant_id, window)
    breakdown = tenant_consumer_breakdown(tenant_id, start, end)

    facts: dict = {
        "brand": Tenant.objects.filter(id=tenant_id).values_list("name", flat=True).first()
        or "",
        period: _period_label(period, start, end),
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

    stores = [r for r in breakdown["by_store"] if r["kind"] in LABELED_KINDS][:MAX_STORES]
    if stores:
        facts["stores"] = [
            {
                "store": r["label"],
                "consumers_sampled": r["consumers"],
                # A festival / activation venue, not a store.
                **({"event_venue": True} if r["kind"] == KIND_VENUE else {}),
            }
            for r in stores
        ]
        facts["store_count"] = breakdown["store_count"]
    states = [r for r in breakdown["by_state"] if r["kind"] == KIND_NAMED]
    if states:
        facts["states"] = [
            {"state": r["label"], "consumers_sampled": r["consumers"]} for r in states
        ]

    prev_start, prev_end = _previous_period(period, start, end)
    prev_events, _prev_recaps = _tenant_event_recap_counts_window(
        tenant_id, _inclusive_dates_to_window(prev_start, prev_end)
    )
    if prev_events:
        facts[f"previous_{period}"] = {
            period: _period_label(period, prev_start, prev_end),
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
    if period == PERIOD_MONTH:
        # The same stored themes the report shows for these dates; the
        # counts are the ones computed there, not the model's.
        themes = get_feedback_themes(tenant_id, start, end)
        if themes["status"] == STATUS_READY:
            sections = {
                "liked": "what people liked",
                "passed": "why people passed",
                "asked": "questions people asked",
            }
            facts["feedback_themes"] = [
                {
                    "section": sections.get(t["section"], t["section"]),
                    "theme": t["label"],
                    "recaps_mentioning_it": t["recap_count"],
                }
                for t in themes["themes"]
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


def compose_summary(
    facts: dict, period: str = PERIOD_WEEK
) -> tuple[str | None, str | None]:
    """``(body, None)``, or ``(None, reason)`` when no draft can be kept."""
    if not int(facts.get("approved_recaps") or 0):
        return None, REASON_NO_RECAPS
    system = _SYSTEM_MONTH if period == PERIOD_MONTH else _SYSTEM
    user = "FACTS:\n" + json.dumps(facts, indent=2, ensure_ascii=False)
    # One more try when a draft holds a number that is not in the facts;
    # after that it is not kept.
    for _attempt in range(2):
        try:
            text = generate_summary(system, user)
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


def generate_summary_draft(
    tenant_id: int, start: date, end: date, period: str = PERIOD_WEEK
):
    """Write (or rewrite) the summary for one brand and week.

    Returns ``(row, None)`` or ``(None, reason)``. A rewrite replaces the
    text and clears any edit; the caller asks first. When nothing can be
    written the stored row is left as it was.

    A WEEKLY summary is published to the brand as soon as it is written
    (status "reviewed", no reviewer): every number in it is checked against
    the approved recaps before it is kept, and staff can still edit it or
    put it back to draft to hide it. A MONTHLY one is an email Ignite sends,
    so it stays a draft until staff review it.
    """
    from tenants.models import TenantWeeklySummary

    facts = week_facts(tenant_id, start, end, period)
    body, reason = compose_summary(facts, period)
    if body is None:
        return None, reason
    now = timezone.now()
    auto_publish = period == PERIOD_WEEK
    row, _created = TenantWeeklySummary.objects.update_or_create(
        tenant_id=tenant_id,
        period=period,
        week_start=start,
        defaults={
            "week_end": end,
            "body": body,
            "edited_body": "",
            "status": (
                TenantWeeklySummary.STATUS_REVIEWED
                if auto_publish
                else TenantWeeklySummary.STATUS_DRAFT
            ),
            "facts": facts,
            "generated_at": now,
            "reviewed_by": None,
            "reviewed_at": now if auto_publish else None,
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


def week_rows(start: date, end: date, period: str = PERIOD_WEEK) -> list[dict]:
    """One row per brand that ran something that week or has a draft."""
    from tenants.models import Tenant, TenantWeeklySummary

    window = _inclusive_dates_to_window(start, end)
    approved = _recap_counts(window, approved=True)
    pending = _recap_counts(window, approved=False)
    drafts = {
        row.tenant_id: row
        for row in TenantWeeklySummary.objects.filter(
            period=period, week_start=start
        ).select_related("reviewed_by", "sent_by")
    }
    wanted = set(approved) | set(pending) | set(drafts)
    tenants = list(Tenant.active().filter(id__in=wanted).order_by("name"))
    # One TenantedUser query for the whole page — not N per brand.
    suggested_by_tenant = _suggested_recipients_for_tenants(tenants)
    rows = []
    for tenant in tenants:
        rows.append(
            {
                "tenant_id": tenant.id,
                "tenant_name": tenant.name or "",
                "approved_recaps": approved.get(tenant.id, 0),
                "pending_recaps": pending.get(tenant.id, 0),
                "suggested_recipients": suggested_by_tenant.get(tenant.id, []),
                "summary": drafts.get(tenant.id),
            }
        )
    return rows


def generate_week_drafts(
    start: date,
    end: date,
    *,
    tenant_id: int | None = None,
    apply: bool = False,
    period: str = PERIOD_WEEK,
) -> dict:
    """The Monday job: a draft for every brand with approved recaps that
    week and no draft yet. An existing draft is never overwritten here, so
    staff edits are safe. Sends nothing."""
    from tenants.models import Tenant, TenantWeeklySummary

    window = _inclusive_dates_to_window(start, end)
    approved = _recap_counts(window, approved=True)
    existing = set(
        TenantWeeklySummary.objects.filter(period=period, week_start=start).values_list(
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
                row, reason = generate_summary_draft(tenant.id, start, end, period)
            except Exception:  # noqa: BLE001
                logger.exception("weekly summary failed tenant=%s", tenant.id)
                row, reason = None, REASON_UNAVAILABLE
            entry["result"] = "written" if row is not None else "not_written"
            if reason:
                entry["reason"] = reason
        results.append(entry)
    return {
        "period": period,
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
    period: str
    week_start: str
    week_end: str
    body: str
    edited_body: str
    text: str
    status: str
    generated_at: str
    reviewed_by_name: str | None
    reviewed_at: str | None
    sent_at: str | None
    sent_by_name: str | None
    sent_to: list[str]


@strawberry.type
class WeeklyClientSummaryRow:
    tenant_id: strawberry.ID
    tenant_name: str
    approved_recaps: int
    # Filed that week and still waiting for review: not in the draft.
    pending_recaps: int
    # Brand contacts staff usually email (recap recipients + client users).
    suggested_recipients: list[str]
    summary: WeeklyClientSummary | None


@strawberry.type
class WeeklyClientSummaryWeek:
    # "week" or "month". For a month, week_start / week_end are its first
    # and last day.
    period: str
    week_start: str
    week_end: str
    rows: list[WeeklyClientSummaryRow]


@strawberry.type
class WeeklyClientSummaryResult:
    ok: bool
    reason: str | None
    summary: WeeklyClientSummary | None
    sent_count: int | None = None


def _person(user) -> str | None:
    if user is None:
        return None
    name = " ".join(
        part for part in (getattr(user, "first_name", ""), getattr(user, "last_name", "")) if part
    ).strip()
    return name or (getattr(user, "email", None) or None)


def _sent_to_list(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(e).strip() for e in raw if str(e).strip()]
    return []


def _summary_type(row) -> WeeklyClientSummary:
    return WeeklyClientSummary(
        id=strawberry.ID(f"{_ID_PREFIX}{row.id}"),
        period=row.period,
        week_start=row.week_start.isoformat(),
        week_end=row.week_end.isoformat(),
        body=row.body,
        edited_body=row.edited_body,
        text=row.edited_body.strip() or row.body,
        status=row.status,
        generated_at=row.generated_at.isoformat(),
        reviewed_by_name=_person(row.reviewed_by),
        reviewed_at=row.reviewed_at.isoformat() if row.reviewed_at else None,
        sent_at=row.sent_at.isoformat() if getattr(row, "sent_at", None) else None,
        sent_by_name=_person(getattr(row, "sent_by", None)),
        sent_to=_sent_to_list(getattr(row, "sent_to", None)),
    )


def _clean_recipients(raw: list[str] | None) -> tuple[list[str] | None, str | None]:
    """Deduped emails, or ``(None, reason)`` when the list is unusable."""
    clean: list[str] = []
    seen: set[str] = set()
    for item in list(raw or []):
        email = (item or "").strip()
        if not email:
            continue
        if not _EMAIL_RE.match(email):
            return None, REASON_BAD_EMAIL
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        clean.append(email)
    if not clean:
        return None, REASON_NO_RECIPIENTS
    if len(clean) > MAX_SEND_RECIPIENTS:
        return None, REASON_TOO_MANY
    return clean, None


def _suggested_recipients(tenant) -> list[str]:
    """Brand contacts for the Send modal prefill (single-tenant path)."""
    return _suggested_recipients_for_tenants([tenant]).get(
        getattr(tenant, "id", None), []
    )


def _suggested_recipients_for_tenants(tenants: list) -> dict[int, list[str]]:
    """Prefill emails per tenant: recap recipients + client-role users.

    Batches the ``TenantedUser`` lookup so ``week_rows`` does one query for
    the whole page instead of one per brand (same membership set as
    :func:`recaps.weekly_digest.client_digest_recipients`).
    """
    from tenants.models import Role, TenantedUser

    out: dict[int, list[str]] = {}
    seen: dict[int, set[str]] = {}
    if not tenants:
        return out

    def _push(tenant_id: int, email: str | None) -> None:
        e = (email or "").strip()
        if not e:
            return
        key = e.lower()
        bucket = seen.setdefault(tenant_id, set())
        if key in bucket:
            return
        bucket.add(key)
        out.setdefault(tenant_id, []).append(e)

    try:
        for tenant in tenants:
            tid = getattr(tenant, "id", None)
            if tid is None:
                continue
            for e in tenant.scheduled_report_recipients():
                _push(tid, e)
        tenant_ids = [t.id for t in tenants if getattr(t, "id", None) is not None]
        if tenant_ids:
            rows = (
                TenantedUser.objects.filter(
                    tenant_id__in=tenant_ids,
                    is_active=True,
                    user__role__slug=Role.CLIENT_SLUG,
                )
                .exclude(user__email__in=["system@spark.local", "admin@spark.local"])
                .values_list("tenant_id", "user__email")
            )
            for tid, email in rows:
                _push(tid, email)
    except Exception:  # noqa: BLE001
        logger.exception("weekly summary: suggested recipients batch failed")
        return {t.id: [] for t in tenants if getattr(t, "id", None) is not None}
    return out


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

    rows = Row.objects.filter(tenant_id=tenant_id, period=PERIOD_WEEK)
    if not include_drafts:
        rows = rows.filter(status=Row.STATUS_REVIEWED)
    return rows.order_by("-week_start").first()


def recent_summaries(tenant_id: int, *, include_drafts: bool, limit: int = 12) -> list:
    """Newest stored weekly summaries for a brand: reviewed ones only unless
    ``include_drafts`` (Ignite staff)."""
    from tenants.models import TenantWeeklySummary as Row

    cap = max(1, min(int(limit or 12), 26))
    rows = Row.objects.filter(tenant_id=tenant_id, period=PERIOD_WEEK)
    if not include_drafts:
        rows = rows.filter(status=Row.STATUS_REVIEWED)
    return list(rows.order_by("-week_start")[:cap])


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
    async def tenant_weekly_summaries(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        limit: int = 12,
    ) -> list[TenantWeeklySummary]:
        """Recent weekly summaries (newest first) for archive stepping on the
        dashboard. Brand users only ever receive reviewed rows for their own
        tenant; Ignite staff also receive drafts."""
        from recaps.report_types import _CampaignReportService

        service = _CampaignReportService()
        try:
            target = await service.resolve_target_tenant_id(info, tenant_id)
            staff = await service.is_staff_caller(info)
        except Exception:  # noqa: BLE001
            return []
        if target is None:
            return []
        try:
            rows = await sync_to_async(recent_summaries, thread_sensitive=True)(
                target, include_drafts=staff, limit=limit
            )
        except Exception:  # noqa: BLE001
            logger.exception("tenantWeeklySummaries failed tenant=%s", target)
            return []
        return [
            TenantWeeklySummary(
                week_start=r.week_start.isoformat(),
                week_end=r.week_end.isoformat(),
                text=r.edited_body.strip() or r.body,
                is_draft=r.status != "reviewed",
                reviewed_at=r.reviewed_at.isoformat() if r.reviewed_at else None,
            )
            for r in rows
        ]

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def weekly_client_summaries(
        self,
        info: strawberry.Info,
        week_start: str | None = None,
        period: str = PERIOD_WEEK,
    ) -> WeeklyClientSummaryWeek | None:
        """Every brand's draft for one week, or with ``period: "month"`` one
        month (``weekStart`` is then any day in it). Ignite staff only:
        anyone else gets null. Reads stored rows; never calls the model."""
        if await _staff_user(info) is None:
            return None
        bounds = period_bounds(period, week_start)
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
                    suggested_recipients=list(r.get("suggested_recipients") or []),
                    summary=_summary_type(r["summary"]) if r["summary"] else None,
                )
                for r in week_rows(start, end, period)
            ]

        try:
            rows = await sync_to_async(_build, thread_sensitive=True)()
        except Exception:  # noqa: BLE001
            logger.exception("weeklyClientSummaries failed")
            return None
        return WeeklyClientSummaryWeek(
            period=period,
            week_start=start.isoformat(), week_end=end.isoformat(), rows=rows
        )


@strawberry.type
class WeeklySummaryMutations:
    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def generate_weekly_client_summary(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        week_start: str,
        period: str = PERIOD_WEEK,
    ) -> WeeklyClientSummaryResult:
        """Write the draft for one brand and week, replacing any stored one
        (and its edit). Staff only. Sends nothing."""
        if await _staff_user(info) is None:
            return _fail("Not available.")
        bounds = period_bounds(period, week_start) if week_start else None
        if bounds is None:
            return _fail("Pick a week or a month.")
        try:
            tid = resolve_id_to_int(str(tenant_id).strip())
        except Exception:  # noqa: BLE001
            return _fail("Pick a brand.")
        start, end = bounds

        def _build():
            from tenants.models import Tenant

            if not Tenant.objects.filter(id=tid).exists():
                return None, "Pick a brand."
            row, reason = generate_summary_draft(tid, start, end, period)
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
                TenantWeeklySummary.objects.select_related("reviewed_by", "sent_by")
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
                TenantWeeklySummary.objects.select_related("reviewed_by", "sent_by")
                .filter(id=rid)
                .first()
            )
            return _summary_type(row)

        summary = await sync_to_async(_save, thread_sensitive=True)()
        if summary is None:
            return _fail(REASON_NOT_FOUND)
        return WeeklyClientSummaryResult(ok=True, reason=None, summary=summary)

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def send_weekly_client_summary(
        self,
        info: strawberry.Info,
        id: strawberry.ID,
        recipients: list[str],
        message: str | None = None,
    ) -> WeeklyClientSummaryResult:
        """Email the branded summary HTML to brand recipients. Staff only.

        Stamps ``sent_at`` / ``sent_by`` / ``sent_to`` on a successful send
        and marks the row reviewed so the brand dashboard also shows it.
        Re-sends are allowed (audit fields update to the latest send).
        """
        user = await _staff_user(info)
        if user is None:
            return _fail("Not available.")
        rid = _row_id(id)
        if rid is None:
            return _fail(REASON_NOT_FOUND)
        clean, bad = _clean_recipients(recipients)
        if clean is None:
            return _fail(bad or REASON_NO_RECIPIENTS)
        note = (message or "").strip()[:1000] or None

        def _send():
            from events.event_confirmations import public_page_base
            from recaps.envelopes import WeeklyClientSummaryMailer
            from tenants.models import TenantWeeklySummary

            row = (
                TenantWeeklySummary.objects.select_related(
                    "tenant", "reviewed_by", "sent_by"
                )
                .filter(id=rid)
                .first()
            )
            if row is None:
                return None, REASON_NOT_FOUND, 0
            text = (row.edited_body.strip() or row.body or "").strip()
            if not text:
                return None, REASON_EMPTY_BODY, 0

            sender_name = _person(user) or getattr(user, "email", "") or "Ignite"
            period_label = _period_label(row.period, row.week_start, row.week_end)
            base = public_page_base()
            dashboard_url = f"{base}/" if base else None

            delivered: list[str] = []
            for recipient in clean:
                mailer = WeeklyClientSummaryMailer(
                    recipients=[recipient],
                    tenant_name=getattr(row.tenant, "name", "") or "Your brand",
                    period=row.period,
                    period_label=period_label,
                    body=text,
                    sender_name=sender_name,
                    message=note,
                    dashboard_url=dashboard_url,
                )
                try:
                    ok = mailer.send()
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "send_weekly_client_summary failed to=%s id=%s",
                        recipient,
                        rid,
                    )
                    continue
                # False = provider rejected; None = nothing to send (skip).
                if ok is False:
                    logger.error(
                        "send_weekly_client_summary provider rejected to=%s id=%s",
                        recipient,
                        rid,
                    )
                    continue
                if ok is None:
                    continue
                delivered.append(recipient)
            if not delivered:
                return None, REASON_SEND_FAILED, 0

            now = timezone.now()
            row.sent_at = now
            row.sent_by_id = user.pk
            # Audit only addresses that actually got the mail.
            row.sent_to = list(delivered)
            if row.status != TenantWeeklySummary.STATUS_REVIEWED:
                row.status = TenantWeeklySummary.STATUS_REVIEWED
                row.reviewed_by_id = user.pk
                row.reviewed_at = now
            row.save(
                update_fields=[
                    "sent_at",
                    "sent_by",
                    "sent_to",
                    "status",
                    "reviewed_by",
                    "reviewed_at",
                    "updated_at",
                ]
            )
            row = (
                TenantWeeklySummary.objects.select_related("reviewed_by", "sent_by")
                .filter(id=rid)
                .first()
            )
            return _summary_type(row), None, len(delivered)

        try:
            summary, reason, sent_count = await sync_to_async(
                _send, thread_sensitive=True
            )()
        except Exception:  # noqa: BLE001
            logger.exception("sendWeeklyClientSummary failed id=%s", rid)
            return _fail(REASON_SEND_FAILED)
        if summary is None:
            return _fail(reason or REASON_SEND_FAILED)
        return WeeklyClientSummaryResult(
            ok=True, reason=None, summary=summary, sent_count=sent_count
        )
