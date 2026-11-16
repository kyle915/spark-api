"""GraphQL for the dashboard sentences: what changed, callouts, a question
about one chart, and where to sample next.

Each is its own query so none of them can slow or break the dashboard's
main query, and each returns nothing (None / an empty list) on any error
rather than raising.

Who sees what: tenant scoping is the one ``tenantKpis`` and Ask AI use
(:class:`recaps.report_types._CampaignReportService`): a brand user is
held to their own tenant whatever id they pass; Ignite staff may pick one.
Staff are decided by ``events.staffing_board._accessible_tenants``, read
from the database.
"""

from __future__ import annotations

import logging

import strawberry
from asgiref.sync import sync_to_async

from utils.graphql.permissions import StrictIsAuthenticated

logger = logging.getLogger(__name__)


@strawberry.type
class WhatChanged:
    """One or two sentences comparing a window with the one before it.
    ``text`` is empty when there is too little data to compare."""

    text: str
    # "model", "template", or "none" when there is nothing to say.
    source: str
    previous_start_date: str | None = None
    previous_end_date: str | None = None


@strawberry.type
class CalloutNumber:
    label: str
    value: str


@strawberry.type
class CalloutLink:
    """Filters for the recaps list that show the recaps behind a callout."""

    store: str | None
    state: str | None
    start_date: str | None
    end_date: str | None


@strawberry.type
class DashboardCallout:
    key: str
    # store_above_average | store_below_average | state_quiet |
    # state_waiting_review (staff only) | product_no_sales | week_drop
    kind: str
    text: str
    numbers: list[CalloutNumber]
    link: CalloutLink


@strawberry.type
class ChartAnswer:
    ok: bool
    # False when ``answer`` is the "this chart cannot answer that" line or
    # the chart's plain summary rather than an answer to the question.
    answered: bool
    answer: str
    reason: str | None = None


@strawberry.type
class SampleNextItem:
    key: str
    # never_visited | revisit
    kind: str
    name: str
    address: str
    market: str
    reason: str
    lat: float | None
    lng: float | None


@strawberry.type
class SampleNext:
    items: list[SampleNextItem]


async def _scope(info: strawberry.Info, tenant_id) -> tuple[int | None, bool]:
    """(the tenant to read, whether the caller is Ignite staff)."""
    from recaps.report_types import _CampaignReportService

    service = _CampaignReportService()
    try:
        target = await service.resolve_target_tenant_id(info, tenant_id)
        staff = await service.is_staff_caller(info)
    except Exception:  # noqa: BLE001 — out of scope reads as "nothing"
        return None, False
    return target, bool(staff)


def _today():
    from events.live_board import _ops_today

    return _ops_today()


@strawberry.type
class DashboardExplainQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def tenant_what_changed(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> WhatChanged | None:
        """What moved against the window before, and what accounts for it.
        Approved, non-archived recaps, windowed by event date."""
        from recaps.report_types import _iso_date

        target, _staff = await _scope(info, tenant_id)
        if target is None:
            return None
        start, end = _iso_date(start_date), _iso_date(end_date)

        def _build():
            from recaps import explain_text
            from recaps.what_changed import compute_what_changed, draft_what_changed

            facts = compute_what_changed(target, start, end)
            if facts is None:
                return WhatChanged(text="", source="none")
            draft = draft_what_changed(facts)
            written = explain_text.write(
                target,
                "what_changed",
                explain_text.window_key(start, end),
                [draft],
            )[0]
            return WhatChanged(
                text=written.text,
                source=written.source,
                previous_start_date=facts["previous_start"].isoformat(),
                previous_end_date=facts["previous_end"].isoformat(),
            )

        try:
            return await sync_to_async(_build, thread_sensitive=True)()
        except Exception as exc:  # noqa: BLE001
            logger.exception("tenantWhatChanged failed tenant=%s: %s", target, exc)
            return None

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def tenant_callouts(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DashboardCallout]:
        """Up to four things that stand out, found by rule, most significant
        first, from approved recaps. Recaps still waiting for review can
        hold a callout back (a state in review is not quiet) for everyone;
        only Ignite staff are told how many are waiting."""
        from recaps.report_types import _iso_date

        target, staff = await _scope(info, tenant_id)
        if target is None:
            return []
        start, end = _iso_date(start_date), _iso_date(end_date)

        def _build():
            from recaps import explain_text
            from recaps.callouts import compute_callouts, draft_callouts

            found = compute_callouts(target, start, end, _today(), staff=staff)
            written = {
                w.key: w.text
                for w in explain_text.write(
                    target,
                    # Staff can get a different list (their own callouts can
                    # take a place), so the two are cached apart.
                    "callouts_staff" if staff else "callouts",
                    explain_text.window_key(start, end),
                    draft_callouts(found),
                )
            }

            def _iso(day):
                return day.isoformat() if day else None

            return [
                DashboardCallout(
                    key=c["key"],
                    kind=c["kind"],
                    text=written.get(c["key"], c["template"]),
                    numbers=[CalloutNumber(**num) for num in c["numbers"]],
                    link=CalloutLink(
                        store=c["link"]["store"],
                        state=c["link"]["state"],
                        start_date=_iso(c["link"]["start"]),
                        end_date=_iso(c["link"]["end"]),
                    ),
                )
                for c in found
            ]

        try:
            return await sync_to_async(_build, thread_sensitive=True)()
        except Exception as exc:  # noqa: BLE001
            logger.exception("tenantCallouts failed tenant=%s: %s", target, exc)
            return []

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def chart_answer(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        chart: str,
        question: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> ChartAnswer:
        """Answer ``question`` from one chart's own figures. ``chart`` names
        the chart (consumers_by_week, consumers_by_store, consumers_by_state,
        samples_by_product); the figures are worked out again here. Fired on
        submit, never on page load. Limited per person per hour."""
        from recaps.report_types import _iso_date

        target, _staff = await _scope(info, tenant_id)
        if target is None:
            return ChartAnswer(
                ok=False, answered=False, answer="", reason="A valid tenant id is required."
            )
        start, end = _iso_date(start_date), _iso_date(end_date)
        user = info.context.request.user
        user_id = getattr(user, "id", None)

        def _build():
            from recaps.chart_questions import answer_chart_question

            return answer_chart_question(
                target, chart, start, end, question, user_id=user_id
            )

        try:
            result = await sync_to_async(_build, thread_sensitive=True)()
        except Exception as exc:  # noqa: BLE001
            logger.exception("chartAnswer failed tenant=%s: %s", target, exc)
            return ChartAnswer(
                ok=False,
                answered=False,
                answer="",
                reason="The question could not be answered right now.",
            )
        return ChartAnswer(**result)

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def tenant_sample_next(
        self, info: strawberry.Info, tenant_id: strawberry.ID
    ) -> SampleNext | None:
        """Stores to sample next, ranked, each with its reason. Ignite staff
        only: anyone else gets None until ``SAMPLE_NEXT_FOR_CLIENTS`` is
        turned on."""
        from recaps.sample_next import clients_may_see

        target, staff = await _scope(info, tenant_id)
        if target is None:
            return None
        if not staff and not clients_may_see():
            return None

        def _build():
            from recaps import explain_text
            from recaps.sample_next import compute_sample_next, draft_sample_next

            items = compute_sample_next(target, _today())
            written = {
                w.key: w.text
                for w in explain_text.write(
                    target, "sample_next", "all", draft_sample_next(items)
                )
            }
            return SampleNext(
                items=[
                    SampleNextItem(
                        key=i["key"],
                        kind=i["kind"],
                        name=i["name"],
                        address=i["address"],
                        market=i["market"],
                        reason=written.get(i["key"], i["template"]),
                        lat=i["lat"],
                        lng=i["lng"],
                    )
                    for i in items
                ]
            )

        try:
            return await sync_to_async(_build, thread_sensitive=True)()
        except Exception as exc:  # noqa: BLE001
            logger.exception("tenantSampleNext failed tenant=%s: %s", target, exc)
            return None
