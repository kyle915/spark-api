"""shareRecapsByEmail — the Recaps list multi-select Share / Email action.

Bulk sibling of shareRecapByEmail (test_share_recap_by_email.py): the
sender picks N recaps on /recaps, types recipient emails + an optional
note, and the API sends ONE digest email per recipient listing every
recap's public /r/:token link (client host — never admin). Covers:

  * admin can bulk-email legacy AND custom recaps in one digest
    (shared_at stamped on every included recap),
  * a same-tenant client can bulk-email APPROVED recaps,
  * one unapproved recap in the batch fails the whole send for a
    client (draft gate mirrors the read resolvers),
  * cross-tenant + ambassador callers are denied,
  * recipient validation (empty / malformed / dedup) + batch caps.
"""

from datetime import datetime, timedelta, timezone as _tz

import pytest
from asgiref.sync import sync_to_async
from django.core import mail

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models

SHARE_BULK_BY_EMAIL = """
mutation ShareBulkByEmail(
  $recapIds: [ID!]
  $customRecapIds: [ID!]
  $recipients: [String!]!
  $message: String
) {
  shareRecapsByEmail(input: {
    recapIds: $recapIds
    customRecapIds: $customRecapIds
    recipients: $recipients
    message: $message
  }) {
    success
    message
    sentCount
    sharedCount
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestShareRecapsByEmail(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Bulk Share Tenant")
        self.other_tenant = self.create_tenant(name="Other Bulk Tenant")
        self.spark_admin = self.create_user(
            username="admin-bulk-share",
            email="admin-bulk-share@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="client-bulk-share",
            email="client-bulk-share@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        self.ba_user = self.create_user(
            username="ba-bulk-share",
            email="ba-bulk-share@test.com",
            role=self.roles["ambassador"],
        )
        now = datetime.now(_tz.utc)
        self.event = self.create_event(
            name="Bulk share event",
            tenant=self.tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )
        self.other_event = self.create_event(
            name="Foreign bulk share event",
            tenant=self.other_tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )
        self.event_type = self.create_event_type(
            name="Sampling", tenant=self.tenant
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Bulk Share Template",
            event_type=self.event_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )

    def _make_recap(self, event, approved=False, name="Legacy recap"):
        return recap_models.Recap.objects.create(
            name=name,
            approved=approved,
            event=event,
            created_by=self.system_user,
            updated_by=self.system_user,
        )

    def _make_custom_recap(self, event, approved=False, name="Custom recap"):
        return recap_models.CustomRecap.objects.create(
            name=name,
            approved=approved,
            event=event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.system_user,
            updated_by=self.system_user,
        )

    @pytest.mark.asyncio
    async def test_admin_bulk_shares_legacy_and_custom_one_digest(self):
        legacy = await sync_to_async(self._make_recap)(
            self.event, approved=True, name="Legacy one"
        )
        custom = await sync_to_async(self._make_custom_recap)(
            self.event, approved=True, name="Custom one"
        )
        result = await self._execute_mutation_authenticated(
            SHARE_BULK_BY_EMAIL,
            {
                "recapIds": [str(legacy.id)],
                "customRecapIds": [str(custom.id)],
                "recipients": ["Buyer@Example.com", "buyer@example.com"],
                "message": "Two great activations!",
            },
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        payload = result.data["shareRecapsByEmail"]
        assert payload["success"] is True, payload
        # Case-insensitive dedup → one recipient, one digest email.
        assert payload["sentCount"] == 1
        assert payload["sharedCount"] == 2
        assert len(mail.outbox) == 1
        msg = mail.outbox[0]
        assert msg.to == ["Buyer@Example.com"]
        html = msg.alternatives[0][0]
        # Both recap names + both /r/ links in the ONE email.
        assert "Legacy one" in html
        assert "Custom one" in html
        assert html.count("/r/") >= 2
        assert "Two great activations!" in html
        assert "admin.igniteproductions.co" not in html
        for model, pk in (
            (recap_models.Recap, legacy.id),
            (recap_models.CustomRecap, custom.id),
        ):
            refreshed = await sync_to_async(model.objects.get)(id=pk)
            assert refreshed.shared_at is not None

    @pytest.mark.asyncio
    async def test_digest_goes_to_each_recipient_once(self):
        legacy = await sync_to_async(self._make_recap)(self.event, approved=True)
        result = await self._execute_mutation_authenticated(
            SHARE_BULK_BY_EMAIL,
            {
                "recapIds": [str(legacy.id)],
                "recipients": ["one@example.com", "two@example.com"],
            },
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        payload = result.data["shareRecapsByEmail"]
        assert payload["success"] is True, payload
        assert payload["sentCount"] == 2
        assert payload["sharedCount"] == 1
        # One email per recipient — recipients never see each other.
        assert len(mail.outbox) == 2
        assert {m.to[0] for m in mail.outbox} == {
            "one@example.com",
            "two@example.com",
        }

    @pytest.mark.asyncio
    async def test_client_can_bulk_share_approved_own_tenant(self):
        legacy = await sync_to_async(self._make_recap)(self.event, approved=True)
        custom = await sync_to_async(self._make_custom_recap)(
            self.event, approved=True
        )
        result = await self._execute_mutation_authenticated(
            SHARE_BULK_BY_EMAIL,
            {
                "recapIds": [str(legacy.id)],
                "customRecapIds": [str(custom.id)],
                "recipients": ["c@example.com"],
            },
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        payload = result.data["shareRecapsByEmail"]
        assert payload["success"] is True, payload
        assert payload["sharedCount"] == 2

    @pytest.mark.asyncio
    async def test_client_unapproved_in_batch_fails_whole_send(self):
        approved = await sync_to_async(self._make_recap)(
            self.event, approved=True, name="Approved one"
        )
        draft = await sync_to_async(self._make_recap)(
            self.event, approved=False, name="Draft one"
        )
        result = await self._execute_mutation_authenticated(
            SHARE_BULK_BY_EMAIL,
            {
                "recapIds": [str(approved.id), str(draft.id)],
                "recipients": ["c@example.com"],
            },
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        payload = result.data["shareRecapsByEmail"]
        assert payload["success"] is False, payload
        assert "not found" in payload["message"].lower()
        assert len(mail.outbox) == 0
        # Nothing stamped — the batch is all-or-nothing.
        refreshed = await sync_to_async(recap_models.Recap.objects.get)(
            id=approved.id
        )
        assert refreshed.shared_at is None

    @pytest.mark.asyncio
    async def test_admin_can_bulk_share_unapproved(self):
        """Admins keep single-share behavior — every recap is shareable."""
        legacy = await sync_to_async(self._make_recap)(
            self.event, approved=False
        )
        result = await self._execute_mutation_authenticated(
            SHARE_BULK_BY_EMAIL,
            {
                "recapIds": [str(legacy.id)],
                "recipients": ["a@example.com"],
            },
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        payload = result.data["shareRecapsByEmail"]
        assert payload["success"] is True, payload

    @pytest.mark.asyncio
    async def test_client_cannot_bulk_share_other_tenant(self):
        foreign = await sync_to_async(self._make_recap)(
            self.other_event, approved=True
        )
        own = await sync_to_async(self._make_recap)(self.event, approved=True)
        result = await self._execute_mutation_authenticated(
            SHARE_BULK_BY_EMAIL,
            {
                "recapIds": [str(own.id), str(foreign.id)],
                "recipients": ["c@example.com"],
            },
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        payload = result.data["shareRecapsByEmail"]
        assert payload["success"] is False, payload
        assert "not found" in payload["message"].lower()
        assert len(mail.outbox) == 0

    @pytest.mark.asyncio
    async def test_ambassador_blocked(self):
        legacy = await sync_to_async(self._make_recap)(self.event, approved=True)
        result = await self._execute_mutation_authenticated(
            SHARE_BULK_BY_EMAIL,
            {
                "recapIds": [str(legacy.id)],
                "recipients": ["c@example.com"],
            },
            self.ba_user,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        payload = result.data["shareRecapsByEmail"]
        assert payload["success"] is False, payload
        assert len(mail.outbox) == 0

    @pytest.mark.asyncio
    async def test_invalid_email_rejected(self):
        legacy = await sync_to_async(self._make_recap)(self.event, approved=True)
        result = await self._execute_mutation_authenticated(
            SHARE_BULK_BY_EMAIL,
            {
                "recapIds": [str(legacy.id)],
                "recipients": ["not-an-email"],
            },
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        payload = result.data["shareRecapsByEmail"]
        assert payload["success"] is False, payload
        assert "invalid email" in payload["message"].lower()
        assert len(mail.outbox) == 0

    @pytest.mark.asyncio
    async def test_empty_recipients_rejected(self):
        legacy = await sync_to_async(self._make_recap)(self.event, approved=True)
        result = await self._execute_mutation_authenticated(
            SHARE_BULK_BY_EMAIL,
            {"recapIds": [str(legacy.id)], "recipients": []},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        payload = result.data["shareRecapsByEmail"]
        assert payload["success"] is False, payload

    @pytest.mark.asyncio
    async def test_no_ids_rejected(self):
        result = await self._execute_mutation_authenticated(
            SHARE_BULK_BY_EMAIL,
            {"recipients": ["c@example.com"]},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        payload = result.data["shareRecapsByEmail"]
        assert payload["success"] is False, payload
        assert "select at least one" in payload["message"].lower()

    @pytest.mark.asyncio
    async def test_batch_cap_rejected(self):
        ids = []
        for i in range(26):
            recap = await sync_to_async(self._make_recap)(
                self.event, approved=True, name=f"R{i}"
            )
            ids.append(str(recap.id))
        result = await self._execute_mutation_authenticated(
            SHARE_BULK_BY_EMAIL,
            {"recapIds": ids, "recipients": ["c@example.com"]},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        payload = result.data["shareRecapsByEmail"]
        assert payload["success"] is False, payload
        assert "25" in payload["message"]
        assert len(mail.outbox) == 0
