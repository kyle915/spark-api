"""OpenAI / Places enrichment for 3rd-party Map-this-store suggestions."""

from datetime import datetime, timedelta, timezone as _tz
from unittest import mock

import pytest
from asgiref.sync import sync_to_async

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.models import Retailer
from recaps import models as recap_models
from recaps import store_automap as sa
from recaps.retailer_proposals import Suggestion

SUGGEST = """
mutation S($id: ID!) {
  suggestCustomRecapStore(input: { id: $id }) {
    success
    message
    customRecap {
      id
      storeMappingStatus
      storeSuggestions
    }
  }
}
"""

MAP = """
mutation M($id: ID!, $retailerId: ID!) {
  mapCustomRecapStore(input: { id: $id, retailerId: $retailerId }) {
    success
    message
    customRecap { storeMappingStatus }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestStoreAutomap(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Torch Automap")
        self.admin = self.create_user(
            username="admin-automap",
            email="admin-automap@igniteproductions.co",
            role=self.roles["spark_admin"],
            is_staff=True,
        )
        self.create_tenanted_user(self.admin, self.tenant)
        now = datetime.now(_tz.utc)
        self.event = self.create_event(
            name="Agency stop Kissimmee",
            tenant=self.tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )
        self.event.address = (
            "2334 E Irlo Bronson Memorial Hwy, Kissimmee, FL 34744, USA"
        )
        self.event.save(update_fields=["address"])
        event_type = self.create_event_type(name="Sampling", tenant=self.tenant)
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="T",
            event_type=event_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )
        self.chain = Retailer.objects.create(
            name="Total Wine & More",
            tenant=self.tenant,
            is_national=True,
            created_by=self.system_user,
        )
        self.other = Retailer.objects.create(
            name="Publix",
            tenant=self.tenant,
            is_national=True,
            created_by=self.system_user,
        )

    def _recap(self, **kw):
        defaults = dict(
            name="Recap",
            event=self.event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            submitted_at=datetime.now(_tz.utc),
            created_by=self.system_user,
            updated_by=self.system_user,
            is_third_party=True,
            typed_store_name=self.event.address,
            typed_store_address=self.event.address,
            store_mapping_status="unmatched",
            store_suggestions=[],
        )
        defaults.update(kw)
        return recap_models.CustomRecap.objects.create(**defaults)

    def test_confidence_for_score(self):
        assert sa.confidence_for_score(95) == "high"
        assert sa.confidence_for_score(80) == "high"
        assert sa.confidence_for_score(55) == "medium"
        assert sa.confidence_for_score(40) == "low"

    def test_heuristic_enrich_without_network_annotates_confidence(self):
        store = Retailer.objects.create(
            name="Total Wine & More",
            address=self.event.address,
            tenant=self.tenant,
            created_by=self.system_user,
        )
        hits = sa.enrich_store_suggestions(
            self.tenant,
            "Total Wine Kissimmee",
            self.event.address,
            use_network=False,
        )
        assert hits
        assert hits[0]["uuid"] == str(store.uuid)
        assert hits[0]["confidence"] == "high"
        assert hits[0]["source"] == "heuristic"

    def test_places_maps_address_to_national_chain(self):
        place = Suggestion(
            name="Total Wine & More",
            source="places",
            confidence="medium",
            reason="Google Maps lists Total Wine & More",
        )
        with mock.patch(
            "recaps.retailer_proposals.places_lookup", return_value=place
        ), mock.patch(
            "recaps.store_automap.ai_enrich", return_value=[]
        ):
            hits = sa.enrich_store_suggestions(
                self.tenant,
                self.event.address,
                self.event.address,
                use_network=True,
            )
        uuids = {h["uuid"] for h in hits}
        assert str(self.chain.uuid) in uuids
        top = next(h for h in hits if h["uuid"] == str(self.chain.uuid))
        assert top["source"] == "places"
        assert top["confidence"] == "high"
        assert "national" in top["reason"].lower() or "Maps" in top["reason"]

    def test_ai_maps_typed_name_to_catalog_chain(self):
        with mock.patch(
            "recaps.store_automap.places_enrich", return_value=[]
        ), mock.patch(
            "utils.ai_text.generate_json",
            return_value={
                "matches": [
                    {
                        "retailer": "Total Wine & More",
                        "confidence": "high",
                        "reason": "Address is a Total Wine location.",
                        "kind": "chain",
                    }
                ]
            },
        ):
            hits = sa.enrich_store_suggestions(
                self.tenant,
                "",
                self.event.address,
                use_network=True,
            )
        assert hits
        assert hits[0]["uuid"] == str(self.chain.uuid)
        assert hits[0]["source"] == "ai"
        # Model "high" is capped to medium unless Places also agrees.
        assert hits[0]["confidence"] == "medium"

    def test_strong_heuristic_skips_network(self):
        Retailer.objects.create(
            name="Total Wine & More",
            address=self.event.address,
            tenant=self.tenant,
            created_by=self.system_user,
        )
        with mock.patch(
            "recaps.store_automap.places_enrich"
        ) as places, mock.patch("recaps.store_automap.ai_enrich") as ai:
            hits = sa.enrich_store_suggestions(
                self.tenant,
                "Total Wine",
                self.event.address,
                use_network=True,
            )
        assert hits and hits[0]["score"] >= 80
        places.assert_not_called()
        ai.assert_not_called()

    @pytest.mark.asyncio
    async def test_suggest_mutation_staff_only_and_persists(self):
        recap = await sync_to_async(self._recap)()
        place = Suggestion(
            name="Total Wine & More",
            source="places",
            confidence="medium",
            reason="listed",
        )
        with mock.patch(
            "recaps.retailer_proposals.places_lookup", return_value=place
        ), mock.patch("recaps.store_automap.ai_enrich", return_value=[]):
            result = await self._execute_query_authenticated(
                SUGGEST, {"id": str(recap.id)}, self.admin, self.endpoint_path
            )
        assert result.errors is None, result.errors
        payload = result.data["suggestCustomRecapStore"]
        assert payload["success"] is True
        suggestions = payload["customRecap"]["storeSuggestions"]
        assert isinstance(suggestions, list) and suggestions
        assert suggestions[0]["uuid"] == str(self.chain.uuid)
        await sync_to_async(recap.refresh_from_db)()
        assert recap.store_mapping_status == "unmatched"
        assert recap.store_suggestions[0]["uuid"] == str(self.chain.uuid)

    @pytest.mark.asyncio
    async def test_suggest_mutation_rejects_non_admin(self):
        user = await sync_to_async(self.create_user)(
            username="brand-client-automap",
            email="brand-client@acme.test",
            role=self.roles["client"],
        )
        await sync_to_async(self.create_tenanted_user)(user, self.tenant)
        recap = await sync_to_async(self._recap)()
        result = await self._execute_query_authenticated(
            SUGGEST, {"id": str(recap.id)}, user, self.endpoint_path
        )
        assert result.errors is None, result.errors
        payload = result.data["suggestCustomRecapStore"]
        assert payload["success"] is False
        assert "staff" in (payload["message"] or "").lower()

    @pytest.mark.asyncio
    async def test_map_still_confirms_human_choice(self):
        recap = await sync_to_async(self._recap)(
            store_suggestions=[
                {
                    "uuid": str(self.chain.uuid),
                    "name": self.chain.name,
                    "score": 78,
                    "confidence": "high",
                    "reason": "Maps lists Total Wine",
                    "kind": "chain",
                    "isNational": True,
                    "source": "places",
                }
            ]
        )
        result = await self._execute_query_authenticated(
            MAP,
            {"id": str(recap.id), "retailerId": str(self.chain.uuid)},
            self.admin,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        assert result.data["mapCustomRecapStore"]["success"] is True
        await sync_to_async(recap.refresh_from_db)()
        assert recap.store_mapping_status == "mapped"
        assert recap.retailer_id == self.chain.id
