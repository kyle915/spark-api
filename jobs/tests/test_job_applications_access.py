"""
jobApplications access (clients schema).

It was open to any signed-in user: anyone could list any job's applicants
with email and phone. Now:

- Ignite staff (``_is_admin_access``) see every job's applicants, with
  contact details.
- Brand/client users keep the applicant list on their OWN tenant's jobs
  (the Job detail page renders it and is not admin-only) but email and
  phone come back empty; another brand's job returns ``[]``.
- A BA (no tenant membership) gets ``[]``.
"""
import uuid

import pytest
from django.utils import timezone
from strawberry.relay import to_base64

from jobs import models
from jobs.tests.base import JobsGraphQLTestCase

QUERY = """
    query JobApplications($jobId: ID!) {
        jobApplications(jobId: $jobId) {
            uuid
            ambassadorFirstName
            ambassadorLastName
            ambassadorEmail
            ambassadorPhone
            ambassadorUuid
        }
    }
"""


@pytest.mark.django_db(transaction=True)
class TestJobApplicationsAccess(JobsGraphQLTestCase):

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.roles = self.setup_default_roles()
        uid = str(uuid.uuid4())[:8]

        self.tenant = self.create_tenant(name="Own Brand")
        self.other_tenant = self.create_tenant(name="Other Brand")

        self.admin = self.create_user(
            username=f"ja_admin_{uid}@test.com", email=f"ja_admin_{uid}@test.com",
            role=self.roles["spark_admin"], password="testpass123",
        )
        self.client_user = self.create_user(
            username=f"ja_client_{uid}@test.com", email=f"ja_client_{uid}@test.com",
            role=self.roles["client"], password="testpass123",
        )
        self.create_tenanted_user(user=self.client_user, tenant=self.tenant)

        self.ba_user = self.create_user(
            username=f"ja_ba_{uid}@test.com", email=f"applicant_{uid}@test.com",
            role=self.roles["ambassador"], password="testpass123",
            first_name="Jordan", last_name="Rivera",
        )
        self.ambassador = self.create_ambassador(
            user=self.ba_user, phone="+15551230000",
        )

        self.own_job = self._job(self.tenant, f"OWN-{uid}")
        self.other_job = self._job(self.other_tenant, f"OTH-{uid}")
        for job, tenant in ((self.own_job, self.tenant),
                            (self.other_job, self.other_tenant)):
            models.JobApplication.objects.create(
                tenant=tenant, job=job, ambassador=self.ambassador,
                status=models.JobApplication.STATUS_APPLIED,
            )

    def _job(self, tenant, code):
        event = self.create_event(
            name=f"Demo {code}", tenant=tenant, address="1 Demo Way",
            start_time=timezone.now(),
        )
        title = self.create_job_title(name=f"BA {code}", tenant=tenant)
        return self.create_job(
            name=f"Gig {code}", code=code, address="1 Demo Way", event=event,
            job_title=title, tenant=tenant,
            lifecycle_status=models.Job.STATUS_POSTED,
        )

    async def _rows(self, job, user):
        result = await self._execute_query_authenticated(
            QUERY, {"jobId": to_base64("Job", job.id)}, user, self.endpoint_path,
        )
        assert result.errors is None, result.errors
        return result.data["jobApplications"]

    @pytest.mark.asyncio
    async def test_staff_sees_any_job_with_contact(self):
        for job in (self.own_job, self.other_job):
            rows = await self._rows(job, self.admin)
            assert len(rows) == 1
            assert rows[0]["ambassadorEmail"] == self.ba_user.email
            assert rows[0]["ambassadorPhone"] == "+15551230000"

    @pytest.mark.asyncio
    async def test_brand_user_own_job_names_kept_contact_stripped(self):
        rows = await self._rows(self.own_job, self.client_user)
        assert len(rows) == 1
        row = rows[0]
        # Names (what brand users already see) are unchanged.
        assert row["ambassadorFirstName"] == "Jordan"
        assert row["ambassadorLastName"] == "Rivera"
        assert row["ambassadorUuid"] == str(self.ambassador.uuid)
        # Contact details are Ignite-staff only.
        assert row["ambassadorEmail"] == ""
        assert row["ambassadorPhone"] == ""

    @pytest.mark.asyncio
    async def test_brand_user_cannot_list_other_brand_applicants(self):
        assert await self._rows(self.other_job, self.client_user) == []

    @pytest.mark.asyncio
    async def test_ba_gets_nothing(self):
        assert await self._rows(self.own_job, self.ba_user) == []
