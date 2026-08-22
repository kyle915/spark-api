"""shareRecapByEmail — the on-platform Share modal's email path.

Replaces the native OS share sheet: an admin/client types recipient
emails + an optional note and the API mails the public /r/:token link
(client host — never admin). Covers:

  * admin can email a legacy AND a custom recap link (shared_at stamped),
  * a same-tenant client can email an APPROVED recap,
  * a client canNOT email an unapproved recap (draft gate mirrors the
    read resolvers — clients never see drafts),
  * cross-tenant + ambassador callers are denied,
  * recipient validation (empty / malformed / dedup).
"""

from datetime import datetime, timedelta, timezone as _tz

import pytest
from asgiref.sync import sync_to_async
from django.core import mail

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models

SHARE_BY_EMAIL = """
mutation ShareByEmail(
  $recapId: ID
  $customRecapId: ID
  $recipients: [String!]!
  $message: String
) {
  shareRecapByEmail(input: {
    recapId: $recapId
    customRecapId: $customRecapId
    recipients: $recipients
    message: $message
  }) {
    success
    message
    shareUrl
    sentCount
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestShareRecapByEmail(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Share Tenant")
        self.other_tenant = self.create_tenant(name="Other Tenant")
        self.spark_admin = self.create_user(
            username="admin-share",
            email="admin-share@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="client-share",
            email="client-share@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        self.ba_user = self.create_user(
            username="ba-share",
            email="ba-share@test.com",
            role=self.roles["ambassador"],
        )
        now = datetime.now(_tz.utc)
        self.event = self.create_event(
            name="Share event",
            tenant=self.tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )
        self.other_event = self.create_event(
            name="Foreign share event",
            tenant=self.other_tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )
        self.event_type = self.create_event_type(
            name="Sampling", tenant=self.tenant
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Share Template",
            event_type=self.event_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )

    def _make_recap(self, event, approved=False):
        return recap_models.Recap.objects.create(
            name="Legacy recap",
            approved=approved,
            event=event,
            created_by=self.system_user,
            updated_by=self.system_user,
        )

    def _make_custom_recap(self, event, approved=False):
        return recap_models.CustomRecap.objects.create(
            name="Custom recap",
            approved=approved,
            event=event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.system_user,
            updated_by=self.system_user,
        )

    @pytest.mark.asyncio
    async def test_admin_can_share_legacy_recap(self):
        recap = await sync_to_async(self._make_recap)(self.event, approved=True)
        result = await self._execute_mutation_authenticated(
            SHARE_BY_EMAIL,
            {
                "recapId": str(recap.id),
                "recipients": ["Buyer@Example.com", "buyer@example.com"],
                "message": "Great activation!",
            },
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        payload = result.data["shareRecapByEmail"]
        assert payload["success"] is True, payload
        assert payload["sentCount"] == 1  # case-insensitive dedup
        assert "/r/" in payload["shareUrl"]
        assert "admin.igniteproductions.co" not in payload["shareUrl"]
        refreshed = await sync_to_async(recap_models.Recap.objects.get)(
            id=recap.id
        )
        assert refreshed.shared_at is not None
        # One recipient after dedup → one message, link in the HTML body.
        assert len(mail.outbox) == 1
        msg = mail.outbox[0]
        assert msg.to == ["Buyer@Example.com"]
        assert payload["shareUrl"] in msg.alternatives[0][0]
        assert "Great activation!" in msg.alternatives[0][0]

    @pytest.mark.asyncio
    async def test_admin_can_share_custom_recap(self):
        recap = await sync_to_async(self._make_custom_recap)(
            self.event, approved=True
        )
        result = await self._execute_mutation_authenticated(
            SHARE_BY_EMAIL,
            {
                "customRecapId": str(recap.id),
                "recipients": ["one@example.com", "two@example.com"],
            },
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        payload = result.data["shareRecapByEmail"]
        assert payload["success"] is True, payload
        assert payload["sentCount"] == 2
        assert len(mail.outbox) == 2
        refreshed = await sync_to_async(recap_models.CustomRecap.objects.get)(
            id=recap.id
        )
        assert refreshed.shared_at is not None

    @pytest.mark.asyncio
    async def test_client_can_share_approved_own_tenant(self):
        recap = await sync_to_async(self._make_custom_recap)(
            self.event, approved=True
        )
        result = await self._execute_mutation_authenticated(
            SHARE_BY_EMAIL,
            {"customRecapId": str(recap.id), "recipients": ["c@example.com"]},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        payload = result.data["shareRecapByEmail"]
        assert payload["success"] is True, payload

    @pytest.mark.asyncio
    async def test_client_cannot_share_unapproved_recap(self):
        recap = await sync_to_async(self._make_custom_recap)(
            self.event, approved=False
        )
        result = await self._execute_mutation_authenticated(
            SHARE_BY_EMAIL,
            {"customRecapId": str(recap.id), "recipients": ["c@example.com"]},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        payload = result.data["shareRecapByEmail"]
        assert payload["success"] is False, payload
        assert "not found" in payload["message"].lower()
        assert len(mail.outbox) == 0

    @pytest.mark.asyncio
    async def test_admin_can_share_unapproved_recap(self):
        """Admins keep today's behavior — every recap is shareable."""
        recap = await sync_to_async(self._make_recap)(self.event, approved=False)
        result = await self._execute_mutation_authenticated(
            SHARE_BY_EMAIL,
            {"recapId": str(recap.id), "recipients": ["a@example.com"]},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        payload = result.data["shareRecapByEmail"]
        assert payload["success"] is True, payload

    @pytest.mark.asyncio
    async def test_client_cannot_share_other_tenant(self):
        recap = await sync_to_async(self._make_recap)(
            self.other_event, approved=True
        )
        result = await self._execute_mutation_authenticated(
            SHARE_BY_EMAIL,
            {"recapId": str(recap.id), "recipients": ["c@example.com"]},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        payload = result.data["shareRecapByEmail"]
        assert payload["success"] is False, payload
        assert "not found" in payload["message"].lower()
        assert len(mail.outbox) == 0

    @pytest.mark.asyncio
    async def test_ambassador_blocked(self):
        recap = await sync_to_async(self._make_recap)(self.event, approved=True)
        result = await self._execute_mutation_authenticated(
            SHARE_BY_EMAIL,
            {"recapId": str(recap.id), "recipients": ["c@example.com"]},
            self.ba_user,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        payload = result.data["shareRecapByEmail"]
        assert payload["success"] is False, payload
        assert len(mail.outbox) == 0

    @pytest.mark.asyncio
    async def test_invalid_email_rejected(self):
        recap = await sync_to_async(self._make_recap)(self.event, approved=True)
        result = await self._execute_mutation_authenticated(
            SHARE_BY_EMAIL,
            {"recapId": str(recap.id), "recipients": ["not-an-email"]},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        payload = result.data["shareRecapByEmail"]
        assert payload["success"] is False, payload
        assert "invalid email" in payload["message"].lower()
        assert len(mail.outbox) == 0

    @pytest.mark.asyncio
    async def test_empty_recipients_rejected(self):
        recap = await sync_to_async(self._make_recap)(self.event, approved=True)
        result = await self._execute_mutation_authenticated(
            SHARE_BY_EMAIL,
            {"recapId": str(recap.id), "recipients": []},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        payload = result.data["shareRecapByEmail"]
        assert payload["success"] is False, payload

    @pytest.mark.asyncio
    async def test_both_ids_rejected(self):
        recap = await sync_to_async(self._make_recap)(self.event, approved=True)
        custom = await sync_to_async(self._make_custom_recap)(
            self.event, approved=True
        )
        result = await self._execute_mutation_authenticated(
            SHARE_BY_EMAIL,
            {
                "recapId": str(recap.id),
                "customRecapId": str(custom.id),
                "recipients": ["c@example.com"],
            },
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        payload = result.data["shareRecapByEmail"]
        assert payload["success"] is False, payload
