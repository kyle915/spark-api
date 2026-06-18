"""Apply-to-job staffing alert (jobs.mutations._notify_admins_of_application).

When a BA files a *fresh* application (newly created, or a re-apply from a
withdrawn/declined row) we email the staffing side — the event's assigned
RMM, the admin who posted the job, and the Ignite inbox — so admins get an
automated heads-up. A duplicate "already on file" apply must NOT re-notify,
and the brand client / applicant are never recipients of this alert.
"""
import uuid
from unittest.mock import MagicMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone

from jobs import models
from jobs.tests.base import JobsGraphQLTestCase


APPLY = """
    mutation Apply($input: ApplyToJobInput!) {
        applyToJob(input: $input) { success message applicationUuid }
    }
"""

MAILER_PATH = "jobs.mutations.JobApplicationReceivedMailer"


@pytest.mark.django_db(transaction=True)
class TestApplyNotifiesAdmins(JobsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_mobile import schema_mobile

        self.roles = self.setup_default_roles()
        self.schema = schema_mobile
        self.endpoint_path = "/api/v1/graphql/mobile"
        self.tenant = self.create_tenant(name="Notify Co")
        uid = str(uuid.uuid4())[:8]

        # RMM assigned to the event — a staffing recipient.
        self.rmm_user = self.create_user(
            username=f"rmm_{uid}@igniteproductions.co",
            email=f"rmm_{uid}@igniteproductions.co",
            role=self.roles["spark_admin"],
            password="x",
        )
        self.create_tenanted_user(user=self.rmm_user, tenant=self.tenant)

        self.ba_user = self.create_user(
            username=f"ba_{uid}@test.com",
            email=f"ba_{uid}@test.com",
            role=self.roles["ambassador"],
            password="x",
            first_name="Casey",
            last_name="BA",
        )
        self.create_tenanted_user(user=self.ba_user, tenant=self.tenant)
        self.ambassador = self.create_ambassador(user=self.ba_user)

        self.event = self.create_event(
            name="Tasting",
            tenant=self.tenant,
            address="9 Rd",
            start_time=timezone.now(),
            rmm_asigned=self.rmm_user,
        )
        self.job_title = self.create_job_title(name="BA", tenant=self.tenant)
        self.job = self.create_job(
            name="Sampling Gig",
            code=f"G-{uid}",
            address="9 Rd",
            event=self.event,
            job_title=self.job_title,
            tenant=self.tenant,
            lifecycle_status=models.Job.STATUS_POSTED,
            total_hours=5,
            hourly_rate=0,
            favorites_only=False,
        )

    async def _apply(self, user=None, **flags):
        return await self._execute_mutation_authenticated(
            APPLY,
            {"input": {"jobId": str(self.job.id), **flags}},
            user or self.ba_user,
        )

    @pytest.mark.asyncio
    async def test_fresh_application_emails_staffing_side(self):
        with patch(MAILER_PATH) as MockMailer:
            MockMailer.return_value = MagicMock()
            res = await self._apply()

        assert res.errors is None, res.errors
        assert res.data["applyToJob"]["success"] is True

        assert MockMailer.called, "expected a staffing alert on a fresh apply"
        kwargs = MockMailer.call_args.kwargs
        recipients = [r.lower() for r in kwargs["to_emails"]]
        # RMM + the always-on Ignite inbox are notified.
        assert self.rmm_user.email.lower() in recipients
        assert "events@igniteproductions.co" in recipients
        # The applicant (BA) is never a recipient of the staffing alert.
        assert self.ba_user.email.lower() not in recipients
        assert kwargs["applicant_name"] == "Casey BA"
        assert kwargs["job_name"] == "Sampling Gig"
        MockMailer.return_value.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_duplicate_apply_does_not_renotify(self):
        # First apply notifies — patch only to suppress the real send.
        with patch(MAILER_PATH):
            first = await self._apply()
        assert first.data["applyToJob"]["success"] is True

        # Second apply is a no-op ("already on file") → must NOT re-notify.
        with patch(MAILER_PATH) as MockMailer:
            res = await self._apply()
        assert res.data["applyToJob"]["success"] is True
        assert res.data["applyToJob"]["message"] == "Application already on file."
        assert not MockMailer.called

    @pytest.mark.asyncio
    async def test_apply_failure_does_not_notify(self):
        # A non-posted job rejects the apply — no application, no alert.
        await sync_to_async(
            lambda: models.Job.objects.filter(id=self.job.id).update(
                lifecycle_status=models.Job.STATUS_PENDING
            )
        )()
        with patch(MAILER_PATH) as MockMailer:
            res = await self._apply()
        assert res.data["applyToJob"]["success"] is False
        assert not MockMailer.called
