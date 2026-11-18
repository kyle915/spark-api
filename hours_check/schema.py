"""GraphQL for the hours check (clients endpoint — the web app's).

Ignite staff only (``IsSparkAdminAccess``): the flags carry BA hours, pay
context and GPS-derived distances, which brand users never see. Scoped to
one client per call, like the payroll timesheet it sits beside.
"""

from __future__ import annotations

from datetime import date as _date

import strawberry
from asgiref.sync import sync_to_async
from strawberry import relay

from events.staffing_board import _accessible_tenants
from utils.graphql.mixins import resolve_id_to_int
from utils.graphql.permissions import IsSparkAdminAccess


@strawberry.type
class HoursFlagFact:
    label: str
    value: str


@strawberry.type
class HoursCheckFlag:
    code: str
    title: str
    severity: str
    summary: str
    action: str
    facts: list[HoursFlagFact]
    uses_location: bool = False
    # One plain-English sentence from OpenAI, only when `explain: true` was
    # asked and the service is configured. Never decides anything.
    ai_reason: str | None = None
    reviewed: bool = False
    stale_review: bool = False
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    review_note: str | None = None


@strawberry.type
class HoursCheckShift:
    event_uuid: str
    event_name: str
    ambassador_uuid: str
    ambassador_name: str
    date: str | None
    needs_look: bool
    flags: list[HoursCheckFlag]


@strawberry.type
class HoursRuleCount:
    code: str
    title: str
    severity: str
    count: int
    unreviewed: int


@strawberry.type
class HoursThreshold:
    key: str
    value: float


@strawberry.type
class HoursCheckReport:
    start_date: str
    end_date: str
    total_shifts: int
    flagged_shifts: int
    needs_look_shifts: int
    by_rule: list[HoursRuleCount]
    thresholds: list[HoursThreshold]
    shifts: list[HoursCheckShift]


def _resolve_tid(tenant_id) -> int | None:
    try:
        return resolve_id_to_int(tenant_id)
    except Exception:  # noqa: BLE001
        return None


@strawberry.type
class HoursCheckQueries:
    @strawberry.field(permission_classes=[IsSparkAdminAccess])
    async def payroll_hours_check(
        self,
        info: strawberry.Info,
        start_date: str,
        end_date: str,
        tenant_id: strawberry.ID,
        explain: bool = False,
    ) -> HoursCheckReport:
        """Rule-based flags on padded or impossible shifts for one client's
        pay period (same shifts the timesheet pays). Advisory only."""
        user = info.context.request.user
        is_admin, allowed = await _accessible_tenants(user)
        tid = _resolve_tid(tenant_id)

        def _go() -> HoursCheckReport:
            from hours_check.rules import RULE_TITLES
            from hours_check.service import run_check

            try:
                start = _date.fromisoformat(str(start_date))
                end = _date.fromisoformat(str(end_date))
            except (TypeError, ValueError):
                start = end = None
            empty = HoursCheckReport(
                start_date=str(start_date), end_date=str(end_date),
                total_shifts=0, flagged_shifts=0, needs_look_shifts=0,
                by_rule=[], thresholds=[], shifts=[],
            )
            if start is None or tid is None:
                return empty
            if not is_admin and tid not in (allowed or set()):
                return empty

            report = run_check(tid, start, end)
            reasons: dict[int, str] = {}
            if explain:
                from hours_check.explain import explain as _explain

                pending = [
                    cf.flag for s in report.shifts for cf in s.flags if not cf.reviewed
                ]
                reasons = _explain(pending)

            shifts = [
                HoursCheckShift(
                    event_uuid=s.facts.event_uuid,
                    event_name=s.facts.event_name,
                    ambassador_uuid=s.facts.ambassador_uuid,
                    ambassador_name=s.facts.ambassador_name,
                    date=s.facts.local_date.isoformat() if s.facts.local_date else None,
                    needs_look=s.needs_look,
                    flags=[
                        HoursCheckFlag(
                            code=cf.flag.code,
                            title=cf.flag.title,
                            severity=cf.flag.severity,
                            summary=cf.flag.summary,
                            action=cf.flag.action,
                            facts=[HoursFlagFact(label=k, value=v) for k, v in cf.flag.facts],
                            uses_location=cf.flag.uses_location,
                            ai_reason=reasons.get(id(cf.flag)),
                            reviewed=cf.reviewed,
                            stale_review=cf.stale_review,
                            reviewed_by=cf.reviewed_by,
                            reviewed_at=cf.reviewed_at,
                            review_note=cf.review_note or None,
                        )
                        for cf in s.flags
                    ],
                )
                for s in report.shifts
            ]
            return HoursCheckReport(
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                total_shifts=report.total_shifts,
                flagged_shifts=report.flagged_shifts,
                needs_look_shifts=report.needs_look_shifts,
                by_rule=[
                    HoursRuleCount(
                        code=code, title=RULE_TITLES[code], severity=sev,
                        count=n, unreviewed=u,
                    )
                    for code, sev, n, u in report.by_rule()
                ],
                thresholds=[
                    HoursThreshold(key=k, value=v) for k, v in report.thresholds.items()
                ],
                shifts=shifts,
            )

        return await sync_to_async(_go)()


@strawberry.input
class MarkHoursFlagReviewedInput:
    tenant_id: strawberry.ID
    event_uuid: str
    ambassador_uuid: str
    code: str
    note: str | None = None
    # True removes the "reviewed" mark so the flag asks again.
    undo: bool = False
    client_mutation_id: strawberry.ID | None = None


@strawberry.type
class HoursFlagReviewResponse:
    success: bool
    message: str
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    client_mutation_id: strawberry.ID | None = None


@strawberry.type
class HoursCheckMutations:
    @relay.mutation(permission_classes=[IsSparkAdminAccess])
    async def mark_hours_flag_reviewed(
        self, info: strawberry.Info, input: MarkHoursFlagReviewedInput
    ) -> HoursFlagReviewResponse:
        """Mark one flag on one shift "OK — reviewed" (records who + when).
        It stays quiet until the facts behind it change."""
        user = info.context.request.user
        is_admin, allowed = await _accessible_tenants(user)
        tid = _resolve_tid(input.tenant_id)

        def _go():
            from ambassadors.models import Ambassador, AmbassadorEvent
            from events.models import Event
            from hours_check.models import HoursFlagReview
            from hours_check.rules import RULE_TITLES
            from hours_check.service import _user_label, current_flag

            if tid is None:
                return False, "Bad client.", None, None
            if not is_admin and tid not in (allowed or set()):
                return False, "Not authorized for this client.", None, None
            if input.code not in RULE_TITLES:
                return False, "Unknown check.", None, None
            event = Event.objects.filter(uuid=str(input.event_uuid)).first()
            amb = Ambassador.objects.filter(uuid=str(input.ambassador_uuid)).first()
            if event is None or amb is None:
                return False, "Shift not found.", None, None
            if event.tenant_id != tid:
                return False, "That shift belongs to another client.", None, None
            if not AmbassadorEvent.objects.filter(
                event=event, ambassador=amb, is_approved=True
            ).exists():
                return False, "That BA is not booked on this shift.", None, None

            if input.undo:
                HoursFlagReview.objects.filter(
                    event=event, ambassador=amb, code=input.code
                ).delete()
                return True, "Review removed.", None, None

            flag = current_flag(tid, event, amb.id, input.code)
            if flag is None:
                return False, "That flag no longer applies to this shift.", None, None
            row, _ = HoursFlagReview.objects.update_or_create(
                event=event, ambassador=amb, code=input.code,
                defaults=dict(
                    tenant_id=tid,
                    fingerprint=flag.fingerprint,
                    note=(input.note or "").strip()[:2000],
                    reviewed_by=user if getattr(user, "id", None) else None,
                ),
            )
            return (
                True, "Marked reviewed.",
                _user_label(row.reviewed_by),
                row.reviewed_at.isoformat() if row.reviewed_at else None,
            )

        ok, msg, by, at = await sync_to_async(_go)()
        return HoursFlagReviewResponse(
            success=ok, message=msg, reviewed_by=by, reviewed_at=at,
            client_mutation_id=input.client_mutation_id,
        )
