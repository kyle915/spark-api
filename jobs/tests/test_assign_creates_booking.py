"""
Tests for task #257: assigning/accepting a BA onto a job must create the
booking (AmbassadorEvent is_approved=True) so the shift surfaces on the
mobile "What's on the books" screens (myActiveShifts / myUpcomingShifts).

Before the fix, assign_ambassador_to_job flipped the JobApplication to
ACCEPTED and fired the "you got the gig" push but never created an
AmbassadorEvent, so the accepted BA's shift never appeared in the app.
"""
from datetime import timedelta
import uuid

import pytest
import strawberry_django  # noqa: F401
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.utils import timezone
from strawberry.relay import to_base64
from unittest.mock import patch

from ambassadors.models import AmbassadorEvent
from jobs import models
from jobs.tests.base import JobsGraphQLTestCase

User = get_user_model()


ASSIGN_MUTATION = """
    mutation Assign($input: AssignAmbassadorToJobInput!) {
        assignAmbassadorToJob(input: $input) {
            success
            message
            lifecycleStatus
        }
    }
"""

ACTIVE_SHIFTS_QUERY = """
    query { myActiveShifts { eventUuid isApproved } }
"""

UPCOMING_SHIFTS_QUERY = """
    query { myUpcomingShifts { eventUuid isApproved } }
"""


@pytest.mark.django_db(transaction=True)
class TestAssignCreatesBooking(JobsGraphQLTestCase):
    """assign_ambassador_to_job → AmbassadorEvent(is_approved=True)."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_spark import schema_spark
        from config.schema_mobile import schema_mobile

        self.schema_spark = schema_spark
        self.schema_mobile = schema_mobile

        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Booking Co")

        uid = str(uuid.uuid4())[:8]
        # Acting admin who performs the assign.
        self.admin_user = self.create_user(
            username=f"admin_{uid}@test.com",
            email=f"admin_{uid}@test.com",
            role=self.roles["spark_admin"],
            password="testpass123",
            is_staff=True,
            is_superuser=True,
        )
        self.create_tenanted_user(user=self.admin_user, tenant=self.tenant)

        # The BA who gets booked.
        self.ba_user = self.create_user(
            username=f"ba_{uid}@test.com",
            email=f"ba_{uid}@test.com",
            role=self.roles["ambassador"],
            password="testpass123",
        )
        self.create_tenanted_user(user=self.ba_user, tenant=self.tenant)
        self.ambassador = self.create_ambassador(user=self.ba_user)

        self.job_title = self.create_job_title(name="Brand Ambassador", tenant=self.tenant)

    def _make_job(self, *, start_time):
        event = self.create_event(
            name="Pop-up Demo",
            tenant=self.tenant,
            address="500 Demo Ave",
            start_time=start_time,
            date=start_time,
        )
        job = self.create_job(
            name="Demo Gig",
            code=f"GIG-{uuid.uuid4().hex[:6]}",
            address="500 Demo Ave",
            event=event,
            job_title=self.job_title,
            tenant=self.tenant,
            lifecycle_status=models.Job.STATUS_POSTED,
            total_hours=4,
            hourly_rate=25,
        )
        return event, job

    async def _assign(self, job, ambassador):
        variables = {
            "input": {
                "jobId": to_base64("Job", job.id),
                "ambassadorId": to_base64("Ambassador", ambassador.id),
            }
        }
        # Push + email are best-effort side-effects; stub them at the lowest
        # level so the test doesn't reach Redis / the mail driver (and so the
        # push's asyncio.run() can't deadlock the test event loop).
        self.schema = self.schema_spark
        with patch("ambassadors.push._send_push_to_user_sync"), patch(
            "utils.mailer.Mailer.send"
        ):
            return await self._execute_mutation_authenticated(
                ASSIGN_MUTATION, variables, self.admin_user,
                "/api/v1/graphql/spark",
            )

    async def _query_mobile(self, query, user):
        self.schema = self.schema_mobile
        return await self._execute_query_authenticated(
            query, {}, user, "/api/v1/graphql/mobile",
        )

    @pytest.mark.asyncio
    async def test_assign_creates_approved_booking_and_shows_in_active_shifts(self):
        """A today-dated gig: assign creates is_approved=True booking and it
        surfaces in myActiveShifts for the booked BA."""
        today = timezone.now().replace(hour=10, minute=0, second=0, microsecond=0)
        event, job = await sync_to_async(self._make_job)(start_time=today)

        result = await self._assign(job, self.ambassador)
        assert result.errors is None, f"errors: {result.errors}"
        assert result.data["assignAmbassadorToJob"]["success"] is True
        assert result.data["assignAmbassadorToJob"]["lifecycleStatus"] == (
            models.Job.STATUS_FILLED
        )

        # Booking exists and is approved.
        booking = await sync_to_async(
            lambda: AmbassadorEvent.objects.filter(
                ambassador=self.ambassador, event=event, is_approved=True
            ).first()
        )()
        assert booking is not None, "expected an is_approved=True AmbassadorEvent"
        assert await sync_to_async(lambda: booking.tenant_id)() == self.tenant.id
        assert await sync_to_async(lambda: booking.created_by_id)() == self.admin_user.id

        # Shift surfaces on the BA's "Active" tab today.
        shifts = await self._query_mobile(ACTIVE_SHIFTS_QUERY, self.ba_user)
        assert shifts.errors is None, f"errors: {shifts.errors}"
        event_uuids = [s["eventUuid"] for s in shifts.data["myActiveShifts"]]
        assert str(event.uuid) in event_uuids

    @pytest.mark.asyncio
    async def test_assign_creates_approved_booking_and_shows_in_upcoming_shifts(self):
        """A future-dated gig surfaces in myUpcomingShifts for the booked BA."""
        future = timezone.now() + timedelta(days=3)
        event, job = await sync_to_async(self._make_job)(start_time=future)

        result = await self._assign(job, self.ambassador)
        assert result.errors is None, f"errors: {result.errors}"
        assert result.data["assignAmbassadorToJob"]["success"] is True

        booking_approved = await sync_to_async(
            lambda: AmbassadorEvent.objects.filter(
                ambassador=self.ambassador, event=event, is_approved=True
            ).exists()
        )()
        assert booking_approved is True

        shifts = await self._query_mobile(UPCOMING_SHIFTS_QUERY, self.ba_user)
        assert shifts.errors is None, f"errors: {shifts.errors}"
        event_uuids = [s["eventUuid"] for s in shifts.data["myUpcomingShifts"]]
        assert str(event.uuid) in event_uuids

    @pytest.mark.asyncio
    async def test_assign_flips_existing_unapproved_invite_to_approved(self):
        """If a prior invite left an is_approved=False AmbassadorEvent, the
        assign flips it to True rather than duplicating the row."""
        today = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0)
        event, job = await sync_to_async(self._make_job)(start_time=today)

        # Pre-existing unapproved invite row.
        await sync_to_async(AmbassadorEvent.objects.create)(
            ambassador=self.ambassador,
            event=event,
            tenant=self.tenant,
            is_approved=False,
            created_by=self.admin_user,
            updated_by=self.admin_user,
        )

        result = await self._assign(job, self.ambassador)
        assert result.data["assignAmbassadorToJob"]["success"] is True

        rows = await sync_to_async(
            lambda: list(
                AmbassadorEvent.objects.filter(
                    ambassador=self.ambassador, event=event
                ).values_list("is_approved", flat=True)
            )
        )()
        assert rows == [True], f"expected a single approved row, got {rows}"
