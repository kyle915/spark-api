"""
Regression coverage for the client/public EXPRESS create-request gap that
made `Event.objects.from_request(...)` hard-fail.

Root cause (incident: a client-created Vons / Liquid Death request, id=1051):
    The client self-serve / public EXPRESS create-request form produces a
    "lighter" Request than the admin path — no retailer / location / state /
    distributor / event_type, and (for the public form) NO ``created_by`` (no
    authenticated user filed it). ``Request.created_by`` is nullable; but
    ``Event.created_by`` is NOT NULL. ``from_request`` copied ``created_by``
    straight across, so an express request with ``created_by=None`` raised

        IntegrityError: null value in column "created_by_id"
                        of relation "events_event" violates not-null constraint

    and the request stayed approved-but-EVENTLESS — invisible to the Missing
    Recaps query and the recap event picker (both iterate Event rows), so no
    recap could ever be filed. The admin "Log event" + approve_request paths
    always have an authenticated user, so they never hit this — which is why
    their tests passed while the express path silently broke and the bulk
    repair command failed on exactly this request.

These tests build a Request the way the express path does (minimal fields,
``created_by=None``) and assert ``from_request`` AND
``_materialize_approved_event_for_request`` now create an approved Event
WITHOUT raising, and that the Event carries what Missing Recaps + the picker
need (tenant set, end_time set/derived). They would have FAILED before the
fix (IntegrityError on the create). Also covers the error-surfacing change in
the repair command.
"""

from __future__ import annotations

from datetime import datetime, timezone as _tz
from io import StringIO

import pytest
from asgiref.sync import sync_to_async
from django.core.management import call_command

from events import models as event_models
from events.mutations import _materialize_approved_event_for_request
from events.tests.base import EventsGraphQLTestCase


def _aware(y, m, d, hh=10, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=_tz.utc)


@pytest.mark.django_db(transaction=True)
class TestExpressEventMaterialization(EventsGraphQLTestCase):
    """The express/client path through the shared manager + helper."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(
            name="Liquid Death", slug="liquid-death"
        )
        # The tenant has the usual approved/pending sets (pending is default).
        self.req_approved = self.create_request_status(
            name="Approved", tenant=self.tenant, slug="approved", create_event=True
        )
        self.ev_pending = self.create_event_status(
            name="Pending", tenant=self.tenant, slug="pending", is_default=True
        )
        self.ev_approved = self.create_event_status(
            name="Approved", tenant=self.tenant, slug="approved"
        )
        self.request_type = self.create_request_type(
            name="Sampling", tenant=self.tenant
        )

    # ─── Express request factory (mirrors the FE express shape) ──────

    def _make_express_request(self, **overrides):
        """A Request as the client/public EXPRESS form writes it: address +
        scheduling only, NO retailer/location/state/distributor/event_type,
        and (public form) NO created_by. Approved (client paths auto-approve).
        """
        kwargs = dict(
            name="Vons",
            address="1608 Broadway St",
            tenant=self.tenant,
            status=self.req_approved,
            request_type=self.request_type,
            start_time=_aware(2026, 5, 29, 12),
            end_time=_aware(2026, 5, 29, 16),
            date=_aware(2026, 5, 29, 12),
            # The public express form has no authenticated user → null creator.
            # This is the exact field that used to make from_request raise.
            created_by=None,
        )
        kwargs.update(overrides)
        return event_models.Request.objects.create(**kwargs)

    # ─── from_request ────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_from_request_materializes_event_for_creatorless_express_request(
        self,
    ):
        """from_request must NOT raise on an express request whose
        created_by is None — and must produce an approved Event with the
        fields Missing Recaps + the recap picker need (tenant + end_time)."""
        request = await sync_to_async(self._make_express_request)()

        # Pre-fix this raised IntegrityError (created_by NOT NULL).
        event = await event_models.Event.objects.from_request(request=request)

        assert event is not None and event.id is not None
        # Recap picker keys on tenant.
        assert event.tenant_id == self.tenant.id
        # Missing Recaps keys on end_time — must be set (copied here).
        assert event.end_time == _aware(2026, 5, 29, 16)
        # created_by was backfilled to a non-null user (NOT the null we passed).
        assert event.created_by_id is not None
        # from_request copies request.date into event.date (#718) so the recap
        # "Event Date" is populated at create time — the forward fix the
        # read-side fallback + backfill complement.
        assert event.date == _aware(2026, 5, 29, 12)  # == request.date
        # Approved request → approved Event status (agrees with the tracker).
        status = await sync_to_async(lambda: event.status)()
        assert status.slug == "approved"

    @pytest.mark.asyncio
    async def test_from_request_copies_request_date_into_event_date(self):
        """Explicit, focused coverage for the #718 forward fix: from_request
        sets event.date = request.date so newly-materialized events never have
        the null-date condition this PR's backfill repairs."""
        request = await sync_to_async(self._make_express_request)(
            date=_aware(2026, 7, 4, 9),
            start_time=_aware(2026, 7, 4, 9),
            end_time=_aware(2026, 7, 4, 13),
        )
        event = await event_models.Event.objects.from_request(request=request)
        assert event.date == _aware(2026, 7, 4, 9)

    @pytest.mark.asyncio
    async def test_from_request_derives_end_time_when_express_request_lacks_it(
        self,
    ):
        """If the express request only carried a start_time, end_time is
        derived (falls back to start) so the Event still has a non-null end
        and stays visible on Missing Recaps."""
        request = await sync_to_async(self._make_express_request)(
            end_time=None
        )
        event = await event_models.Event.objects.from_request(request=request)
        assert event.end_time == _aware(2026, 5, 29, 12)  # == start_time

    # ─── _materialize_approved_event_for_request (the shared helper) ─

    @pytest.mark.asyncio
    async def test_materialize_helper_succeeds_for_creatorless_express_request(
        self,
    ):
        """The shared escape hatch every auto-approve path calls must
        materialize the Event (not silently swallow) for a creatorless
        express request, and it must be approved + recap-visible."""
        request = await sync_to_async(self._make_express_request)()

        event = await _materialize_approved_event_for_request(request, None)

        assert event is not None and event.id is not None
        assert event.tenant_id == self.tenant.id
        assert event.end_time is not None
        status = await sync_to_async(lambda: event.status)()
        assert status.slug == "approved"

    @pytest.mark.asyncio
    async def test_materialize_helper_is_idempotent(self):
        """Second call returns the existing Event, creates no duplicate."""
        request = await sync_to_async(self._make_express_request)()
        first = await _materialize_approved_event_for_request(request, None)
        second = await _materialize_approved_event_for_request(request, None)
        assert first.id == second.id
        count = await sync_to_async(
            lambda: event_models.Event.objects.filter(
                request_id=request.id
            ).count()
        )()
        assert count == 1


@pytest.mark.django_db(transaction=True)
class TestRepairMissingEventsSurfacesAndFixesExpress(EventsGraphQLTestCase):
    """The bulk repair command on a creatorless express request: it now
    CREATES the Event (post-fix) and, on any genuine failure, surfaces the
    real exception (type + message + frame) in its report."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(
            name="Liquid Death", slug="liquid-death"
        )
        self.req_approved = self.create_request_status(
            name="Approved", tenant=self.tenant, slug="approved", create_event=True
        )
        self.ev_pending = self.create_event_status(
            name="Pending", tenant=self.tenant, slug="pending", is_default=True
        )
        self.ev_approved = self.create_event_status(
            name="Approved", tenant=self.tenant, slug="approved"
        )
        self.request_type = self.create_request_type(
            name="Sampling", tenant=self.tenant
        )

    def _make_express_request(self, **overrides):
        kwargs = dict(
            name="Vons",
            address="1608 Broadway St",
            tenant=self.tenant,
            status=self.req_approved,
            request_type=self.request_type,
            start_time=_aware(2026, 5, 29, 12),
            end_time=_aware(2026, 5, 29, 16),
            date=_aware(2026, 5, 29, 12),
            created_by=None,
        )
        kwargs.update(overrides)
        return event_models.Request.objects.create(**kwargs)

    def test_repair_creates_event_for_creatorless_express_request(self):
        """--execute on a creatorless approved express request now creates the
        approved Event (this is the Vons repair the operator's run failed on)."""
        request = self._make_express_request()
        out = StringIO()
        call_command(
            "repair_missing_events_for_approved_requests",
            execute=True,
            tenant="liquid-death",
            stdout=out,
        )
        report = out.getvalue()
        assert "Created: 1 event(s)" in report
        assert "Failed to create: 0" not in report  # there's no such line
        assert "Failed to create" not in report
        event = event_models.Event.objects.get(request_id=request.id)
        assert event.status.slug == "approved"
        assert event.end_time is not None
        assert event.created_by_id is not None

    def test_repair_report_surfaces_exception_detail_on_failure(self, monkeypatch):
        """If a per-request create genuinely fails, the report must carry the
        exception TYPE + MESSAGE (not the old opaque 'see log') so a re-run
        pinpoints it without Cloud Run access."""
        self._make_express_request()

        # Force from_request to blow up with a recognisable error.
        async def _boom(*args, **kwargs):
            raise ValueError("synthetic boom for test")

        monkeypatch.setattr(
            event_models.Event.objects, "from_request", _boom
        )

        out = StringIO()
        call_command(
            "repair_missing_events_for_approved_requests",
            execute=True,
            tenant="liquid-death",
            stdout=out,
        )
        report = out.getvalue()
        # Exception type + message surfaced (not the opaque "see log").
        assert "ValueError" in report
        assert "synthetic boom for test" in report
        assert "see log" not in report
        # And the failure is counted.
        assert "Failed to create: 1 event(s)" in report
