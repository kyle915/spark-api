"""Recaps list `ambassadorName` filter.

The web Recaps BA control used to be a dropdown populated from the
50-cap `ambassadors` resolver, so most of the roster never appeared.
Typing a BA name filters recaps server-side by linked Ambassador
first/last/full name and by write-in `external_ba_name`.
"""

from datetime import datetime, timedelta, timezone as _tz

import pytest

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models


RECAPS_QUERY = """
query Recaps($tenantId: ID, $ambassadorName: String, $first: Int) {
  recaps(
    filters: { tenantId: $tenantId, ambassadorName: $ambassadorName }
    first: $first
  ) {
    totalCount
    edges { node { uuid name } }
  }
}
"""

CUSTOM_RECAPS_QUERY = """
query CustomRecaps($tenantId: ID, $ambassadorName: String, $first: Int) {
  customRecaps(
    filters: { tenantId: $tenantId, ambassadorName: $ambassadorName }
    first: $first
  ) {
    totalCount
    edges { node { uuid name } }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestRecapAmbassadorNameFilter(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Liquid Death")
        self.spark_admin = self.create_user(
            username="admin-ba-name-filter",
            email="admin-ba-name-filter@test.com",
            role=self.roles["spark_admin"],
        )
        now = datetime.now(_tz.utc)
        self.event = self.create_event(
            name="7-Eleven 36016",
            tenant=self.tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=2),
        )
        self.event_type = self.create_event_type(
            name="Sampling", tenant=self.tenant
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="LD Template",
            event_type=self.event_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )

        vanita_user = self.create_user(
            username="vanita-khan",
            email="vanita@test.com",
            role=self.roles["ambassador"],
            first_name="Vanita",
            last_name="Khan",
        )
        alex_user = self.create_user(
            username="alex-other",
            email="alex@test.com",
            role=self.roles["ambassador"],
            first_name="Alex",
            last_name="Other",
        )
        self.vanita = self.create_ambassador(vanita_user)
        self.alex = self.create_ambassador(alex_user)

        self.vanita_legacy = recap_models.Recap.objects.create(
            name="Vanita legacy",
            approved=True,
            event=self.event,
            ambassador=self.vanita,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        recap_models.Recap.objects.create(
            name="Alex legacy",
            approved=True,
            event=self.event,
            ambassador=self.alex,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        recap_models.Recap.objects.create(
            name="Pat external legacy",
            approved=True,
            event=self.event,
            external_ba_name="Pat Helper",
            created_by=self.system_user,
            updated_by=self.system_user,
        )

        self.vanita_custom = recap_models.CustomRecap.objects.create(
            name="Vanita custom",
            approved=True,
            event=self.event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            ambassador=self.vanita,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        recap_models.CustomRecap.objects.create(
            name="Alex custom",
            approved=True,
            event=self.event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            ambassador=self.alex,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        recap_models.CustomRecap.objects.create(
            name="Pat external custom",
            approved=True,
            event=self.event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            external_ba_name="Pat Helper",
            created_by=self.system_user,
            updated_by=self.system_user,
        )

    async def _names(self, query: str, ambassador_name: str | None) -> set[str]:
        result = await self._execute_query_authenticated(
            query,
            {
                "tenantId": str(self.tenant.id),
                "ambassadorName": ambassador_name,
                "first": 50,
            },
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        key = "customRecaps" if "customRecaps(" in query else "recaps"
        conn = result.data[key]
        return {e["node"]["name"] for e in conn["edges"]}

    @pytest.mark.asyncio
    async def test_legacy_first_name_contains(self):
        names = await self._names(RECAPS_QUERY, "Vanita")
        assert names == {"Vanita legacy"}

    @pytest.mark.asyncio
    async def test_legacy_full_name_and_case(self):
        names = await self._names(RECAPS_QUERY, "vanita khan")
        assert names == {"Vanita legacy"}

    @pytest.mark.asyncio
    async def test_legacy_external_ba_name(self):
        names = await self._names(RECAPS_QUERY, "Pat")
        assert names == {"Pat external legacy"}

    @pytest.mark.asyncio
    async def test_custom_first_name_contains(self):
        names = await self._names(CUSTOM_RECAPS_QUERY, "Vanita")
        assert names == {"Vanita custom"}

    @pytest.mark.asyncio
    async def test_custom_external_ba_name(self):
        names = await self._names(CUSTOM_RECAPS_QUERY, "Helper")
        assert names == {"Pat external custom"}

    @pytest.mark.asyncio
    async def test_empty_name_does_not_narrow(self):
        names = await self._names(RECAPS_QUERY, "  ")
        assert names == {
            "Vanita legacy",
            "Alex legacy",
            "Pat external legacy",
        }
