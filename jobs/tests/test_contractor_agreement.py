"""
Coverage for the apply-time rate confirmation + contractor agreement.

When a job has an hourly rate AND an active contractor agreement is on
file, applying requires the BA to confirm the rate and accept the
agreement; the acceptance (version + rate snapshot + timestamp) is
written to the JobApplication. Brands with no agreement configured are
NOT blocked. ``ContractorAgreement.active_for_tenant`` prefers a
tenant's own active row over the global default.
"""
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from jobs import models
from jobs.tests.base import JobsGraphQLTestCase

User = get_user_model()


APPLY = """
    mutation Apply($input: ApplyToJobInput!) {
        applyToJob(input: $input) {
            success
            message
            applicationUuid
        }
    }
"""

AGREEMENT_QUERY = """
    query Agreement($tenantId: ID) {
        activeContractorAgreement(tenantId: $tenantId) { uuid version body }
    }
"""


@pytest.mark.django_db(transaction=True)
class TestContractorAgreement(JobsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_mobile import schema_mobile

        self.roles = self.setup_default_roles()
        self.schema = schema_mobile
        self.endpoint_path = "/api/v1/graphql/mobile"
        self.tenant = self.create_tenant(name="Agreement Co")
        uid = str(uuid.uuid4())[:8]
        self.ba_user = self.create_user(
            username=f"ba_{uid}@test.com",
            email=f"ba_{uid}@test.com",
            role=self.roles["ambassador"],
            password="x",
        )
        self.create_tenanted_user(user=self.ba_user, tenant=self.tenant)
        self.ambassador = self.create_ambassador(user=self.ba_user)

        self.event = self.create_event(
            name="Tasting", tenant=self.tenant, address="9 Rd",
            start_time=timezone.now(),
        )
        self.job_title = self.create_job_title(name="BA", tenant=self.tenant)
        self.job = self.create_job(
            name="Gig", code=f"G-{uid}", address="9 Rd", event=self.event,
            job_title=self.job_title, tenant=self.tenant,
            lifecycle_status=models.Job.STATUS_POSTED,
            total_hours=5, hourly_rate=30, favorites_only=False,
        )

    def _global_agreement(self, version="v1"):
        return models.ContractorAgreement.objects.create(
            version=version, body="Be a good contractor.", is_active=True,
            tenant=None,
        )

    async def _apply(self, **flags):
        return await self._execute_mutation_authenticated(
            APPLY,
            {"input": {"jobId": str(self.job.id), **flags}},
            self.ba_user,
        )

    # ---------- model resolution ----------

    @pytest.mark.django_db
    def test_active_for_tenant_prefers_override(self):
        self._global_agreement(version="global")
        override = models.ContractorAgreement.objects.create(
            version="tenant", body="brand terms", is_active=True,
            tenant=self.tenant,
        )
        resolved = models.ContractorAgreement.active_for_tenant(self.tenant.id)
        assert resolved.id == override.id
        # No override → global default.
        other = self.create_tenant(name="Other Co")
        resolved2 = models.ContractorAgreement.active_for_tenant(other.id)
        assert resolved2.version == "global"

    # ---------- gate ----------

    @pytest.mark.asyncio
    async def test_apply_blocked_without_confirmation(self):
        from asgiref.sync import sync_to_async

        await sync_to_async(self._global_agreement)()
        res = await self._apply(rateConfirmed=False, agreementAccepted=False)
        payload = res.data["applyToJob"]
        assert payload["success"] is False
        assert "rate" in payload["message"].lower()
        # Nothing recorded.
        exists = await sync_to_async(
            models.JobApplication.objects.filter(job=self.job).exists
        )()
        assert exists is False

    @pytest.mark.asyncio
    async def test_apply_blocked_without_agreement_acceptance(self):
        from asgiref.sync import sync_to_async

        await sync_to_async(self._global_agreement)()
        res = await self._apply(rateConfirmed=True, agreementAccepted=False)
        payload = res.data["applyToJob"]
        assert payload["success"] is False
        assert "agreement" in payload["message"].lower()

    @pytest.mark.asyncio
    async def test_apply_records_acceptance(self):
        from asgiref.sync import sync_to_async

        ag = await sync_to_async(self._global_agreement)(version="2026-06")
        res = await self._apply(rateConfirmed=True, agreementAccepted=True)
        payload = res.data["applyToJob"]
        assert payload["success"] is True, payload["message"]

        def _check():
            app = models.JobApplication.objects.get(job=self.job)
            assert app.agreement_id == ag.id
            assert app.agreement_version == "2026-06"
            assert float(app.rate_confirmed_amount) == 30.0
            assert app.agreement_accepted_at is not None

        await sync_to_async(_check)()

    @pytest.mark.asyncio
    async def test_apply_ungated_when_no_agreement(self):
        from asgiref.sync import sync_to_async

        # No agreement on file → apply succeeds even without flags, and no
        # acceptance is stamped.
        res = await self._apply()
        payload = res.data["applyToJob"]
        assert payload["success"] is True, payload["message"]

        def _check():
            app = models.JobApplication.objects.get(job=self.job)
            assert app.agreement_id is None
            assert app.rate_confirmed_amount is None

        await sync_to_async(_check)()

    # ---------- query ----------

    @pytest.mark.asyncio
    async def test_agreement_query_returns_active(self):
        from asgiref.sync import sync_to_async

        await sync_to_async(self._global_agreement)(version="2026-06")
        res = await self._execute_query_authenticated(
            AGREEMENT_QUERY, {"tenantId": str(self.tenant.id)},
            self.ba_user, self.endpoint_path,
        )
        assert res.errors is None, res.errors
        ag = res.data["activeContractorAgreement"]
        assert ag["version"] == "2026-06"
        assert ag["body"]

    @pytest.mark.asyncio
    async def test_agreement_query_null_when_none(self):
        res = await self._execute_query_authenticated(
            AGREEMENT_QUERY, {"tenantId": str(self.tenant.id)},
            self.ba_user, self.endpoint_path,
        )
        assert res.errors is None, res.errors
        assert res.data["activeContractorAgreement"] is None
