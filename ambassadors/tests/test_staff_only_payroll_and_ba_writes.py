"""
Staff-only gates on Wingspan payroll data and BA write mutations.

Brand users (role=client, tenant-scoped) could reach these through the
clients schema (the one the web app talks to):

- wingspanStatus / wingspanPayrollPeriods / wingspanPayments exposed
  Ignite's entire Wingspan account (every contractor's name, email and
  pay, across all clients).
- createAmbassador / updateAmbassador / createAmbassadorWithUser /
  approveAmbassador / regenerateAmbassadorPasswords /
  createAmbassadorSkill / deleteAmbassadorSkill let a brand user create,
  edit, activate or reset the password of any BA.

All are now ``IsSparkAdminAccess`` (``_is_admin_access``). Every case
runs through the real clients schema.
"""

import pytest
from asgiref.sync import sync_to_async

from ambassadors.models import Ambassador
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from wingspan import client as wingspan_client

PERIODS = """
query { wingspanPayrollPeriods(limit: 5) { id label totalAmount contractorCount } }
"""

PAYMENTS = """
query { wingspanPayments(limit: 5) { id contractorName contractorEmail amount } }
"""

STATUS = """
query { wingspanStatus { connected message } }
"""

CREATE_AMB = """
mutation M($input: CreateAmbassadorInput!) {
  createAmbassador(input: $input) { success message }
}
"""

UPDATE_AMB = """
mutation M($input: UpdateAmbassadorInput!) {
  updateAmbassador(input: $input) { success message }
}
"""

CREATE_WITH_USER = """
mutation M($input: CreateAmbassadorWithUserInput!) {
  createAmbassadorWithUser(input: $input) { success message }
}
"""

APPROVE = """
mutation M($input: ApproveAmbassadorInput!) {
  approveAmbassador(input: $input) { success message }
}
"""

REGEN = """
mutation M($input: RegenerateAmbassadorPasswordsInput!) {
  regenerateAmbassadorPasswords(input: $input) { success message }
}
"""

CREATE_SKILL = """
mutation M($input: CreateAmbassadorSkillInput!) {
  createAmbassadorSkill(input: $input) { success message }
}
"""

DELETE_SKILL = """
mutation M($input: DeleteAmbassadorSkillInput!) {
  deleteAmbassadorSkill(input: $input) { success message }
}
"""


def _assert_refused(result, field):
    assert result.errors, f"{field}: brand user must be refused"
    assert "Only Ignite admins" in str(result.errors[0].message)
    assert result.data is None or result.data.get(field) is None


@pytest.mark.django_db(transaction=True)
class TestStaffOnlyPayrollAndBaWrites(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db, monkeypatch):
        from config.schema_client import schema_clients

        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.roles = self.setup_default_roles()

        self.tenant = self.create_tenant(name="Staff Gate Brand")
        self.admin = self.create_user(
            username="staffgate-admin", email="staffgate-admin@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="staffgate-client", email="staffgate-client@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        self.ba_user = self.create_user(
            username="staffgate-ba", email="staffgate-ba@test.com",
            role=self.roles["ambassador"],
        )
        self.create_tenanted_user(self.ba_user, self.tenant)
        self.ba = self.create_ambassador(
            self.ba_user, address="1 Original St", is_active=False,
        )

        # Fake Wingspan so staff calls return real-looking rows.
        async def _periods(*, limit=12):
            return [wingspan_client.PayrollPeriod(
                id="p1", label="Nov 1 – Nov 7", total_amount=1234.5,
                contractor_count=7,
            )]

        async def _payments(*, period_id=None, limit=50):
            return [wingspan_client.Payment(
                id="pay1", contractor_name="Other Brand BA",
                contractor_email="someone@else.test", amount=250.0,
            )]

        monkeypatch.setattr(wingspan_client, "list_payroll_periods", _periods)
        monkeypatch.setattr(wingspan_client, "list_payments", _payments)

    async def _run(self, query, variables, user):
        return await self._execute_mutation(
            query, variables, self.endpoint_path, user=user
        )

    # ── Wingspan ─────────────────────────────────────────────────────

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "query,field",
        [
            (PERIODS, "wingspanPayrollPeriods"),
            (PAYMENTS, "wingspanPayments"),
            (STATUS, "wingspanStatus"),
        ],
    )
    async def test_wingspan_refused_for_brand_user(self, query, field):
        result = await self._run(query, None, self.client_user)
        _assert_refused(result, field)

    @pytest.mark.asyncio
    async def test_wingspan_refused_for_ba(self):
        result = await self._run(PAYMENTS, None, self.ba_user)
        _assert_refused(result, "wingspanPayments")

    @pytest.mark.asyncio
    async def test_wingspan_staff_still_reads(self):
        periods = await self._run(PERIODS, None, self.admin)
        assert periods.errors is None, periods.errors
        assert periods.data["wingspanPayrollPeriods"][0]["contractorCount"] == 7

        payments = await self._run(PAYMENTS, None, self.admin)
        assert payments.errors is None, payments.errors
        row = payments.data["wingspanPayments"][0]
        assert row["contractorEmail"] == "someone@else.test"
        assert row["amount"] == 250.0

        status = await self._run(STATUS, None, self.admin)
        assert status.errors is None, status.errors

    # ── BA writes ────────────────────────────────────────────────────

    def _write_cases(self):
        return [
            (CREATE_AMB, "createAmbassador",
             {"userId": str(self.client_user.id)}),
            (UPDATE_AMB, "updateAmbassador",
             {"ambassadorId": str(self.ba.id), "address": "Hijacked"}),
            (CREATE_WITH_USER, "createAmbassadorWithUser",
             {"firstName": "New", "email": "brand-made-ba@x.test",
              "isActive": True}),
            (APPROVE, "approveAmbassador",
             {"ambassadorId": str(self.ba.id)}),
            (REGEN, "regenerateAmbassadorPasswords",
             {"ambassadorIds": [str(self.ba.id)]}),
            (CREATE_SKILL, "createAmbassadorSkill",
             {"ambassadorId": str(self.ba.id), "skillId": "1"}),
            (DELETE_SKILL, "deleteAmbassadorSkill",
             {"ambassadorSkillId": "1"}),
        ]

    @pytest.mark.asyncio
    async def test_ba_writes_refused_for_brand_user(self):
        password_before = self.ba_user.password
        for query, field, input_ in self._write_cases():
            result = await self._run(query, {"input": input_}, self.client_user)
            _assert_refused(result, field)

        ba = await sync_to_async(
            Ambassador.objects.select_related("user").get
        )(pk=self.ba.pk)
        assert ba.address == "1 Original St"
        assert ba.is_active is False
        assert ba.user.password == password_before
        from django.contrib.auth import get_user_model

        made = await sync_to_async(
            get_user_model().objects.filter(email="brand-made-ba@x.test").exists
        )()
        assert not made

    @pytest.mark.asyncio
    async def test_ba_writes_refused_for_ba(self):
        for query, field, input_ in self._write_cases():
            result = await self._run(query, {"input": input_}, self.ba_user)
            _assert_refused(result, field)

    @pytest.mark.asyncio
    async def test_staff_can_update_and_approve_ba(self):
        upd = await self._run(
            UPDATE_AMB,
            {"input": {"ambassadorId": str(self.ba.id), "address": "2 Staff St"}},
            self.admin,
        )
        assert upd.errors is None, upd.errors
        assert upd.data["updateAmbassador"]["success"] is True

        appr = await self._run(
            APPROVE, {"input": {"ambassadorId": str(self.ba.id)}}, self.admin
        )
        assert appr.errors is None, appr.errors
        assert appr.data["approveAmbassador"]["success"] is True

        ba = await sync_to_async(Ambassador.objects.get)(pk=self.ba.pk)
        assert ba.address == "2 Staff St"
        assert ba.is_active is True

    @pytest.mark.asyncio
    async def test_staff_can_create_ba_with_user(self):
        result = await self._run(
            CREATE_WITH_USER,
            {"input": {"firstName": "Staff", "email": "staff-made-ba@x.test",
                       "isActive": True}},
            self.admin,
        )
        assert result.errors is None, result.errors
        assert result.data["createAmbassadorWithUser"]["success"] is True
