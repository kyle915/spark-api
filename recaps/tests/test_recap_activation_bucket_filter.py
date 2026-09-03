"""Recaps list `activationBucket` filter.

Mirrors Insights / CONV activation-type buckets so Retail / Event chips
page correctly under the 50-row list ceiling. Retail includes On-premise.
"""

from datetime import datetime, timedelta, timezone as _tz

import pytest

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models


RECAPS_QUERY = """
query Recaps($tenantId: ID, $activationBucket: String, $first: Int) {
  recaps(
    filters: { tenantId: $tenantId, activationBucket: $activationBucket }
    first: $first
  ) {
    totalCount
    edges { node { uuid name } }
  }
}
"""

CUSTOM_RECAPS_QUERY = """
query CustomRecaps($tenantId: ID, $activationBucket: String, $first: Int) {
  customRecaps(
    filters: { tenantId: $tenantId, activationBucket: $activationBucket }
    first: $first
  ) {
    totalCount
    edges { node { uuid name } }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestRecapActivationBucketFilter(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Activation Filter Co")
        self.spark_admin = self.create_user(
            username="admin-activation-filter",
            email="admin-activation-filter@test.com",
            role=self.roles["spark_admin"],
        )
        now = datetime.now(_tz.utc)
        self.location = self.create_location(
            name="HQ", code="HQ", zip_code="94105", tenant=self.tenant
        )
        self.client_row = self.create_client(
            name="Brand", email="brand@example.com", tenant=self.tenant
        )
        self.distributor = self.create_distributor(
            name="Distro",
            email="distro@example.com",
            location=self.location,
            tenant=self.tenant,
        )
        self.retailer = self.create_retailer(
            name="Retailer",
            address="1 Main",
            store_contact="mgr",
            location=self.location,
            tenant=self.tenant,
        )
        self.status_done = self.create_request_status(
            name="Done", tenant=self.tenant, create_event=True
        )
        self.type_retail = self.create_request_type(
            "Retail Sampling", self.tenant
        )
        self.type_onprem = self.create_request_type(
            "On-Premise Sampling", self.tenant
        )
        self.type_event = self.create_request_type(
            "Event Activation", self.tenant
        )
        self.type_other = self.create_request_type("Misc Ops", self.tenant)

        def _event_for(rtype, name):
            req = self.create_request(
                name=name,
                date=now,
                address="1 Main",
                client=self.client_row,
                distributor=self.distributor,
                retailer=self.retailer,
                request_type=rtype,
                tenant=self.tenant,
                status=self.status_done,
            )
            return self.create_event(
                name=name,
                tenant=self.tenant,
                date=now,
                start_time=now,
                end_time=now + timedelta(hours=2),
                request=req,
            )

        retail_ev = _event_for(self.type_retail, "Retail shift")
        onprem_ev = _event_for(self.type_onprem, "Onprem shift")
        event_ev = _event_for(self.type_event, "Event shift")
        other_ev = _event_for(self.type_other, "Other shift")

        for ev, label in (
            (retail_ev, "legacy-retail"),
            (onprem_ev, "legacy-onprem"),
            (event_ev, "legacy-event"),
            (other_ev, "legacy-other"),
        ):
            recap_models.Recap.objects.create(
                name=label,
                approved=True,
                event=ev,
                created_by=self.system_user,
                updated_by=self.system_user,
            )

        et = self.create_event_type(name="Sampling", tenant=self.tenant)
        for name, label in (
            ("Retail Sampling Template", "custom-retail"),
            ("On-Premise Bar Night", "custom-onprem"),
            ("Festival Activation", "custom-event"),
            ("Demo Misc", "custom-other"),
        ):
            tmpl = recap_models.CustomRecapTemplate.objects.create(
                name=name,
                event_type=et,
                tenant=self.tenant,
                created_by=self.system_user,
            )
            recap_models.CustomRecap.objects.create(
                name=label,
                approved=True,
                event=retail_ev,
                tenant=self.tenant,
                custom_recap_template=tmpl,
                created_by=self.system_user,
                updated_by=self.system_user,
            )

    async def _names(self, query: str, activation_bucket: str | None) -> set[str]:
        result = await self._execute_query_authenticated(
            query,
            {
                "tenantId": str(self.tenant.id),
                "activationBucket": activation_bucket,
                "first": 50,
            },
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        key = "customRecaps" if "customRecaps(" in query else "recaps"
        return {edge["node"]["name"] for edge in result.data[key]["edges"]}

    @pytest.mark.asyncio
    async def test_legacy_retail_includes_onprem_excludes_event(self):
        names = await self._names(RECAPS_QUERY, "retail")
        assert names == {"legacy-retail", "legacy-onprem"}

    @pytest.mark.asyncio
    async def test_legacy_event_excludes_retail_and_other(self):
        names = await self._names(RECAPS_QUERY, "event")
        assert names == {"legacy-event"}

    @pytest.mark.asyncio
    async def test_legacy_null_bucket_returns_all(self):
        names = await self._names(RECAPS_QUERY, None)
        assert names == {
            "legacy-retail",
            "legacy-onprem",
            "legacy-event",
            "legacy-other",
        }

    @pytest.mark.asyncio
    async def test_custom_retail_includes_onprem(self):
        names = await self._names(CUSTOM_RECAPS_QUERY, "retail")
        assert names == {"custom-retail", "custom-onprem"}

    @pytest.mark.asyncio
    async def test_custom_event_only(self):
        names = await self._names(CUSTOM_RECAPS_QUERY, "event")
        assert names == {"custom-event"}
