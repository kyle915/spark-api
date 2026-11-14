"""Retailer suggestions for events that report as "Unknown"."""

import pytest
from datetime import datetime, timedelta, timezone as _tz
from unittest import mock

from asgiref.sync import sync_to_async
from django.core.management import call_command

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.models import Retailer, SheetShift
from recaps import models as recap_models
from recaps import retailer_proposals as rp

CUSTOM_RECAP_QUERY = """
query Q($id: ID!) {
  customRecap(id: $id) { retailerProposal { id name confidence source sourceLabel reason isNewRetailer } }
}
"""

APPROVE_CUSTOM = """
mutation A($id: ID!, $accept: Boolean) {
  approveCustomRecap(input: { id: $id, approved: true, acceptRetailerProposal: $accept }) {
    success message
  }
}
"""

DECIDE = """
mutation D($id: ID!, $accept: Boolean!, $name: String) {
  decideRetailerProposal(input: { proposalId: $id, accept: $accept, retailerName: $name }) {
    success message retailerName
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestRetailerProposals(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Torch")
        self.admin = self.create_user(
            username="admin-rp", email="admin-rp@test.com", role=self.roles["spark_admin"]
        )
        now = datetime.now(_tz.utc)
        self.event = self.create_event(
            name="Sampling 9703 Barker Cypress",
            tenant=self.tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )
        self.event.address = "9703 Barker Cypress Rd, Cypress, TX 77433"
        self.event.save(update_fields=["address"])
        event_type = self.create_event_type(name="Sampling", tenant=self.tenant)
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="T", event_type=event_type, tenant=self.tenant, created_by=self.system_user
        )
        self.kroger = Retailer.objects.create(
            name="Kroger", tenant=self.tenant, created_by=self.system_user
        )

    def _recap(self, **kw):
        return recap_models.CustomRecap.objects.create(
            name="Recap",
            event=self.event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            submitted_at=datetime.now(_tz.utc),
            created_by=self.system_user,
            updated_by=self.system_user,
            **kw,
        )

    def _sheet(self, store):
        return SheetShift.objects.create(
            tenant=self.tenant,
            sheet_id="s",
            row_number=2,
            day=self.event.date.date(),
            store_name=store,
            ba_name="A B",
            event=self.event,
        )

    # ---- suggestion sources -------------------------------------------------

    def test_sheet_store_name_matches_a_known_retailer(self):
        self._sheet("Kroger #412 - Barker Cypress")
        suggestion, _ = rp.suggest_for_event(self.event, use_network=False)
        assert suggestion.name == "Kroger"
        assert suggestion.retailer_id == self.kroger.id
        assert suggestion.source == "sheet" and suggestion.confidence == "high"

    def test_typed_store_not_in_spark_is_suggested_as_new(self):
        self._recap(typed_store_name="Spec's #88")
        suggestion, _ = rp.suggest_for_event(self.event, use_network=False)
        assert suggestion.name == "Spec's"
        assert suggestion.retailer_id is None and suggestion.source == "typed"

    def test_short_retailer_name_does_not_match_inside_another_word(self):
        Retailer.objects.create(name="HEB", tenant=self.tenant, created_by=self.system_user)
        self.event.name = "Thebes Market sampling"
        self.event.save(update_fields=["name"])
        suggestion, _ = rp.suggest_for_event(self.event, use_network=False)
        assert suggestion is None

    def test_nothing_to_go_on_stores_a_none_row_and_is_not_asked_again(self):
        self._recap()
        with mock.patch.object(rp, "places_lookup", return_value=None) as places, mock.patch.object(
            rp, "ai_suggestion", return_value=None
        ) as ai:
            call_command("propose_recap_retailers")
            call_command("propose_recap_retailers")
        assert places.call_count == 1 and ai.call_count == 1
        row = recap_models.RetailerProposal.objects.get(event=self.event)
        assert row.status == "none"

    def test_ai_suggestion_is_used_last_and_never_marked_high(self):
        self._recap()
        with mock.patch.object(rp, "places_lookup", return_value=None), mock.patch(
            "utils.ai_text.generate_json",
            return_value={"retailer": "kroger", "confidence": "high", "reason": "Named."},
        ):
            call_command("propose_recap_retailers")
        row = recap_models.RetailerProposal.objects.get(event=self.event)
        assert row.status == "pending" and row.source == "ai"
        assert row.proposed_name == "Kroger" and row.retailer_id == self.kroger.id
        assert row.confidence == "medium"

    def test_places_status_reports_a_refused_key_without_printing_it(self):
        class _Resp:
            status_code = 403

            def json(self):
                return {"error": {"message": "Places API (New) has not been used; key SECRETKEY"}}

        with mock.patch("utils.map_matching._google_maps_api_key", return_value="SECRETKEY"), mock.patch(
            "requests.post", return_value=_Resp()
        ):
            line = rp.places_status()
        assert "not available (HTTP 403)" in line and "SECRETKEY" not in line

    def test_event_that_already_has_a_retailer_is_skipped(self):
        self._recap()
        self._sheet("Kroger")
        self.event.retailer = self.kroger
        self.event.save(update_fields=["retailer"])
        call_command("propose_recap_retailers", "--no-network")
        assert not recap_models.RetailerProposal.objects.exists()

    def test_event_without_a_filed_recap_is_skipped(self):
        self._sheet("Kroger")
        call_command("propose_recap_retailers", "--no-network")
        assert not recap_models.RetailerProposal.objects.exists()

    def test_dry_run_stores_nothing(self):
        self._recap()
        self._sheet("Kroger")
        call_command("propose_recap_retailers", "--no-network", "--dry-run")
        assert not recap_models.RetailerProposal.objects.exists()

    # ---- through the schema -------------------------------------------------

    def _pending(self):
        recap = self._recap()
        self._sheet("Kroger")
        call_command("propose_recap_retailers", "--no-network")
        return recap, recap_models.RetailerProposal.objects.get(event=self.event)

    @pytest.mark.asyncio
    async def test_staff_see_the_suggestion_and_a_brand_user_does_not(self):
        recap, _ = await sync_to_async(self._pending)()
        result = await self._execute_query_authenticated(
            CUSTOM_RECAP_QUERY, {"id": str(recap.id)}, self.admin, self.endpoint_path
        )
        assert result.errors is None, result.errors
        got = result.data["customRecap"]["retailerProposal"]
        assert got["name"] == "Kroger" and got["isNewRetailer"] is False
        assert got["sourceLabel"] == "the staffing sheet"

        client = await sync_to_async(self.create_user)(
            username="client-rp", email="client-rp@brand.com", role=self.roles["client"]
        )
        await sync_to_async(self.create_tenanted_user)(client, self.tenant)
        # A brand user only sees an approved, shared recap; the suggestion is
        # still pending on it and must stay hidden.
        from django.utils import timezone

        await sync_to_async(
            recap_models.CustomRecap.objects.filter(id=recap.id).update
        )(approved=True, shared_at=timezone.now())
        result = await self._execute_query_authenticated(
            CUSTOM_RECAP_QUERY, {"id": str(recap.id)}, client, self.endpoint_path
        )
        assert result.errors is None, result.errors
        assert result.data["customRecap"] is not None
        assert result.data["customRecap"]["retailerProposal"] is None

    @pytest.mark.asyncio
    async def test_approving_with_the_box_ticked_sets_the_retailer(self):
        recap, proposal = await sync_to_async(self._pending)()
        result = await self._execute_mutation_authenticated(
            APPROVE_CUSTOM, {"id": str(recap.id), "accept": True}, self.admin, self.endpoint_path
        )
        assert result.errors is None, result.errors
        assert result.data["approveCustomRecap"]["success"] is True
        await sync_to_async(self.event.refresh_from_db)()
        await sync_to_async(recap.refresh_from_db)()
        await sync_to_async(proposal.refresh_from_db)()
        assert self.event.retailer_id == self.kroger.id
        assert recap.retailer_id == self.kroger.id and recap.approved is True
        assert proposal.status == "accepted" and proposal.decided_by_id == self.admin.id

    @pytest.mark.asyncio
    async def test_approving_without_the_box_leaves_the_suggestion_pending(self):
        recap, proposal = await sync_to_async(self._pending)()
        result = await self._execute_mutation_authenticated(
            APPROVE_CUSTOM, {"id": str(recap.id), "accept": None}, self.admin, self.endpoint_path
        )
        assert result.data["approveCustomRecap"]["success"] is True
        await sync_to_async(self.event.refresh_from_db)()
        await sync_to_async(proposal.refresh_from_db)()
        assert self.event.retailer_id is None and proposal.status == "pending"

    @pytest.mark.asyncio
    async def test_accepting_with_a_corrected_name_creates_that_retailer(self):
        _, proposal = await sync_to_async(self._pending)()
        result = await self._execute_mutation_authenticated(
            DECIDE,
            {"id": str(proposal.uuid), "accept": True, "name": "Total Wine"},
            self.admin,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        assert result.data["decideRetailerProposal"]["retailerName"] == "Total Wine"
        await sync_to_async(self.event.refresh_from_db)()
        made = await sync_to_async(Retailer.objects.get)(tenant=self.tenant, name="Total Wine")
        assert self.event.retailer_id == made.id

    @pytest.mark.asyncio
    async def test_dismissing_changes_nothing_and_a_brand_user_cannot_decide(self):
        _, proposal = await sync_to_async(self._pending)()
        client = await sync_to_async(self.create_user)(
            username="client-rp2", email="client-rp2@brand.com", role=self.roles["client"]
        )
        await sync_to_async(self.create_tenanted_user)(client, self.tenant)
        result = await self._execute_mutation_authenticated(
            DECIDE, {"id": str(proposal.uuid), "accept": True, "name": None}, client, self.endpoint_path
        )
        assert result.data["decideRetailerProposal"]["success"] is False

        result = await self._execute_mutation_authenticated(
            DECIDE, {"id": str(proposal.uuid), "accept": False, "name": None}, self.admin, self.endpoint_path
        )
        assert result.data["decideRetailerProposal"]["success"] is True
        await sync_to_async(self.event.refresh_from_db)()
        await sync_to_async(proposal.refresh_from_db)()
        assert self.event.retailer_id is None and proposal.status == "dismissed"
