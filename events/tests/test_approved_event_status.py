"""
Coverage for the "internal event lands as Pending instead of Approved" bug
and its backfill.

The bug: the admin "Log event" flow (`create_request`, admin branch) and
`approve_request` materialized an Event via `Event.objects.from_request(...)`
WITHOUT passing a status, so the Event defaulted to the tenant's default
EventStatus ("pending") even though the parent Request was set to "approved".
The Master Tracker (Request.status) showed Approved while the Event detail
page (Event.status) showed Pending.

These tests cover three layers:
  1. `Event.objects.from_request` — the manager now defaults to the tenant's
     APPROVED EventStatus when the parent request is approved/scheduled (and
     honors an explicit status), instead of always falling to "pending".
  2. The two real mutations (`approveRequest`, `createRequest` admin branch)
     through the spark GraphQL schema — the Event they materialize is now
     "approved", not "pending".
  3. The `repair_approved_event_status` backfill command — flips
     pending→approved only for approved/scheduled-request events, leaves
     others alone, --dry-run changes nothing, idempotent, and is
     tenant-scopable.
"""

from __future__ import annotations

from datetime import datetime, timezone as _tz
from io import StringIO

import pytest
from asgiref.sync import sync_to_async
from django.core.management import call_command

from events import models as event_models
from events.tests.base import EventsGraphQLTestCase
from recaps import models as recap_models


def _aware(y, m, d, hh=10, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=_tz.utc)


@pytest.mark.django_db(transaction=True)
class TestFromRequestApprovedStatus(EventsGraphQLTestCase):
    """Unit-level coverage of the manager fix — the shared code path both
    call sites rely on."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Manager Tenant", slug="mgr-tenant")
        # Statuses: pending is the tenant DEFAULT; approved + scheduled exist.
        self.req_pending = self.create_request_status(
            name="Pending", tenant=self.tenant, slug="pending", is_default=True
        )
        self.req_approved = self.create_request_status(
            name="Approved", tenant=self.tenant, slug="approved", create_event=True
        )
        self.req_scheduled = self.create_request_status(
            name="Scheduled", tenant=self.tenant, slug="scheduled"
        )
        self.ev_pending = self.create_event_status(
            name="Pending", tenant=self.tenant, slug="pending", is_default=True
        )
        self.ev_approved = self.create_event_status(
            name="Approved", tenant=self.tenant, slug="approved"
        )
        self.ev_scheduled = self.create_event_status(
            name="Scheduled", tenant=self.tenant, slug="scheduled"
        )
        self.event_type = self.create_event_type(name="In Store", tenant=self.tenant)
        self.request_type = self.create_request_type(
            name="Sampling", tenant=self.tenant
        )
        self.client = self.create_client(
            name="Client", email="c@test.com", tenant=self.tenant
        )

    def _make_request(self, status):
        return event_models.Request.objects.create(
            name="Req",
            address="123 Main St",
            tenant=self.tenant,
            status=status,
            request_type=self.request_type,
            start_time=_aware(2026, 6, 1, 10),
            end_time=_aware(2026, 6, 1, 14),
            created_by=self.system_user,
        )

    @pytest.mark.asyncio
    async def test_approved_request_no_status_defaults_to_approved_event_status(self):
        request = await sync_to_async(self._make_request)(self.req_approved)
        event = await event_models.Event.objects.from_request(
            request=request, created_by=self.system_user
        )
        status = await sync_to_async(lambda: event.status)()
        assert status.slug == "approved", (
            "Event off an APPROVED request must default to the approved "
            "EventStatus, not the tenant default 'pending'."
        )

    @pytest.mark.asyncio
    async def test_scheduled_request_no_status_defaults_to_approved_event_status(self):
        request = await sync_to_async(self._make_request)(self.req_scheduled)
        event = await event_models.Event.objects.from_request(
            request=request, created_by=self.system_user
        )
        status = await sync_to_async(lambda: event.status)()
        assert status.slug == "approved"

    @pytest.mark.asyncio
    async def test_pending_request_no_status_keeps_default_pending(self):
        """Non-approved/normal flow unchanged: still falls to the tenant
        default EventStatus ('pending')."""
        request = await sync_to_async(self._make_request)(self.req_pending)
        event = await event_models.Event.objects.from_request(
            request=request, created_by=self.system_user
        )
        status = await sync_to_async(lambda: event.status)()
        assert status.slug == "pending"

    @pytest.mark.asyncio
    async def test_explicit_status_is_honored(self):
        """An explicit status arg always wins (over the approved default)."""
        request = await sync_to_async(self._make_request)(self.req_approved)
        event = await event_models.Event.objects.from_request(
            request=request,
            created_by=self.system_user,
            status=self.ev_scheduled,
        )
        status = await sync_to_async(lambda: event.status)()
        assert status.slug == "scheduled"

    @pytest.mark.asyncio
    async def test_approved_request_falls_back_to_default_when_no_approved_event_status(
        self,
    ):
        """If the tenant has NO approved EventStatus, an approved request's
        event falls through to the default (still materializes — never
        hard-fails)."""
        # Fresh tenant with only a default pending EventStatus.
        tenant2 = await sync_to_async(self.create_tenant)(
            name="No Approved EV", slug="no-approved-ev"
        )
        req_approved2 = await sync_to_async(self.create_request_status)(
            name="Approved", tenant=tenant2, slug="approved"
        )
        await sync_to_async(self.create_event_status)(
            name="Pending", tenant=tenant2, slug="pending", is_default=True
        )
        await sync_to_async(self.create_event_type)(name="In Store", tenant=tenant2)
        request_type2 = await sync_to_async(self.create_request_type)(
            name="Sampling", tenant=tenant2
        )
        request = await sync_to_async(
            lambda: event_models.Request.objects.create(
                name="Req2",
                address="1 A St",
                tenant=tenant2,
                status=req_approved2,
                request_type=request_type2,
                created_by=self.system_user,
            )
        )()
        event = await event_models.Event.objects.from_request(
            request=request, created_by=self.system_user
        )
        status = await sync_to_async(lambda: event.status)()
        assert status.slug == "pending"


@pytest.mark.django_db(transaction=True)
class TestRequestMutationsApprovedEventStatus(EventsGraphQLTestCase):
    """The two fixed call sites, driven through the real spark schema."""

    APPROVE_REQUEST = """
    mutation ApproveRequest($id: ID!) {
      approveRequest(input: { id: $id }) {
        success
        message
        event { uuid status { slug name } }
      }
    }
    """

    CREATE_REQUEST = """
    mutation CreateRequest($input: CreateRequestInput!) {
      createRequest(input: $input) {
        success
        message
        request { uuid status { slug } }
      }
    }
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        from config.schema_spark import schema_spark

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql"

        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Mutations Tenant", slug="mut-tenant")

        self.admin = self.create_user(
            username="spark-admin",
            email="admin@test.com",
            role=self.roles["spark_admin"],
        )
        self.create_tenanted_user(user=self.admin, tenant=self.tenant)

        # A CLIENT user (role slug="client") with membership in the tenant. The
        # client self-serve create-request path auto-approves and must now
        # materialize an approved Event — the bug this PR fixes.
        self.client_user = self.create_user(
            username="client-user",
            email="client-user@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(user=self.client_user, tenant=self.tenant)

        self.req_pending = self.create_request_status(
            name="Pending", tenant=self.tenant, slug="pending", is_default=True
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
        self.event_type = self.create_event_type(name="In Store", tenant=self.tenant)
        self.request_type = self.create_request_type(name="Sampling", tenant=self.tenant)
        self.timezone = event_models.TimeZone.objects.create(
            name="UTC", code="UTC", offset=0, created_by=self.system_user
        )
        self.client = self.create_client(
            name="Client", email="c@test.com", tenant=self.tenant
        )

    @pytest.mark.asyncio
    async def test_approve_request_materializes_approved_event(self):
        request = await sync_to_async(
            lambda: event_models.Request.objects.create(
                name="Req-approve",
                address="123 Main St",
                tenant=self.tenant,
                status=self.req_pending,
                request_type=self.request_type,
                start_time=_aware(2026, 6, 2, 10),
                end_time=_aware(2026, 6, 2, 14),
                created_by=self.system_user,
            )
        )()

        result = await self._execute_mutation_authenticated(
            self.APPROVE_REQUEST,
            {"id": str(request.id)},
            user=self.admin,
        )
        assert result.errors is None, result.errors
        payload = result.data["approveRequest"]
        assert payload["success"] is True, payload["message"]
        assert payload["event"] is not None, "approve_request should return an Event"
        assert payload["event"]["status"]["slug"] == "approved", (
            "Event materialized by approve_request must be 'approved', not "
            "'pending'."
        )

        # And the DB row agrees.
        event = await sync_to_async(
            lambda: event_models.Event.objects.select_related("status").get(
                request_id=request.id
            )
        )()
        assert event.status.slug == "approved"

    @pytest.mark.asyncio
    async def test_create_request_admin_logevent_materializes_approved_event(self):
        variables = {
            "input": {
                "name": "Logged demo",
                "date": "2026-06-03T10:00:00+00:00",
                "startTime": "2026-06-03T10:00:00+00:00",
                "endTime": "2026-06-03T14:00:00+00:00",
                "address": "123 Main St",
                "coordinates": [0.0, 0.0],
                "timezoneId": str(self.timezone.id),
                "requestTypeId": str(self.request_type.id),
                "tenantId": str(self.tenant.id),
                "details": [],
                "products": [],
            }
        }
        result = await self._execute_mutation_authenticated(
            self.CREATE_REQUEST, variables, user=self.admin
        )
        assert result.errors is None, result.errors
        payload = result.data["createRequest"]
        assert payload["success"] is True, payload["message"]
        # Admin log-event flow auto-approves the request.
        assert payload["request"]["status"]["slug"] == "approved"

        # The materialized Event must be approved, not pending.
        request_uuid = payload["request"]["uuid"]
        event = await sync_to_async(
            lambda: event_models.Event.objects.select_related("status", "request")
            .filter(request__uuid=request_uuid)
            .first()
        )()
        assert event is not None, "Admin log-event should materialize an Event"
        assert event.status.slug == "approved", (
            "Event materialized by the admin log-event flow must be "
            "'approved', not 'pending'."
        )

    @pytest.mark.asyncio
    async def test_create_request_client_self_serve_materializes_approved_event(
        self,
    ):
        """REGRESSION: a CLIENT-created request (is_client=True) auto-approves
        and must now materialize an APPROVED Event — previously the is_client
        branch only sent emails and left the request approved-but-eventless,
        invisible to the Missing Recaps query and the recap event picker."""
        # No tenantId in the input: the client isn't a spark-schema user, so
        # the service resolves the tenant from the client's single membership,
        # and save() flips auto_approve on (role.is_client) → status approved.
        variables = {
            "input": {
                "name": "Client self-serve demo",
                "date": "2026-06-04T10:00:00+00:00",
                "startTime": "2026-06-04T10:00:00+00:00",
                "endTime": "2026-06-04T14:00:00+00:00",
                "address": "123 Main St",
                "coordinates": [0.0, 0.0],
                "timezoneId": str(self.timezone.id),
                "requestTypeId": str(self.request_type.id),
                "details": [],
                "products": [],
            }
        }
        result = await self._execute_mutation_authenticated(
            self.CREATE_REQUEST, variables, user=self.client_user
        )
        assert result.errors is None, result.errors
        payload = result.data["createRequest"]
        assert payload["success"] is True, payload["message"]
        # Client self-serve auto-approves the request.
        assert payload["request"]["status"]["slug"] == "approved"

        # The materialized Event must exist and be approved — this is the fix.
        request_uuid = payload["request"]["uuid"]
        event = await sync_to_async(
            lambda: event_models.Event.objects.select_related("status", "request")
            .filter(request__uuid=request_uuid)
            .first()
        )()
        assert event is not None, (
            "Client self-serve create-request must materialize an Event — "
            "an approved-but-eventless request can never receive a recap."
        )
        assert event.status.slug == "approved", (
            "Event materialized by the client self-serve flow must be "
            "'approved', not 'pending'."
        )


@pytest.mark.django_db(transaction=True)
class TestRepairApprovedEventStatusCommand(EventsGraphQLTestCase):
    """Coverage for the `repair_approved_event_status` backfill command."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.system_user = self.get_system_user()

        # ── Tenant A (generic) ──────────────────────────────────────
        self.tenant_a = self.create_tenant(name="Tenant A", slug="tenant-a")
        self.a_req_pending = self.create_request_status(
            name="Pending", tenant=self.tenant_a, slug="pending", is_default=True
        )
        self.a_req_approved = self.create_request_status(
            name="Approved", tenant=self.tenant_a, slug="approved"
        )
        self.a_req_scheduled = self.create_request_status(
            name="Scheduled", tenant=self.tenant_a, slug="scheduled"
        )
        self.a_ev_pending = self.create_event_status(
            name="Pending", tenant=self.tenant_a, slug="pending", is_default=True
        )
        self.a_ev_approved = self.create_event_status(
            name="Approved", tenant=self.tenant_a, slug="approved"
        )

        # ── Tenant B (Liquid Death) ─────────────────────────────────
        self.tenant_ld = self.create_tenant(name="Liquid Death", slug="liquid-death")
        self.ld_req_approved = self.create_request_status(
            name="Approved", tenant=self.tenant_ld, slug="approved"
        )
        self.ld_ev_pending = self.create_event_status(
            name="Pending", tenant=self.tenant_ld, slug="pending", is_default=True
        )
        self.ld_ev_approved = self.create_event_status(
            name="Approved", tenant=self.tenant_ld, slug="approved"
        )
        self.event_type_a = self.create_event_type(name="In Store", tenant=self.tenant_a)
        self.event_type_ld = self.create_event_type(
            name="In Store", tenant=self.tenant_ld
        )
        self.request_type_a = self.create_request_type(
            name="Sampling", tenant=self.tenant_a
        )
        self.request_type_ld = self.create_request_type(
            name="Sampling", tenant=self.tenant_ld
        )
        self._request_type_for = {
            self.tenant_a.id: self.request_type_a,
            self.tenant_ld.id: self.request_type_ld,
        }

    # ── helpers ─────────────────────────────────────────────────────

    def _request(self, tenant, status):
        return event_models.Request.objects.create(
            name="Req",
            address="123 Main St",
            tenant=tenant,
            status=status,
            request_type=self._request_type_for[tenant.id],
            created_by=self.system_user,
        )

    def _event(self, tenant, request, status):
        return event_models.Event.objects.create(
            name="Ev",
            tenant=tenant,
            address="123 Main St",
            request=request,
            status=status,
            created_by=self.system_user,
        )

    def _run(self, *args):
        out = StringIO()
        call_command("repair_approved_event_status", *args, stdout=out, stderr=out)
        return out.getvalue()

    # ── tests ───────────────────────────────────────────────────────

    def test_flips_pending_event_on_approved_request(self):
        req = self._request(self.tenant_a, self.a_req_approved)
        ev = self._event(self.tenant_a, req, self.a_ev_pending)

        self._run()

        ev.refresh_from_db()
        assert ev.status_id == self.a_ev_approved.id

    def test_flips_pending_event_on_scheduled_request(self):
        req = self._request(self.tenant_a, self.a_req_scheduled)
        ev = self._event(self.tenant_a, req, self.a_ev_pending)

        self._run()

        ev.refresh_from_db()
        assert ev.status_id == self.a_ev_approved.id

    def test_leaves_pending_event_on_pending_request_alone(self):
        req = self._request(self.tenant_a, self.a_req_pending)
        ev = self._event(self.tenant_a, req, self.a_ev_pending)

        self._run()

        ev.refresh_from_db()
        assert ev.status_id == self.a_ev_pending.id

    def test_leaves_already_approved_event_alone(self):
        req = self._request(self.tenant_a, self.a_req_approved)
        ev = self._event(self.tenant_a, req, self.a_ev_approved)

        self._run()

        ev.refresh_from_db()
        assert ev.status_id == self.a_ev_approved.id

    def test_dry_run_changes_nothing(self):
        req = self._request(self.tenant_a, self.a_req_approved)
        ev = self._event(self.tenant_a, req, self.a_ev_pending)

        output = self._run("--dry-run")

        ev.refresh_from_db()
        assert ev.status_id == self.a_ev_pending.id
        assert "DRY RUN" in output
        assert "Would update" in output

    def test_idempotent_second_run_is_zero_changes(self):
        req = self._request(self.tenant_a, self.a_req_approved)
        ev = self._event(self.tenant_a, req, self.a_ev_pending)

        self._run()  # first run flips it
        ev.refresh_from_db()
        assert ev.status_id == self.a_ev_approved.id

        output = self._run()  # second run: nothing left to do
        assert "Updated: 0 event(s)" in output
        ev.refresh_from_db()
        assert ev.status_id == self.a_ev_approved.id

    def test_tenant_scoping_only_touches_named_tenant(self):
        # Tenant A: a fixable event.
        req_a = self._request(self.tenant_a, self.a_req_approved)
        ev_a = self._event(self.tenant_a, req_a, self.a_ev_pending)
        # Liquid Death: a fixable event.
        req_ld = self._request(self.tenant_ld, self.ld_req_approved)
        ev_ld = self._event(self.tenant_ld, req_ld, self.ld_ev_pending)

        # Scope to tenant A only (by slug).
        self._run("--tenant", "tenant-a")

        ev_a.refresh_from_db()
        ev_ld.refresh_from_db()
        assert ev_a.status_id == self.a_ev_approved.id, "Tenant A should be fixed"
        assert ev_ld.status_id == self.ld_ev_pending.id, (
            "Liquid Death must be untouched when scoped to tenant A"
        )

    def test_tenant_scoping_by_numeric_id(self):
        req_a = self._request(self.tenant_a, self.a_req_approved)
        ev_a = self._event(self.tenant_a, req_a, self.a_ev_pending)

        self._run("--tenant", str(self.tenant_a.id))

        ev_a.refresh_from_db()
        assert ev_a.status_id == self.a_ev_approved.id

    def test_liquid_death_specifics_reported(self):
        """The LD report surfaces events-that-change, multi-event requests,
        and recaps on those multi-event requests."""
        # An approved LD request with TWO events: one pending (candidate),
        # one already approved — and a recap on each.
        req = self._request(self.tenant_ld, self.ld_req_approved)
        ev_pending = self._event(self.tenant_ld, req, self.ld_ev_pending)
        ev_approved = self._event(self.tenant_ld, req, self.ld_ev_approved)
        recap_models.Recap.objects.create(
            name="Recap on pending event",
            event=ev_pending,
            created_by=self.system_user,
        )
        recap_models.Recap.objects.create(
            name="Recap on approved event",
            event=ev_approved,
            created_by=self.system_user,
        )

        output = self._run("--tenant", "liquid-death", "--dry-run")

        assert "LIQUID DEATH" in output
        assert "Liquid Death specifics:" in output
        assert "affected requests with >1 Event:" in output
        assert "recaps on those multi-event requests:" in output
        # 1 candidate event, 1 affected request, that request has >1 event,
        # 2 recaps on it (one on a pending candidate).
        assert "affected requests with >1 Event:          1" in output
        assert "recaps on those multi-event requests:     2" in output

        # Dry-run must not have changed the pending event.
        ev_pending.refresh_from_db()
        assert ev_pending.status_id == self.ld_ev_pending.id


@pytest.mark.django_db(transaction=True)
class TestRepairMissingEventsForApprovedRequestsCommand(EventsGraphQLTestCase):
    """Coverage for the `repair_missing_events_for_approved_requests` backfill —
    CREATES the approved Event for approved/scheduled requests that have NO
    Event at all (the client auto-approve gap). Distinct from
    `repair_approved_event_status`, which flips an EXISTING pending event."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.system_user = self.get_system_user()

        # ── Tenant A (generic) ──────────────────────────────────────
        self.tenant_a = self.create_tenant(name="Tenant A", slug="tenant-a")
        self.a_req_pending = self.create_request_status(
            name="Pending", tenant=self.tenant_a, slug="pending", is_default=True
        )
        self.a_req_approved = self.create_request_status(
            name="Approved", tenant=self.tenant_a, slug="approved"
        )
        self.a_req_scheduled = self.create_request_status(
            name="Scheduled", tenant=self.tenant_a, slug="scheduled"
        )
        self.a_ev_pending = self.create_event_status(
            name="Pending", tenant=self.tenant_a, slug="pending", is_default=True
        )
        self.a_ev_approved = self.create_event_status(
            name="Approved", tenant=self.tenant_a, slug="approved"
        )

        # ── Tenant B (Liquid Death) ─────────────────────────────────
        self.tenant_ld = self.create_tenant(name="Liquid Death", slug="liquid-death")
        self.ld_req_approved = self.create_request_status(
            name="Approved", tenant=self.tenant_ld, slug="approved"
        )
        self.ld_ev_pending = self.create_event_status(
            name="Pending", tenant=self.tenant_ld, slug="pending", is_default=True
        )
        self.ld_ev_approved = self.create_event_status(
            name="Approved", tenant=self.tenant_ld, slug="approved"
        )
        # EventType is required by Event.objects.from_request (it resolves the
        # tenant default/first), so every tenant under test needs one.
        self.event_type_a = self.create_event_type(name="In Store", tenant=self.tenant_a)
        self.event_type_ld = self.create_event_type(
            name="In Store", tenant=self.tenant_ld
        )
        self.request_type_a = self.create_request_type(
            name="Sampling", tenant=self.tenant_a
        )
        self.request_type_ld = self.create_request_type(
            name="Sampling", tenant=self.tenant_ld
        )
        self._request_type_for = {
            self.tenant_a.id: self.request_type_a,
            self.tenant_ld.id: self.request_type_ld,
        }

    # ── helpers ─────────────────────────────────────────────────────

    def _request(self, tenant, status, **kwargs):
        """An EVENTLESS request (no Event created for it)."""
        return event_models.Request.objects.create(
            name="Req",
            address="123 Main St",
            tenant=tenant,
            status=status,
            request_type=self._request_type_for[tenant.id],
            start_time=_aware(2026, 5, 29, 10),
            end_time=_aware(2026, 5, 29, 14),
            created_by=self.system_user,
            **kwargs,
        )

    def _event_count_for(self, request):
        return event_models.Event.objects.filter(request_id=request.id).count()

    def _run(self, *args):
        out = StringIO()
        call_command(
            "repair_missing_events_for_approved_requests",
            *args,
            stdout=out,
            stderr=out,
        )
        return out.getvalue()

    # ── tests ───────────────────────────────────────────────────────

    def test_creates_approved_event_for_eventless_approved_request(self):
        req = self._request(self.tenant_a, self.a_req_approved)
        assert self._event_count_for(req) == 0

        self._run("--execute")

        event = event_models.Event.objects.select_related("status").get(
            request_id=req.id
        )
        assert event.status.slug == "approved", (
            "Repaired event must land approved (not the tenant default pending)."
        )

    def test_creates_event_for_eventless_scheduled_request(self):
        req = self._request(self.tenant_a, self.a_req_scheduled)

        self._run("--execute")

        event = event_models.Event.objects.select_related("status").get(
            request_id=req.id
        )
        assert event.status.slug == "approved"

    def test_creates_pending_job_for_repaired_request(self):
        """The repair also materializes the Pending Job (mirrors the resolver
        flow): an approved open-gig request lands a Job once it has an Event."""
        from jobs import models as job_models

        # create_pending_jobs_for_request needs a JobTitle + Rate for the tenant.
        self.create_job_title(name="Brand Ambassador", tenant=self.tenant_a)
        rate_type = self.create_rate_type(name="Hourly", tenant=self.tenant_a)
        self.create_rate(amount=25, rate_type=rate_type, tenant=self.tenant_a)
        req = self._request(self.tenant_a, self.a_req_approved)

        self._run("--execute")

        assert event_models.Event.objects.filter(request_id=req.id).exists()
        assert job_models.Job.objects.filter(
            event__request_id=req.id
        ).exists(), "Repair should create the Pending Job for the new Event."

    def test_leaves_request_that_already_has_an_event_alone(self):
        req = self._request(self.tenant_a, self.a_req_approved)
        # Pre-existing event (e.g. created by approve_request).
        existing = event_models.Event.objects.create(
            name="Existing",
            tenant=self.tenant_a,
            address="123 Main St",
            request=req,
            status=self.a_ev_approved,
            created_by=self.system_user,
        )

        self._run("--execute")

        # Still exactly one event, and it's the original (no duplicate created).
        events = list(event_models.Event.objects.filter(request_id=req.id))
        assert len(events) == 1
        assert events[0].id == existing.id

    def test_leaves_pending_request_alone(self):
        req = self._request(self.tenant_a, self.a_req_pending)

        self._run("--execute")

        assert self._event_count_for(req) == 0, (
            "A pending (not approved/scheduled) request must not get an Event."
        )

    def test_skips_soft_deleted_request(self):
        from django.utils import timezone as dj_tz

        req = self._request(
            self.tenant_a, self.a_req_approved, deleted_at=dj_tz.now()
        )

        self._run("--execute")

        assert self._event_count_for(req) == 0, (
            "Soft-deleted requests must be skipped."
        )

    def test_dry_run_default_writes_nothing(self):
        """No flags → DRY RUN by default (must not write)."""
        req = self._request(self.tenant_a, self.a_req_approved)

        output = self._run()  # no --execute → dry run

        assert self._event_count_for(req) == 0, "Default run must not write."
        assert "DRY RUN" in output
        assert "Would create" in output

    def test_explicit_dry_run_writes_nothing(self):
        req = self._request(self.tenant_a, self.a_req_approved)

        output = self._run("--dry-run")

        assert self._event_count_for(req) == 0
        assert "DRY RUN" in output

    def test_idempotent_second_run_is_zero_changes(self):
        req = self._request(self.tenant_a, self.a_req_approved)

        self._run("--execute")  # first run creates the event
        assert self._event_count_for(req) == 1

        output = self._run("--execute")  # second run: nothing left to do
        assert "Created: 0 event(s)" in output
        assert self._event_count_for(req) == 1, (
            "Second run must not create a duplicate event."
        )

    def test_tenant_scoping_only_touches_named_tenant(self):
        req_a = self._request(self.tenant_a, self.a_req_approved)
        req_ld = self._request(self.tenant_ld, self.ld_req_approved)

        # Scope to tenant A only (by slug).
        self._run("--tenant", "tenant-a", "--execute")

        assert self._event_count_for(req_a) == 1, "Tenant A should be repaired"
        assert self._event_count_for(req_ld) == 0, (
            "Liquid Death must be untouched when scoped to tenant A"
        )

    def test_tenant_scoping_by_numeric_id(self):
        req_a = self._request(self.tenant_a, self.a_req_approved)

        self._run("--tenant", str(self.tenant_a.id), "--execute")

        assert self._event_count_for(req_a) == 1

    def test_liquid_death_specifics_reported(self):
        """The LD report surfaces the eventless approved/scheduled request
        count under a LIQUID DEATH header."""
        self._request(self.tenant_ld, self.ld_req_approved)

        output = self._run("--tenant", "liquid-death")  # dry run default

        assert "LIQUID DEATH" in output
        assert "Liquid Death specifics:" in output
        assert "eventless approved/scheduled requests:" in output
        # 1 eventless candidate request for LD.
        assert "eventless approved/scheduled requests:    1" in output
