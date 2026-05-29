"""
Coverage for two related recap behaviours on the clients schema:

  Bug #2 — DELETE a recap (both legacy `Recap` and `CustomRecap`).
    A tenant-scoped, admin-only delete mutation (mirrors the
    `delete_request` precedent). Ambassadors are blocked; a client-role
    user can only delete inside its own tenant; a spark-admin can delete
    anywhere. Deleting frees the event for a new recap and drops the row
    from every list.

  Bug #3 — MULTIPLE recaps per event.
    Old Spark let several BAs file recaps for the same event. There must
    be NO restriction blocking a second recap once one exists: the
    `recapEventOptions` picker must still surface the event, and a second
    `createCustomRecap` against the same event must succeed.

These run against the real `schema_clients` GraphQL surface (the same one
the web admin hits at /graphql/clients), end to end.
"""

import pytest
from datetime import datetime, timedelta, timezone as _tz

from asgiref.sync import sync_to_async

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models


DELETE_RECAP_MUTATION = """
mutation DeleteRecap($id: ID!) {
  deleteRecap(input: { id: $id }) {
    success
    message
    deletedRecapUuid
  }
}
"""

DELETE_CUSTOM_RECAP_MUTATION = """
mutation DeleteCustomRecap($id: ID!) {
  deleteCustomRecap(input: { id: $id }) {
    success
    message
    deletedCustomRecapUuid
  }
}
"""

RECAP_EVENT_OPTIONS_QUERY = """
query RecapEventOptions($tenantId: ID, $first: Int) {
  recapEventOptions(tenantId: $tenantId, first: $first) {
    totalCount
    edges { node { uuid name } }
  }
}
"""

CREATE_CUSTOM_RECAP_MUTATION = """
mutation CreateCustomRecap(
  $eventId: ID!
  $templateId: ID!
  $name: String!
) {
  createCustomRecap(
    input: {
      eventId: $eventId
      customRecapTemplateId: $templateId
      name: $name
    }
  ) {
    success
    message
    customRecap { uuid name event { uuid } }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestRecapDeleteAndMultiPerEvent(AmbassadorsGraphQLTestCase):
    """deleteRecap / deleteCustomRecap + multiple recaps per event."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()

        self.tenant = self.create_tenant(name="Girl Beer")
        self.other_tenant = self.create_tenant(name="Liquid Death")

        self.spark_admin = self.create_user(
            username="admin-recap-delete",
            email="admin-recap-delete@test.com",
            role=self.roles["spark_admin"],
        )
        # Client belongs to `self.tenant` only.
        self.client_user = self.create_user(
            username="client-recap-delete",
            email="client-recap-delete@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        # Ambassador (role 1) — must be blocked from deleting.
        self.ba_user = self.create_user(
            username="ba-recap-delete",
            email="ba-recap-delete@test.com",
            role=self.roles["ambassador"],
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

    # ─── Bug #2: delete legacy Recap ──────────────────────────────

    @pytest.mark.asyncio
    async def test_spark_admin_deletes_legacy_recap(self):
        recap = await sync_to_async(recap_models.Recap.objects.create)(
            name="Bad test recap",
            approved=False,
            event=self.event,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        recap_uuid = str(recap.uuid)

        result = await self._execute_mutation_authenticated(
            DELETE_RECAP_MUTATION,
            {"id": str(recap.id)},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["deleteRecap"]
        assert payload["success"] is True, payload
        assert payload["deletedRecapUuid"] == recap_uuid

        # Gone from the DB.
        exists = await sync_to_async(
            recap_models.Recap.objects.filter(id=recap.id).exists
        )()
        assert exists is False

    @pytest.mark.asyncio
    async def test_delete_legacy_recap_removes_child_rows(self):
        # A recap with RESTRICT-FK children must still delete cleanly.
        recap = await sync_to_async(recap_models.Recap.objects.create)(
            name="Recap with children",
            approved=False,
            event=self.event,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        await sync_to_async(
            recap_models.ConsumerEngagements.objects.create
        )(recap=recap, total_consumer=42, created_by=self.system_user)
        await sync_to_async(
            recap_models.ConsumerFeedback.objects.create
        )(recap=recap, feedback="great", created_by=self.system_user)
        await sync_to_async(
            recap_models.AccountFeedback.objects.create
        )(recap=recap, feedback="ok", created_by=self.system_user)

        result = await self._execute_mutation_authenticated(
            DELETE_RECAP_MUTATION,
            {"id": str(recap.id)},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        assert result.data["deleteRecap"]["success"] is True

        gone = not await sync_to_async(
            recap_models.Recap.objects.filter(id=recap.id).exists
        )()
        no_children = not await sync_to_async(
            recap_models.ConsumerEngagements.objects.filter(
                recap_id=recap.id
            ).exists
        )()
        assert gone and no_children

    @pytest.mark.asyncio
    async def test_ambassador_cannot_delete_recap(self):
        recap = await sync_to_async(recap_models.Recap.objects.create)(
            name="Protected recap",
            approved=False,
            event=self.event,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        result = await self._execute_mutation_authenticated(
            DELETE_RECAP_MUTATION,
            {"id": str(recap.id)},
            self.ba_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["deleteRecap"]
        assert payload["success"] is False
        assert "authorized" in payload["message"].lower()
        # Still in the DB.
        still = await sync_to_async(
            recap_models.Recap.objects.filter(id=recap.id).exists
        )()
        assert still is True

    # ─── Bug #2: delete CustomRecap ───────────────────────────────

    @pytest.mark.asyncio
    async def test_spark_admin_deletes_custom_recap_with_values(self):
        custom = await sync_to_async(
            recap_models.CustomRecap.objects.create
        )(
            name="Bad custom recap",
            approved=False,
            event=self.event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        # A field + a stored value (RESTRICT FK) so we exercise the
        # child-cleanup path.
        section = await sync_to_async(
            recap_models.RecapSection.objects.create
        )(name="Sales", tenant=self.tenant, created_by=self.system_user)
        ftype = await sync_to_async(
            recap_models.CustomRecapFieldType.objects.create
        )(name="number", created_by=self.system_user)
        field = await sync_to_async(
            recap_models.CustomField.objects.create
        )(
            name="Cans sold",
            custom_recap_template=self.template,
            custom_field_type=ftype,
            recap_section=section,
            created_by=self.system_user,
        )
        await sync_to_async(
            recap_models.CustomFieldValue.objects.create
        )(
            custom_recap=custom,
            custom_field=field,
            value="12",
            created_by=self.system_user,
        )
        custom_uuid = str(custom.uuid)

        result = await self._execute_mutation_authenticated(
            DELETE_CUSTOM_RECAP_MUTATION,
            {"id": str(custom.id)},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["deleteCustomRecap"]
        assert payload["success"] is True, payload
        assert payload["deletedCustomRecapUuid"] == custom_uuid

        gone = not await sync_to_async(
            recap_models.CustomRecap.objects.filter(id=custom.id).exists
        )()
        values_gone = not await sync_to_async(
            recap_models.CustomFieldValue.objects.filter(
                custom_recap_id=custom.id
            ).exists
        )()
        assert gone and values_gone

    @pytest.mark.asyncio
    async def test_client_cannot_delete_other_tenant_custom_recap(self):
        # A CustomRecap belonging to the FOREIGN tenant — our client_user
        # (member of self.tenant only) must not be able to delete it.
        other_event = await sync_to_async(self.create_event)(
            name="Foreign event",
            tenant=self.other_tenant,
            date=datetime.now(_tz.utc),
        )
        other_et = await sync_to_async(self.create_event_type)(
            name="Sampling", tenant=self.other_tenant
        )
        other_template = await sync_to_async(
            recap_models.CustomRecapTemplate.objects.create
        )(
            name="LD Template",
            event_type=other_et,
            tenant=self.other_tenant,
            created_by=self.system_user,
        )
        foreign = await sync_to_async(
            recap_models.CustomRecap.objects.create
        )(
            name="Foreign custom recap",
            approved=False,
            event=other_event,
            tenant=self.other_tenant,
            custom_recap_template=other_template,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        result = await self._execute_mutation_authenticated(
            DELETE_CUSTOM_RECAP_MUTATION,
            {"id": str(foreign.id)},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["deleteCustomRecap"]
        assert payload["success"] is False
        assert "authorized" in payload["message"].lower()
        still = await sync_to_async(
            recap_models.CustomRecap.objects.filter(id=foreign.id).exists
        )()
        assert still is True

    # ─── Bug #3: multiple recaps per event ────────────────────────

    @pytest.mark.asyncio
    async def test_event_with_recap_still_selectable_in_picker(self):
        # File a recap on the event, then confirm the event STILL shows
        # up in the recap-create picker (no recap-exclusion filter).
        await sync_to_async(recap_models.Recap.objects.create)(
            name="First recap",
            approved=False,
            event=self.event,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        result = await self._execute_query_authenticated(
            RECAP_EVENT_OPTIONS_QUERY,
            {"tenantId": str(self.tenant.id), "first": 50},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        names = {
            e["node"]["name"]
            for e in result.data["recapEventOptions"]["edges"]
        }
        assert "Whole Foods Burbank" in names

    @pytest.mark.asyncio
    async def test_second_custom_recap_on_same_event_allowed(self):
        # First custom recap on the event.
        await sync_to_async(
            recap_models.CustomRecap.objects.create
        )(
            name="BA #1 recap",
            approved=False,
            event=self.event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        # Second one via the real createCustomRecap mutation — old Spark
        # let several BAs file for the same event; this must succeed.
        result = await self._execute_mutation_authenticated(
            CREATE_CUSTOM_RECAP_MUTATION,
            {
                "eventId": str(self.event.id),
                "templateId": str(self.template.id),
                "name": "BA #2 recap",
            },
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["createCustomRecap"]
        assert payload["success"] is True, payload
        assert payload["customRecap"]["name"] == "BA #2 recap"

        count = await sync_to_async(
            recap_models.CustomRecap.objects.filter(event=self.event).count
        )()
        assert count == 2
