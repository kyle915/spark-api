"""
Regression: explicit totalEngagements must survive updateCustomRecap when
customFieldValues are also sent.

RecapCustomView always round-trips every template field on Save. After
writing those values, update_custom_recap used to re-derive
total_engagements from a matching "consumers interacted/sampled" custom
field and OVERWRITE the Metrics number the admin just typed. Soft refresh
still showed the old count because the DB never kept the new one
(Brew Dr Event Activation: 7500 → stuck at 4300).
"""

from datetime import datetime, timedelta, timezone as _tz

import pytest
from asgiref.sync import sync_to_async

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models


UPDATE_CUSTOM_RECAP_WITH_ENGAGEMENTS = """
mutation UpdateCustomRecap(
  $id: ID!
  $eventId: ID!
  $templateId: ID!
  $name: String!
  $totalEngagements: Int
  $customFieldValues: [CustomFieldValueInput!]
) {
  updateCustomRecap(
    input: {
      id: $id
      eventId: $eventId
      customRecapTemplateId: $templateId
      name: $name
      totalEngagements: $totalEngagements
      customFieldValues: $customFieldValues
    }
  ) {
    success
    message
    customRecap {
      uuid
      totalEngagements
    }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestExplicitTotalEngagementsWins(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()

        self.tenant = self.create_tenant(name="Brew Dr. Kombucha")
        self.spark_admin = self.create_user(
            username="admin-te-persist",
            email="admin-te-persist@test.com",
            role=self.roles["spark_admin"],
        )

        now = datetime.now(_tz.utc)
        self.event = self.create_event(
            name="Southeast Water Avenue",
            tenant=self.tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )
        self.event_type = self.create_event_type(
            name="Event Activation", tenant=self.tenant
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Brew Dr Event Activation",
            event_type=self.event_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )
        self.number_ft, _ = recap_models.CustomRecapFieldType.objects.get_or_create(
            name="number",
            defaults={"created_by": self.system_user},
        )
        self.section = recap_models.RecapSection.objects.create(
            tenant=self.tenant,
            name="Metrics",
            created_by=self.system_user,
        )
        # Wording that matches _CONSUMERS_SAMPLED_RE — the overwrite source.
        self.interact_field = recap_models.CustomField.objects.create(
            name="How many consumers did you interact with?",
            custom_recap_template=self.template,
            custom_field_type=self.number_ft,
            recap_section=self.section,
            created_by=self.system_user,
        )

    def _make_recap(self, *, engagements: int, interact_value: str):
        recap = recap_models.CustomRecap.objects.create(
            name="OMSI Event",
            event=self.event,
            custom_recap_template=self.template,
            tenant=self.tenant,
            total_engagements=engagements,
            approved=True,
            created_by=self.system_user,
        )
        recap_models.CustomFieldValue.objects.create(
            custom_recap=recap,
            custom_field=self.interact_field,
            value=interact_value,
            created_by=self.system_user,
        )
        return recap

    @pytest.mark.asyncio
    async def test_explicit_total_engagements_not_overwritten_by_custom_field(self):
        recap = await sync_to_async(self._make_recap)(
            engagements=4300, interact_value="4300"
        )

        result = await self._execute_mutation_authenticated(
            UPDATE_CUSTOM_RECAP_WITH_ENGAGEMENTS,
            {
                "id": str(recap.id),
                "eventId": str(self.event.id),
                "templateId": str(self.template.id),
                "name": recap.name,
                "totalEngagements": 7500,
                "customFieldValues": [
                    {
                        "customFieldId": str(self.interact_field.id),
                        "customFieldValueId": None,
                        "value": "4300",
                    }
                ],
            },
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["updateCustomRecap"]
        assert payload["success"] is True, payload
        assert payload["customRecap"]["totalEngagements"] == 7500

        refreshed = await sync_to_async(recap_models.CustomRecap.objects.get)(
            id=recap.id
        )
        assert refreshed.total_engagements == 7500

    @pytest.mark.asyncio
    async def test_omitted_total_engagements_still_backfills_from_custom_field(self):
        """When Metrics is omitted, keep auto-derive from consumers field."""
        recap = await sync_to_async(self._make_recap)(
            engagements=None, interact_value="120"
        )

        result = await self._execute_mutation_authenticated(
            UPDATE_CUSTOM_RECAP_WITH_ENGAGEMENTS,
            {
                "id": str(recap.id),
                "eventId": str(self.event.id),
                "templateId": str(self.template.id),
                "name": recap.name,
                # totalEngagements omitted (null) → backfill from custom field
                "totalEngagements": None,
                "customFieldValues": [
                    {
                        "customFieldId": str(self.interact_field.id),
                        "customFieldValueId": None,
                        "value": "999",
                    }
                ],
            },
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["updateCustomRecap"]
        assert payload["success"] is True, payload
        assert payload["customRecap"]["totalEngagements"] == 999

        refreshed = await sync_to_async(recap_models.CustomRecap.objects.get)(
            id=recap.id
        )
        assert refreshed.total_engagements == 999
