"""
Tests for task #255: the BA-facing myApplications query.

my_available_jobs hides jobs the BA already applied to, so myApplications is
the only place a BA sees their application history + status. A BA must see
their own applications (with the nested job summary the mobile screen needs)
and must NOT see another BA's applications.
"""
import uuid

import pytest
import strawberry_django  # noqa: F401
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.utils import timezone

from jobs import models
from jobs.tests.base import JobsGraphQLTestCase

User = get_user_model()


MY_APPLICATIONS_QUERY = """
    query MyApplications($status: String) {
        myApplications(status: $status) {
            applicationUuid
            status
            appliedAt
            decidedAt
            job {
                id
                uuid
                name
                hourlyRate
                totalHours
                event {
                    uuid
                    name
                    address
                    date
                    startTime
                    endTime
                }
            }
        }
    }
"""


@pytest.mark.django_db(transaction=True)
class TestMyApplications(JobsGraphQLTestCase):

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_mobile import schema_mobile

        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Apps Co")
        uid = str(uuid.uuid4())[:8]

        # The calling BA.
        self.ba_user = self.create_user(
            username=f"ba_{uid}@test.com",
            email=f"ba_{uid}@test.com",
            role=self.roles["ambassador"],
            password="testpass123",
        )
        self.create_tenanted_user(user=self.ba_user, tenant=self.tenant)
        self.ambassador = self.create_ambassador(user=self.ba_user)

        # A second BA whose applications must stay invisible to the first.
        self.other_user = self.create_user(
            username=f"other_{uid}@test.com",
            email=f"other_{uid}@test.com",
            role=self.roles["ambassador"],
            password="testpass123",
        )
        self.create_tenanted_user(user=self.other_user, tenant=self.tenant)
        self.other_ambassador = self.create_ambassador(user=self.other_user)

        self.event = self.create_event(
            name="Tasting Event",
            tenant=self.tenant,
            address="9 Tasting Rd",
            start_time=timezone.now(),
        )
        self.job_title = self.create_job_title(name="BA", tenant=self.tenant)
        self.job = self.create_job(
            name="Tasting Gig",
            code=f"GIG-{uid}",
            address="9 Tasting Rd",
            event=self.event,
            job_title=self.job_title,
            tenant=self.tenant,
            lifecycle_status=models.Job.STATUS_POSTED,
            total_hours=5,
            hourly_rate=30,
        )

        # My application (applied) + the other BA's application on the same job.
        self.my_app = models.JobApplication.objects.create(
            tenant=self.tenant, job=self.job, ambassador=self.ambassador,
            status=models.JobApplication.STATUS_APPLIED,
        )
        models.JobApplication.objects.create(
            tenant=self.tenant, job=self.job, ambassador=self.other_ambassador,
            status=models.JobApplication.STATUS_APPLIED,
        )

        self.schema = schema_mobile
        self.endpoint_path = "/api/v1/graphql/mobile"

    @pytest.mark.asyncio
    async def test_my_applications_returns_own_with_job_summary(self):
        result = await self._execute_query_authenticated(
            MY_APPLICATIONS_QUERY, {}, self.ba_user, self.endpoint_path
        )
        assert result.errors is None, f"errors: {result.errors}"
        rows = result.data["myApplications"]
        assert len(rows) == 1, "BA should see exactly their own application"
        row = rows[0]
        assert row["applicationUuid"] == str(self.my_app.uuid)
        assert row["status"] == models.JobApplication.STATUS_APPLIED
        assert row["appliedAt"]
        # Nested job summary the mobile screen renders.
        assert row["job"]["uuid"] == str(self.job.uuid)
        assert row["job"]["name"] == "Tasting Gig"
        assert float(row["job"]["hourlyRate"]) == 30.0
        assert float(row["job"]["totalHours"]) == 5.0
        assert row["job"]["event"]["uuid"] == str(self.event.uuid)
        assert row["job"]["event"]["address"] == "9 Tasting Rd"
        assert row["job"]["event"]["startTime"] is not None

    @pytest.mark.asyncio
    async def test_my_applications_excludes_other_bas(self):
        """The other BA sees only their own row, never mine."""
        result = await self._execute_query_authenticated(
            MY_APPLICATIONS_QUERY, {}, self.other_user, self.endpoint_path
        )
        assert result.errors is None, f"errors: {result.errors}"
        rows = result.data["myApplications"]
        uuids = {r["applicationUuid"] for r in rows}
        assert str(self.my_app.uuid) not in uuids

    @pytest.mark.asyncio
    async def test_my_applications_status_filter(self):
        """The status arg narrows to a single status."""
        # Add a withdrawn application on a second job.
        event2 = await sync_to_async(self.create_event)(
            name="Second Event", tenant=self.tenant, address="2 St",
            start_time=timezone.now(),
        )
        job2 = await sync_to_async(self.create_job)(
            name="Second Gig", code=f"G2-{uuid.uuid4().hex[:6]}", address="2 St",
            event=event2, job_title=self.job_title, tenant=self.tenant,
            lifecycle_status=models.Job.STATUS_POSTED,
        )
        await sync_to_async(models.JobApplication.objects.create)(
            tenant=self.tenant, job=job2, ambassador=self.ambassador,
            status=models.JobApplication.STATUS_WITHDRAWN,
        )

        # applied-only → just the original.
        applied = await self._execute_query_authenticated(
            MY_APPLICATIONS_QUERY, {"status": "applied"}, self.ba_user,
            self.endpoint_path,
        )
        assert applied.errors is None, f"errors: {applied.errors}"
        applied_rows = applied.data["myApplications"]
        assert len(applied_rows) == 1
        assert applied_rows[0]["status"] == "applied"

        # no filter → both.
        both = await self._execute_query_authenticated(
            MY_APPLICATIONS_QUERY, {}, self.ba_user, self.endpoint_path
        )
        assert both.errors is None
        assert len(both.data["myApplications"]) == 2
