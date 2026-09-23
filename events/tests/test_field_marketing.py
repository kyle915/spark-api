"""Torch field marketing stays off the retail sampling request type."""

import pytest
from django.core.exceptions import MultipleObjectsReturned

from events import models as em
from events.field_marketing import (
    FieldMarketingError,
    _active_tenant_for_user,
    _require_torch_user,
    build_board,
    log_results,
    plan_event,
)
from events.tests.base import EventsGraphQLTestCase


@pytest.mark.django_db(transaction=True)
class TestFieldMarketing(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(
            name="Torch THC",
            slug="torch-thc",
            request_url_name="keee-torch-thc",
        )
        self.other = self.create_tenant(name="Other Brand", slug="other-brand")
        self.sys = self.get_system_user()
        self.marketer = self.create_user(
            username="alec",
            email="alec@torchdrinks.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.marketer, self.tenant)
        self.other_user = self.create_user(
            username="other",
            email="other@example.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.other_user, self.other)

    def _plan(self, **overrides):
        payload = {
            "market": "miami",
            "activity": "full_can",
            "name": "Wynwood drop",
            "starts_on": "2026-09-12",
            "days": 1,
            "address": "123 NW 2nd Ave, Miami, FL",
            "notes": "Guerilla",
            "planned_full_cans": 200,
            "planned_pour_samples": 0,
            "planned_emails": 40,
        }
        payload.update(overrides)
        return plan_event(user=self.marketer, payload=payload, submit=overrides.get("submit", False))

    def test_plan_does_not_create_a_request(self):
        event = self._plan()
        assert event.status == em.FieldMarketingEvent.STATUS_PLANNED
        assert event.request_id is None
        assert em.Request.objects.filter(tenant=self.tenant).count() == 0
        board = build_board(self.tenant, "2026-09")
        cans = next(row for row in board["kpis"] if row["key"] == "full_cans")
        assert cans["target"] == 1152
        assert cans["planned"] == 200
        assert cans["logged"] is None

    def test_submit_creates_field_marketing_request(self):
        event = self._plan(submit=True)
        event.refresh_from_db()
        assert event.status == em.FieldMarketingEvent.STATUS_SUBMITTED
        request = event.request
        assert request is not None
        assert request.request_type.name == "Field Marketing"
        assert request.status_id is None
        assert request.name.startswith("Field marketing ·")
        assert "Retail Sampling" not in request.request_type.name
        assert em.RequestActivityLog.objects.filter(request=request).exists()

    def test_pour_samples_do_not_count_as_full_cans(self):
        self._plan(
            activity="pour",
            name="Festival pour",
            planned_full_cans=999,
            planned_pour_samples=300,
        )
        board = build_board(self.tenant, "2026-09")
        cans = next(row for row in board["kpis"] if row["key"] == "full_cans")
        pours = next(row for row in board["kpis"] if row["key"] == "pour_samples")
        assert cans["planned"] == 0
        assert pours["planned"] == 300
        assert pours["target"] == 3456

    def test_logged_zero_is_a_result_and_a_blank_month_is_not(self):
        event = self._plan()
        board = build_board(self.tenant, "2026-09")
        emails = next(row for row in board["kpis"] if row["key"] == "emails")
        assert emails["logged"] is None
        log_results(
            user=self.marketer,
            event_id=str(event.uuid),
            payload={"logged_emails": 0, "logged_full_cans": 180},
        )
        board = build_board(self.tenant, "2026-09")
        emails = next(row for row in board["kpis"] if row["key"] == "emails")
        cans = next(row for row in board["kpis"] if row["key"] == "full_cans")
        assert emails["logged"] == 0
        assert cans["logged"] == 180

    def test_sponsorship_days_and_retail_count_separately(self):
        self._plan(
            activity="sponsorship",
            name="Austin fest",
            market="austin-dallas",
            days=3,
            planned_pour_samples=90,
        )
        retail = self._plan(
            activity="retail_support",
            name="DP meeting",
            market="houston",
            planned_emails=5,
        )
        board = build_board(self.tenant, "2026-09")
        days = next(row for row in board["kpis"] if row["key"] == "sponsorship_days")
        support = next(row for row in board["kpis"] if row["key"] == "retail_support")
        assert days["planned"] == 3
        assert days["target"] == 8
        assert days["logged"] is None
        assert support["planned"] == 1
        assert support["logged"] is None
        log_results(
            user=self.marketer,
            event_id=str(retail.uuid),
            payload={},
        )
        board = build_board(self.tenant, "2026-09")
        support = next(row for row in board["kpis"] if row["key"] == "retail_support")
        assert support["logged"] == 1




    def test_all_dates_returns_every_plan_row(self):
        self._plan(starts_on="2026-07-10", planned_full_cans=100)
        self._plan(
            name="Sep drop",
            starts_on="2026-09-12",
            planned_full_cans=50,
        )
        all_board = build_board(self.tenant, month=None)
        assert all_board["month"] == "all"
        assert all_board["month_label"] == "All plans"
        assert len(all_board["events"]) == 2
        cans = next(row for row in all_board["kpis"] if row["key"] == "full_cans")
        assert cans["planned"] == 150
        # Monthly target still returned for reference; FE must not score all-dates against it.
        assert cans["target"] == 1152
        assert cans["logged"] is None
        sept = build_board(self.tenant, month="2026-09")
        assert sept["month"] == "2026-09"
        assert len(sept["events"]) == 1
        cans_s = next(row for row in sept["kpis"] if row["key"] == "full_cans")
        assert cans_s["planned"] == 50

    def test_market_filter_scopes_events_and_projected_kpis(self):
        self._plan(market="miami", planned_full_cans=200, planned_emails=40)
        self._plan(
            market="houston",
            name="Houston drop",
            planned_full_cans=80,
            planned_emails=10,
        )
        all_board = build_board(self.tenant, "2026-09")
        miami = build_board(self.tenant, "2026-09", market="miami")
        houston = build_board(self.tenant, "2026-09", market="houston")
        assert len(all_board["events"]) == 2
        assert len(miami["events"]) == 1
        assert miami["events"][0]["market"] == "miami"
        assert miami["events"][0]["manager_name"] == "Alec Aparicio"
        cans_all = next(row for row in all_board["kpis"] if row["key"] == "full_cans")
        cans_miami = next(row for row in miami["kpis"] if row["key"] == "full_cans")
        cans_houston = next(row for row in houston["kpis"] if row["key"] == "full_cans")
        assert cans_all["planned"] == 280
        assert cans_miami["planned"] == 200
        assert cans_houston["planned"] == 80
        assert cans_miami["target"] == 1152
        assert cans_miami["logged"] is None
        assert len(miami["managers"]) == 4

    def test_other_brand_cannot_plan(self):
        with pytest.raises(FieldMarketingError):
            plan_event(
                user=self.other_user,
                payload={
                    "market": "miami",
                    "activity": "full_can",
                    "name": "Nope",
                    "starts_on": "2026-09-12",
                },
                submit=False,
            )

    def test_submit_requires_an_address(self):
        with pytest.raises(FieldMarketingError):
            self._plan(address="", submit=True)
        assert em.Request.objects.filter(tenant=self.tenant).count() == 0

    def test_multi_tenant_admin_uses_active_torch_tenant(self):
        """Spark admins have many TenantedUser rows — never blind user.tenant."""
        admin = self.create_user(
            username="kyle_admin",
            email="kyle@igniteproductions.co",
            role=self.roles["spark_admin"],
            is_staff=True,
        )
        self.create_tenanted_user(admin, self.tenant)
        self.create_tenanted_user(admin, self.other)
        for i in range(20):
            brand = self.create_tenant(name=f"Brand {i}", slug=f"extra-brand-{i}")
            self.create_tenanted_user(admin, brand)

        with pytest.raises(MultipleObjectsReturned):
            _ = admin.tenant

        assert _active_tenant_for_user(admin) is None

        torch = _require_torch_user(admin, tenant_id=self.tenant.id)
        assert torch.id == self.tenant.id
        board = build_board(torch, "2026-09")
        assert board["available"] is True
        assert len(board["kpis"]) == 5
        assert {row["key"] for row in board["kpis"]} == {
            "full_cans",
            "pour_samples",
            "sponsorship_days",
            "retail_support",
            "emails",
        }

        with pytest.raises(FieldMarketingError, match="Switch to Torch"):
            _require_torch_user(admin, tenant_id=self.other.id)

        event = plan_event(
            user=admin,
            payload={
                "market": "miami",
                "activity": "full_can",
                "name": "Admin planned drop",
                "starts_on": "2026-09-15",
                "address": "1 Brickell, Miami, FL",
                "planned_full_cans": 50,
            },
            submit=False,
            tenant_id=self.tenant.id,
        )
        assert event.tenant_id == self.tenant.id

    @pytest.mark.asyncio
    async def test_plan_field_marketing_accepts_relay_tenant_global_id(self):
        """Clients schema: planFieldMarketing tenantId as TenantType:ID global id.

        Kyle's admin UI sends VGVuYW50VHlwZToxNw== (TenantType:17). The live
        PlanFieldMarketingInput must accept that field and resolve Torch.
        """
        import base64

        from asgiref.sync import sync_to_async
        from config.schema_client import schema_clients

        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"

        def _seed_multi_tenant_admin():
            admin = self.create_user(
                username="kyle_relay_admin",
                email="kyle.relay@igniteproductions.co",
                role=self.roles["spark_admin"],
                is_staff=True,
            )
            self.create_tenanted_user(admin, self.tenant)
            self.create_tenanted_user(admin, self.other)
            for i in range(5):
                brand = self.create_tenant(
                    name=f"Relay Brand {i}", slug=f"relay-brand-{i}"
                )
                self.create_tenanted_user(admin, brand)
            return admin

        admin = await sync_to_async(_seed_multi_tenant_admin)()

        relay_tenant_id = base64.b64encode(
            f"TenantType:{self.tenant.id}".encode()
        ).decode()

        plan = """
        mutation PlanFieldMarketing($input: PlanFieldMarketingInput!) {
          planFieldMarketing(input: $input) {
            success
            message
            event { id name status }
          }
        }
        """
        result = await self._execute_mutation_authenticated(
            plan,
            {
                "input": {
                    "market": "miami",
                    "activity": "sponsorship",
                    "name": "Wynwood Test Festival",
                    "startsOn": "2026-09-25",
                    "days": 1,
                    "plannedPourSamples": 5000,
                    "plannedEmails": 15,
                    "submit": False,
                    "tenantId": relay_tenant_id,
                }
            },
            user=admin,
        )
        assert result.errors is None, result.errors
        payload = result.data["planFieldMarketing"]
        assert payload["success"] is True
        assert payload["event"]["name"] == "Wynwood Test Festival"
        assert (
            await sync_to_async(
                em.FieldMarketingEvent.objects.filter(
                    tenant=self.tenant, name="Wynwood Test Festival"
                ).count
            )()
            == 1
        )

        board_q = """
        query FieldMarketingBoard($month: String, $tenantId: ID) {
          fieldMarketing(month: $month, tenantId: $tenantId) {
            available
            kpis { key planned logged target }
          }
        }
        """
        board = await self._execute_query_authenticated(
            board_q,
            {"month": "2026-09", "tenantId": relay_tenant_id},
            user=admin,
        )
        assert board.errors is None, board.errors
        assert board.data["fieldMarketing"]["available"] is True
        pours = next(
            row
            for row in board.data["fieldMarketing"]["kpis"]
            if row["key"] == "pour_samples"
        )
        assert pours["planned"] == 5000
        assert pours["logged"] is None
