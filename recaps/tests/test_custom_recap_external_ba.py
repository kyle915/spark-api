"""
Coverage for the custom-recap write-in BA (external_ba_name) on the
clients schema.

The Girl Beer custom-recap "FILLING FOR A BA?" picker can record a worker
who isn't in Spark yet via a free-text name. This mirrors the legacy Recap
model's external_ba_name. Rules under test:

  • createCustomRecap with externalBaName (and no ambassador) persists the
    typed name and exposes it on the CustomRecap type.
  • If BOTH ambassadorId and externalBaName are sent, the ambassador FK
    wins and the write-in is cleared (a real Spark BA takes precedence).
  • A blank/whitespace externalBaName stores NULL, not "".

Runs end to end against the real `schema_clients` GraphQL surface.
"""

import pytest
from datetime import datetime, timedelta, timezone as _tz

from asgiref.sync import sync_to_async

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models


CREATE_CUSTOM_RECAP_EXTERNAL = """
mutation CreateCustomRecap(
  $eventId: ID!
  $templateId: ID!
  $name: String!
  $externalBaName: String
  $ambassadorId: ID
) {
  createCustomRecap(
    input: {
      eventId: $eventId
      customRecapTemplateId: $templateId
      name: $name
      externalBaName: $externalBaName
      ambassadorId: $ambassadorId
    }
  ) {
    success
    message
    customRecap {
      uuid
      name
      externalBaName
      ambassador { uuid }
    }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestCustomRecapExternalBaName(AmbassadorsGraphQLTestCase):
    """createCustomRecap external_ba_name (write-in BA)."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()

        self.tenant = self.create_tenant(name="Girl Beer")

        self.spark_admin = self.create_user(
            username="admin-external-ba",
            email="admin-external-ba@test.com",
            role=self.roles["spark_admin"],
        )

        now = datetime.now(_tz.utc)
        self.event = self.create_event(
            name="Whole Foods Burbank",
            tenant=self.tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )
        self.event_type = self.create_event_type(
            name="Sampling", tenant=self.tenant
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="GB Template",
            event_type=self.event_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )

    @pytest.mark.asyncio
    async def test_external_ba_name_persisted_when_no_ambassador(self):
        result = await self._execute_mutation_authenticated(
            CREATE_CUSTOM_RECAP_EXTERNAL,
            {
                "eventId": str(self.event.id),
                "templateId": str(self.template.id),
                "name": "Sub recap",
                "externalBaName": "Jane Subcontractor",
            },
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["createCustomRecap"]
        assert payload["success"] is True, payload
        assert payload["customRecap"]["externalBaName"] == "Jane Subcontractor"
        assert payload["customRecap"]["ambassador"] is None

        # Persisted on the row.
        row = await sync_to_async(recap_models.CustomRecap.objects.get)(
            uuid=payload["customRecap"]["uuid"]
        )
        assert row.external_ba_name == "Jane Subcontractor"
        assert row.ambassador_id is None

    @pytest.mark.asyncio
    async def test_ambassador_wins_over_external_ba_name(self):
        ba_user = await sync_to_async(self.create_user)(
            username="ba-wins",
            email="ba-wins@test.com",
            role=self.roles["ambassador"],
        )
        ambassador = await sync_to_async(self.create_ambassador)(user=ba_user)
        from strawberry.relay import to_base64

        result = await self._execute_mutation_authenticated(
            CREATE_CUSTOM_RECAP_EXTERNAL,
            {
                "eventId": str(self.event.id),
                "templateId": str(self.template.id),
                "name": "Both sent recap",
                "externalBaName": "Should Be Ignored",
                "ambassadorId": to_base64("Ambassador", ambassador.id),
            },
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["createCustomRecap"]
        assert payload["success"] is True, payload
        # FK wins → write-in cleared.
        assert payload["customRecap"]["externalBaName"] is None
        assert payload["customRecap"]["ambassador"]["uuid"] == str(
            ambassador.uuid
        )

        row = await sync_to_async(recap_models.CustomRecap.objects.get)(
            uuid=payload["customRecap"]["uuid"]
        )
        assert row.external_ba_name is None
        assert row.ambassador_id == ambassador.id

    @pytest.mark.asyncio
    async def test_blank_external_ba_name_stored_as_null(self):
        result = await self._execute_mutation_authenticated(
            CREATE_CUSTOM_RECAP_EXTERNAL,
            {
                "eventId": str(self.event.id),
                "templateId": str(self.template.id),
                "name": "Blank write-in recap",
                "externalBaName": "   ",
            },
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["createCustomRecap"]
        assert payload["success"] is True, payload
        assert payload["customRecap"]["externalBaName"] is None

        row = await sync_to_async(recap_models.CustomRecap.objects.get)(
            uuid=payload["customRecap"]["uuid"]
        )
        assert row.external_ba_name is None
