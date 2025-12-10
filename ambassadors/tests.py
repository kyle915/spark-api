import pytest
from config.schema_spark import schema_spark
from jobs import models as job_models
from jobs.tests.base import JobsGraphQLTestCase


@pytest.mark.django_db(transaction=True)
class TestApplyAmbassadorJob(JobsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        self.schema = schema_spark
        self.system_user = self.get_system_user()

        self.tenant = self.create_tenant()
        self.user_role = self.create_role("Ambassador", 3)
        self.user = self.create_user(
            "ambassador", "ambassador@spark.local", self.user_role
        )
        self.tenanted_user = self.create_tenanted_user(self.user, self.tenant)

        self.ambassador = self.create_ambassador(self.user)
        self.application_status = self.create_status("Pending Application", self.tenant)
        self.rate_type = self.create_rate_type("Hourly", self.tenant)
        self.rate = self.create_rate(25, self.rate_type, self.tenant)

        self.location = self.create_location("Main HQ", "MAIN", "00000", self.tenant)
        self.event = self.create_event("Launch", tenant=self.tenant, address="123 St")
        self.company = self.create_company(
            "ACME", "contact@acme.local", "123456789", self.tenant, location=self.location
        )
        self.job_title = self.create_job_title("Promoter", self.tenant)
        self.job = self.create_job(
            name="Test Job",
            code="JOB-1",
            address="123 St",
            company=self.company,
            event=self.event,
            location=self.location,
            job_title=self.job_title,
            tenant=self.tenant,
            rate=self.rate,
        )

        self.mutation = """
            mutation ApplyAmbassadorJob($jobId: ID!) {
                applyAmbassadorJob(jobId: $jobId) {
                    success
                    message
                    application {
                        id
                        appearAsRfp
                    }
                }
            }
        """

    @pytest.mark.asyncio
    async def test_apply_ambassador_job_success(self):
        variables = {"jobId": str(self.job.id)}
        response = await self._execute_mutation_authenticated(
            self.mutation, variables=variables, user=self.user
        )

        assert response.errors is None
        assert response.data["applyAmbassadorJob"]["success"] is True
        assert response.data["applyAmbassadorJob"]["message"] == "Application successful"
        assert response.data["applyAmbassadorJob"]["application"]["id"] is not None
        assert (
            response.data["applyAmbassadorJob"]["application"]["appearAsRfp"] is False
        )

        exists = await job_models.AmbassadorJob.objects.filter(
            ambassador=self.ambassador, job=self.job
        ).aexists()
        assert exists is True

    @pytest.mark.asyncio
    async def test_apply_ambassador_job_already_applied(self):
        await job_models.AmbassadorJob.objects.acreate(
            ambassador=self.ambassador,
            job=self.job,
            tenant=self.tenant,
            status=self.application_status,
            rate=self.rate,
            created_by=self.system_user,
        )

        variables = {"jobId": str(self.job.id)}
        response = await self._execute_mutation_authenticated(
            self.mutation, variables=variables, user=self.user
        )

        assert response.data["applyAmbassadorJob"]["success"] is False
        assert (
            response.data["applyAmbassadorJob"]["message"]
            == "Already applied to this job"
        )
