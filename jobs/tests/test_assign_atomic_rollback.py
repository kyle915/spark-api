"""
Regression tests: assign_ambassador_to_job must be ATOMIC.

The inner _assign() flips the JobApplication to ACCEPTED, auto-declines the
other applicants, sets the job FILLED + closed, and ONLY THEN books the shift
via _ensure_approved_booking(). That booking step is sync and can raise (e.g. a
transient DB error). Before the fix _assign() ran without a transaction, so a
booking failure left the job FILLED with NO AmbassadorEvent — and the
resolver's lifecycle gate ("Job is {status}; can't reassign.") then blocked any
retry from the UI, stranding the gig with no fix path but the
/internal/cron/book-ambassador-on-event stopgap.

The fix wraps the writes in transaction.atomic(): a booking failure rolls back
the JobApplication accept, the auto-declines, and the FILLED/closed flip too, so
the job stays PENDING/POSTED and remains re-assignable.

Mirrors the fixture style of test_assign_creates_booking.py
(JobsGraphQLTestCase, schema_spark, push/mailer stubbed).
"""
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


@pytest.mark.django_db(transaction=True)
class TestAssignAtomicRollback(JobsGraphQLTestCase):
    """A booking failure inside assign_ambassador_to_job rolls back the whole
    assignment, leaving the job re-assignable."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_spark import schema_spark

        self.schema_spark = schema_spark

        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Rollback Co")

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

        # The BA we attempt to book.
        self.ba_user = self.create_user(
            username=f"ba_{uid}@test.com",
            email=f"ba_{uid}@test.com",
            role=self.roles["ambassador"],
            password="testpass123",
        )
        self.create_tenanted_user(user=self.ba_user, tenant=self.tenant)
        self.ambassador = self.create_ambassador(user=self.ba_user)

        self.job_title = self.create_job_title(
            name="Brand Ambassador", tenant=self.tenant
        )

    def _make_job(self):
        """A POSTED gig with a today-dated event (sync; call via sync_to_async)."""
        start_time = timezone.now()
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

    def _vars(self, job):
        return {
            "input": {
                "jobId": to_base64("Job", job.id),
                "ambassadorId": to_base64("Ambassador", self.ambassador.id),
            }
        }

    async def _assign_ok(self, job):
        """A normal assign — the real booking step runs. Push + email are
        best-effort side-effects; stub them at the lowest level (matches
        test_assign_creates_booking.py) so the test never reaches Redis / the
        mail driver."""
        self.schema = self.schema_spark
        with patch("ambassadors.push._send_push_to_user_sync"), patch(
            "utils.mailer.Mailer.send"
        ):
            return await self._execute_mutation_authenticated(
                ASSIGN_MUTATION, self._vars(job), self.admin_user,
                "/api/v1/graphql/spark",
            )

    async def _assign_with_failing_booking(self, job):
        """An assign where the booking step (_ensure_approved_booking) raises —
        the stand-in for a transient DB error at the booking commit. Patched on
        the jobs.mutations module global the resolver calls by name."""
        self.schema = self.schema_spark
        with patch("ambassadors.push._send_push_to_user_sync"), patch(
            "utils.mailer.Mailer.send"
        ), patch(
            "jobs.mutations._ensure_approved_booking",
            side_effect=RuntimeError("transient booking failure"),
        ):
            return await self._execute_mutation_authenticated(
                ASSIGN_MUTATION, self._vars(job), self.admin_user,
                "/api/v1/graphql/spark",
            )

    @pytest.mark.asyncio
    async def test_booking_failure_rolls_back_assignment(self):
        """If _ensure_approved_booking raises, the FILLED/closed flip, the
        JobApplication accept, and the booking all roll back together — the job
        stays POSTED with no partial state."""
        event, job = await sync_to_async(self._make_job)()

        result = await self._assign_with_failing_booking(job)

        # The resolver does NOT swallow the booking error, so the mutation
        # surfaced it rather than reporting success.
        assert result.errors is not None, "expected the booking failure to surface"
        assert (
            result.data is None
            or result.data.get("assignAmbassadorToJob") is None
        )

        # Job rolled back to POSTED / not closed — still re-assignable.
        await sync_to_async(job.refresh_from_db)()
        assert job.lifecycle_status == models.Job.STATUS_POSTED
        assert job.closed is False

        # No partial JobApplication persisted (the get_or_create rolled back).
        app_exists = await sync_to_async(
            lambda: models.JobApplication.objects.filter(
                job=job, ambassador=self.ambassador
            ).exists()
        )()
        assert app_exists is False, "JobApplication should have rolled back"

        # No partial booking persisted either.
        booking_exists = await sync_to_async(
            lambda: AmbassadorEvent.objects.filter(
                ambassador=self.ambassador, event=event
            ).exists()
        )()
        assert booking_exists is False, "AmbassadorEvent should have rolled back"

    @pytest.mark.asyncio
    async def test_other_applicants_not_left_declined_after_rollback(self):
        """The auto-decline of competing applicants is part of the same atomic
        unit: a booking failure must NOT leave a rival's APPLIED row flipped to
        DECLINED. (Before the fix the declines committed even though the assign
        ultimately failed.)"""
        event, job = await sync_to_async(self._make_job)()

        # A second BA who has APPLIED to the same gig.
        other_user = await sync_to_async(self.create_user)(
            username=f"other_{uuid.uuid4().hex[:6]}@test.com",
            email=f"other_{uuid.uuid4().hex[:6]}@test.com",
            role=self.roles["ambassador"],
            password="testpass123",
        )
        await sync_to_async(self.create_tenanted_user)(
            user=other_user, tenant=self.tenant
        )
        other_amb = await sync_to_async(self.create_ambassador)(user=other_user)
        other_app = await sync_to_async(models.JobApplication.objects.create)(
            job=job,
            ambassador=other_amb,
            tenant=self.tenant,
            status=models.JobApplication.STATUS_APPLIED,
        )

        result = await self._assign_with_failing_booking(job)
        assert result.errors is not None

        # The rival's application is still APPLIED, not DECLINED.
        await sync_to_async(other_app.refresh_from_db)()
        assert other_app.status == models.JobApplication.STATUS_APPLIED

    @pytest.mark.asyncio
    async def test_job_remains_reassignable_after_booking_failure(self):
        """The point of the fix: after a booking failure rolls the assignment
        back, the admin can retry and it succeeds. Before the fix the job was
        stuck FILLED and the lifecycle gate ("can't reassign") blocked the
        retry, so the gig could never be booked from the UI."""
        event, job = await sync_to_async(self._make_job)()

        # First attempt: the booking step raises → everything rolls back.
        failed = await self._assign_with_failing_booking(job)
        assert failed.errors is not None
        await sync_to_async(job.refresh_from_db)()
        assert job.lifecycle_status == models.Job.STATUS_POSTED

        # Retry with a healthy booking step → succeeds and books the shift.
        ok = await self._assign_ok(job)
        assert ok.errors is None, f"retry errored: {ok.errors}"
        assert ok.data["assignAmbassadorToJob"]["success"] is True
        assert ok.data["assignAmbassadorToJob"]["lifecycleStatus"] == (
            models.Job.STATUS_FILLED
        )

        # The approved booking now exists and the job is FILLED + closed.
        booking_ok = await sync_to_async(
            lambda: AmbassadorEvent.objects.filter(
                ambassador=self.ambassador, event=event, is_approved=True
            ).exists()
        )()
        assert booking_ok is True

        await sync_to_async(job.refresh_from_db)()
        assert job.lifecycle_status == models.Job.STATUS_FILLED
        assert job.closed is True

        # And the accepted JobApplication persisted exactly once.
        accepted_status = await sync_to_async(
            lambda: models.JobApplication.objects.get(
                job=job, ambassador=self.ambassador
            ).status
        )()
        assert accepted_status == models.JobApplication.STATUS_ACCEPTED
