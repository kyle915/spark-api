"""Archive parks a recap without deleting it or making it client-visible."""

import pytest
from datetime import datetime, timedelta, timezone as _tz

from asgiref.sync import sync_to_async
from django.utils import timezone as django_timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models
from recaps.mutations import _stamp_recap_archive, _stamp_recap_approval

ARCHIVE_RECAP = """
mutation ArchiveRecap($id: ID!, $reason: String) {
  archiveRecap(input: { id: $id, reason: $reason }) {
    success
    message
    recap { uuid approved archivedAt archiveReason }
  }
}
"""

UNARCHIVE_RECAP = """
mutation UnarchiveRecap($id: ID!) {
  unarchiveRecap(input: { id: $id }) {
    success
    message
    recap { uuid approved archivedAt }
  }
}
"""

ARCHIVE_CUSTOM = """
mutation ArchiveCustomRecap($id: ID!, $reason: String) {
  archiveCustomRecap(input: { id: $id, reason: $reason }) {
    success
    message
    customRecap { uuid approved archivedAt archiveReason }
  }
}
"""

UNARCHIVE_CUSTOM = """
mutation UnarchiveCustomRecap($id: ID!) {
  unarchiveCustomRecap(input: { id: $id }) {
    success
    message
    customRecap { uuid approved archivedAt }
  }
}
"""

RECAPS_QUERY = """
query Recaps($tenantId: ID, $approved: Boolean, $archived: Boolean, $first: Int) {
  recaps(
    filters: { tenantId: $tenantId, approved: $approved, archived: $archived }
    first: $first
  ) {
    totalCount
    edges { node { uuid name approved archivedAt } }
  }
}
"""

CUSTOM_RECAPS_QUERY = """
query CustomRecaps($tenantId: ID, $approved: Boolean, $archived: Boolean, $first: Int) {
  customRecaps(
    filters: { tenantId: $tenantId, approved: $approved, archived: $archived }
    first: $first
  ) {
    totalCount
    edges { node { uuid name approved archivedAt } }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestRecapArchive(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Torch Archive Test")
        self.spark_admin = self.create_user(
            username="admin-recap-archive",
            email="admin-recap-archive@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="client-recap-archive",
            email="client-recap-archive@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        now = datetime.now(_tz.utc)
        self.event = self.create_event(
            name="Whole Foods Archive",
            tenant=self.tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )
        self.event_type = self.create_event_type(
            name="Sampling", tenant=self.tenant
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Torch Template",
            event_type=self.event_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )

    def _make_recap(self, *, approved=False, name="Legacy recap"):
        return recap_models.Recap.objects.create(
            name=name,
            approved=approved,
            event=self.event,
            created_by=self.system_user,
            updated_by=self.system_user,
        )

    def _make_custom_recap(self, *, approved=False, name="Custom recap"):
        return recap_models.CustomRecap.objects.create(
            name=name,
            approved=approved,
            event=self.event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.system_user,
            updated_by=self.system_user,
        )

    @pytest.mark.asyncio
    async def test_archive_and_unarchive_legacy_recap(self):
        recap = await sync_to_async(self._make_recap)(approved=False)
        result = await self._execute_mutation_authenticated(
            ARCHIVE_RECAP,
            {"id": str(recap.id), "reason": "AI-edited photos"},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        assert result.data["archiveRecap"]["success"] is True
        assert result.data["archiveRecap"]["recap"]["archivedAt"] is not None
        assert (
            result.data["archiveRecap"]["recap"]["archiveReason"]
            == "AI-edited photos"
        )
        refreshed = await sync_to_async(recap_models.Recap.objects.get)(id=recap.id)
        assert refreshed.archived_at is not None
        assert refreshed.archived_by_id == self.spark_admin.id
        assert refreshed.approved is False

        restore = await self._execute_mutation_authenticated(
            UNARCHIVE_RECAP,
            {"id": str(recap.id)},
            self.spark_admin,
            self.endpoint_path,
        )
        assert restore.errors is None, restore.errors
        assert restore.data["unarchiveRecap"]["success"] is True
        refreshed = await sync_to_async(recap_models.Recap.objects.get)(id=recap.id)
        assert refreshed.archived_at is None
        assert refreshed.archived_by_id is None
        assert refreshed.archive_reason == ""

    @pytest.mark.asyncio
    async def test_archive_and_unarchive_custom_recap(self):
        recap = await sync_to_async(self._make_custom_recap)(approved=False)
        result = await self._execute_mutation_authenticated(
            ARCHIVE_CUSTOM,
            {"id": str(recap.id), "reason": "parked"},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        assert result.data["archiveCustomRecap"]["success"] is True
        refreshed = await sync_to_async(recap_models.CustomRecap.objects.get)(
            id=recap.id
        )
        assert refreshed.archived_at is not None
        assert refreshed.archived_by_id == self.spark_admin.id

        restore = await self._execute_mutation_authenticated(
            UNARCHIVE_CUSTOM,
            {"id": str(recap.id)},
            self.spark_admin,
            self.endpoint_path,
        )
        assert restore.errors is None, restore.errors
        refreshed = await sync_to_async(recap_models.CustomRecap.objects.get)(
            id=recap.id
        )
        assert refreshed.archived_at is None

    @pytest.mark.asyncio
    async def test_needs_review_excludes_archived(self):
        active = await sync_to_async(self._make_recap)(
            approved=False, name="Needs review"
        )
        archived = await sync_to_async(self._make_recap)(
            approved=False, name="Archived draft"
        )
        archived.archived_at = django_timezone.now()
        archived.archived_by = self.spark_admin
        await sync_to_async(archived.save)(
            update_fields=["archived_at", "archived_by"]
        )

        result = await self._execute_query_authenticated(
            RECAPS_QUERY,
            {
                "tenantId": str(self.tenant.id),
                "approved": False,
                "archived": False,
                "first": 50,
            },
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        names = {e["node"]["name"] for e in result.data["recaps"]["edges"]}
        assert "Needs review" in names
        assert "Archived draft" not in names
        assert active.uuid  # silence unused

        archived_tab = await self._execute_query_authenticated(
            RECAPS_QUERY,
            {
                "tenantId": str(self.tenant.id),
                "archived": True,
                "first": 50,
            },
            self.spark_admin,
            self.endpoint_path,
        )
        assert archived_tab.errors is None, archived_tab.errors
        archived_names = {
            e["node"]["name"] for e in archived_tab.data["recaps"]["edges"]
        }
        assert "Archived draft" in archived_names
        assert "Needs review" not in archived_names

    @pytest.mark.asyncio
    async def test_client_cannot_see_archived_even_if_approved(self):
        """Defense in depth: archived never client-visible."""
        approved = await sync_to_async(self._make_custom_recap)(
            approved=True, name="Client OK"
        )
        archived = await sync_to_async(self._make_custom_recap)(
            approved=True, name="Archived approved"
        )
        archived.archived_at = django_timezone.now()
        await sync_to_async(archived.save)(update_fields=["archived_at"])

        result = await self._execute_query_authenticated(
            CUSTOM_RECAPS_QUERY,
            {"tenantId": str(self.tenant.id), "first": 50},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        names = {e["node"]["name"] for e in result.data["customRecaps"]["edges"]}
        assert "Client OK" in names
        assert "Archived approved" not in names
        assert approved.uuid

    def test_approve_clears_archive(self):
        recap = self._make_recap(approved=False)
        _stamp_recap_archive(
            recap, archived=True, actor=self.spark_admin, reason="park"
        )
        assert recap.archived_at is not None
        _stamp_recap_approval(recap, approved=True, actor=self.spark_admin)
        assert recap.approved is True
        assert recap.archived_at is None
        assert recap.archive_reason == ""
