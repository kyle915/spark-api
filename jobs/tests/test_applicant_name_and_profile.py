"""
Tests for task #256 (backend): the admin Applicants list must show the BA's
real name (not the literal "Ambassador"), and a not-yet-booked applicant's
profile must be openable.

- jobApplications resolves ambassadorFirstName / ambassadorLastName /
  ambassadorEmail / ambassadorPhone / ambassadorImage off the BA + user.
- ambassadorProfileDetail returns a profile for a BA who only has a
  JobApplication in the tenant (no AmbassadorEvent yet).
"""
import uuid

import pytest
import strawberry_django  # noqa: F401
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from strawberry.relay import to_base64

from jobs import models
from jobs.tests.base import JobsGraphQLTestCase

User = get_user_model()


JOB_APPLICATIONS_QUERY = """
    query JobApplications($jobId: ID!) {
        jobApplications(jobId: $jobId) {
            uuid
            status
            ambassadorUuid
            ambassadorFirstName
            ambassadorLastName
            ambassadorEmail
            ambassadorPhone
            ambassadorImage
        }
    }
"""

PROFILE_DETAIL_QUERY = """
    query Profile($ambassadorUuid: ID!, $tenantId: ID) {
        ambassadorProfileDetail(ambassadorUuid: $ambassadorUuid, tenantId: $tenantId) {
            fullName
            email
            phone
        }
    }
"""


@pytest.mark.django_db(transaction=True)
class TestApplicantNameAndProfile(JobsGraphQLTestCase):

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_spark import schema_spark

        self.schema_spark = schema_spark
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Applicant Co")

        uid = str(uuid.uuid4())[:8]
        self.admin_user = self.create_user(
            username=f"admin_{uid}@test.com",
            email=f"admin_{uid}@test.com",
            role=self.roles["spark_admin"],
            password="testpass123",
            is_staff=True,
            is_superuser=True,
        )
        self.create_tenanted_user(user=self.admin_user, tenant=self.tenant)

        # BA with a real name on the User + phone/headshot on the Ambassador.
        self.ba_user = self.create_user(
            username=f"ba_{uid}@test.com",
            email=f"jordan_{uid}@test.com",
            role=self.roles["ambassador"],
            password="testpass123",
            first_name="Jordan",
            last_name="Rivera",
        )
        self.create_tenanted_user(user=self.ba_user, tenant=self.tenant)
        self.ambassador = self.create_ambassador(
            user=self.ba_user,
            phone="+15551230000",
            headshot="ambassadors/headshots/jordan.jpg",
        )

        self.event = self.create_event(
            name="Sampling Demo",
            tenant=self.tenant,
            address="1 Demo Way",
            start_time=timezone.now(),
        )
        self.job_title = self.create_job_title(name="BA", tenant=self.tenant)
        self.job = self.create_job(
            name="Demo Gig",
            code=f"GIG-{uid}",
            address="1 Demo Way",
            event=self.event,
            job_title=self.job_title,
            tenant=self.tenant,
            lifecycle_status=models.Job.STATUS_POSTED,
        )
        self.application = models.JobApplication.objects.create(
            tenant=self.tenant,
            job=self.job,
            ambassador=self.ambassador,
            status=models.JobApplication.STATUS_APPLIED,
            note="I'd be great at this!",
        )

        self.schema = self.schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

    @pytest.mark.asyncio
    @override_settings(GS_BUCKET_NAME="test-bucket")
    async def test_job_applications_returns_real_name_and_contact(self):
        result = await self._execute_query_authenticated(
            JOB_APPLICATIONS_QUERY,
            {"jobId": to_base64("Job", self.job.id)},
            self.admin_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errors: {result.errors}"
        rows = result.data["jobApplications"]
        assert len(rows) == 1
        row = rows[0]
        # The bug: these came back empty (FE then showed literal "Ambassador").
        assert row["ambassadorFirstName"] == "Jordan"
        assert row["ambassadorLastName"] == "Rivera"
        assert row["ambassadorEmail"].startswith("jordan_")
        assert row["ambassadorPhone"] == "+15551230000"
        # headshot blob path resolves to a non-empty public URL.
        assert row["ambassadorImage"]
        assert "jordan.jpg" in row["ambassadorImage"]
        assert row["ambassadorUuid"] == str(self.ambassador.uuid)

    @pytest.mark.asyncio
    async def test_profile_detail_works_for_applicant_without_booking(self):
        """A BA with only a JobApplication (no AmbassadorEvent) is now
        reachable so the admin can open their profile pop-up."""
        result = await self._execute_query_authenticated(
            PROFILE_DETAIL_QUERY,
            {
                "ambassadorUuid": str(self.ambassador.uuid),
                # resolve_id_to_int accepts a raw int string.
                "tenantId": str(self.tenant.id),
            },
            self.admin_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errors: {result.errors}"
        profile = result.data["ambassadorProfileDetail"]
        assert profile is not None, "applicant profile should be reachable"
        assert profile["fullName"] == "Jordan Rivera"
        assert profile["phone"] == "+15551230000"
