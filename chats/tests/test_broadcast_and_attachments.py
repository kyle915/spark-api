"""Coverage for the admin BA-messaging panel additions:

  - broadcastChatMessage fan-out — one message per target across
    explicit BAs + group members, with NO duplicate threads;
  - STRICT tenant isolation — cross-tenant BAs and cross-tenant groups
    are dropped, never messaged;
  - attachments — persisted on the message and the public (non-signed)
    URL resolves on the ChatMessage.attachments field;
  - body-optional-when-attachment — an attachment-only send (empty body)
    is accepted, a body-less + attachment-less send is rejected.

All exercised through the CLIENT schema (`/graphql/clients`) the web
admin talks to, with a spark-admin acting inside a tenant by passing
`tenantId` — the same posture the recap pickers use.
"""

import pytest
from datetime import datetime, timedelta, timezone as _tz

from asgiref.sync import sync_to_async

from ambassadors.tests.base import AmbassadorsGraphQLTestCase


@pytest.fixture(autouse=True)
def _gcs_bucket(settings):
    # Attachment URLs come from utils.gcs.public_url(), which returns None
    # when settings.GS_BUCKET_NAME is empty (no bucket configured in CI) —
    # pin a dummy bucket so the URL assertions run everywhere.
    settings.GS_BUCKET_NAME = "spark-test-bucket"


BROADCAST_MUTATION = """
mutation Broadcast($input: BroadcastChatMessageInput!) {
  broadcastChatMessage(input: $input) {
    success
    message
    recipientCount
    threadUuids
  }
}
"""

SEND_MUTATION = """
mutation Send($input: SendChatMessageInput!) {
  sendChatMessage(input: $input) {
    uuid
    body
    attachments {
      uuid
      kind
      originalFilename
      contentType
      byteSize
      url
    }
  }
}
"""

OPEN_THREAD_MUTATION = """
mutation Open($input: OpenChatThreadInput!) {
  openChatThread(input: $input) {
    uuid
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestBroadcastAndAttachments(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"

        # Active tenant + a foreign tenant whose BAs/groups must never be
        # reachable from a broadcast scoped to the active tenant.
        self.tenant = self.create_tenant(name="Girl Beer")
        self.other_tenant = self.create_tenant(name="Liquid Death")

        # Spark admin (unrestricted role) acting inside self.tenant by
        # passing tenantId. Also a TenantedUser of the active tenant so
        # the existing 1:1 sendChatMessage membership gate passes.
        self.admin = self.create_user(
            username="admin-broadcast",
            email="admin-broadcast@test.com",
            role=self.roles["spark_admin"],
        )
        self.create_tenanted_user(self.admin, self.tenant)

        now = datetime.now(_tz.utc)

        # --- Two BAs with event history in the ACTIVE tenant. ---
        self.ba1 = self._make_ba("ba1")
        self.ba2 = self._make_ba("ba2")
        self.event = self.create_event(
            name="Whole Foods Burbank",
            tenant=self.tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )
        self._link_ba_to_tenant(self.ba1, self.tenant, self.event)
        self._link_ba_to_tenant(self.ba2, self.tenant, self.event)

        # --- A BA who belongs ONLY to the foreign tenant. ---
        self.ba_foreign = self._make_ba("ba-foreign")
        self.foreign_event = self.create_event(
            name="Albertsons",
            tenant=self.other_tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )
        self._link_ba_to_tenant(
            self.ba_foreign, self.other_tenant, self.foreign_event
        )

        # --- A group in the ACTIVE tenant containing ba1 + a 3rd BA. ---
        self.ba3 = self._make_ba("ba3")
        self.event3 = self.create_event(
            name="H-E-B West Lake Hills",
            tenant=self.tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )
        self._link_ba_to_tenant(self.ba3, self.tenant, self.event3)
        self.group = self._make_group("Austin Crew", self.tenant)
        self._add_group_member(self.group, self.ba1)  # overlaps explicit ba1
        self._add_group_member(self.group, self.ba3)

        # --- A group in the FOREIGN tenant — must never be honored. ---
        self.foreign_group = self._make_group("LD Crew", self.other_tenant)
        self._add_group_member(self.foreign_group, self.ba_foreign)

    # ----------------------------------------------------------------- helpers
    def _make_ba(self, slug: str):
        user = self.create_user(
            username=f"{slug}-user",
            email=f"{slug}@test.com",
            role=self.roles["ambassador"],
        )
        return self.create_ambassador(user=user)

    def _link_ba_to_tenant(self, ambassador, tenant, event):
        from ambassadors.models import AmbassadorEvent

        system_user = self.get_system_user()
        return AmbassadorEvent.objects.create(
            ambassador=ambassador,
            tenant=tenant,
            event=event,
            is_approved=True,
            created_by=system_user,
        )

    def _make_group(self, name, tenant):
        from ambassadors.models import AmbassadorGroup, GroupType

        system_user = self.get_system_user()
        gt, _ = GroupType.objects.get_or_create(
            name="Crew", defaults={"created_by": system_user}
        )
        return AmbassadorGroup.objects.create(
            name=name,
            group_type=gt,
            tenant=tenant,
            created_by=system_user,
        )

    def _add_group_member(self, group, ambassador):
        from ambassadors.models import UserGroup

        return UserGroup.objects.create(
            user=ambassador.user,
            group=group,
            ambassador=ambassador,
        )

    @sync_to_async
    def _amb_uuid(self, ambassador):
        return str(ambassador.uuid)

    @sync_to_async
    def _group_uuid(self, group):
        return str(group.uuid)

    @sync_to_async
    def _thread_count(self, **filters):
        from chats.models import ChatThread

        return ChatThread.objects.filter(**filters).count()

    @sync_to_async
    def _messages_for_ambassador(self, ambassador):
        from chats.models import ChatMessage

        return list(
            ChatMessage.objects.filter(
                thread__ambassador_id=ambassador.id
            ).values_list("body", flat=True)
        )

    # ------------------------------------------------------------------- tests
    @pytest.mark.asyncio
    async def test_fanout_one_message_per_target_no_dup_threads(self):
        """Union of explicit BAs (ba1, ba2) + group members (ba1, ba3)
        = {ba1, ba2, ba3}. Each gets exactly one message in exactly one
        (de-duplicated) thread, even though ba1 is targeted twice."""
        ba1_uuid = await self._amb_uuid(self.ba1)
        ba2_uuid = await self._amb_uuid(self.ba2)
        group_uuid = await self._group_uuid(self.group)

        result = await self._execute_mutation_authenticated(
            BROADCAST_MUTATION,
            {
                "input": {
                    "body": "Shift reminder team!",
                    "ambassadorUuids": [ba1_uuid, ba2_uuid],
                    "groupUuids": [group_uuid],
                    "tenantId": str(self.tenant.id),
                }
            },
            self.admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        data = result.data["broadcastChatMessage"]
        assert data["success"] is True, data
        # ba1, ba2, ba3 — ba1 counted once despite double-targeting.
        assert data["recipientCount"] == 3, data
        assert len(data["threadUuids"]) == 3
        assert len(set(data["threadUuids"])) == 3  # all distinct

        # Exactly one general thread per recipient BA in this tenant.
        for ba in (self.ba1, self.ba2, self.ba3):
            cnt = await self._thread_count(
                tenant_id=self.tenant.id, ambassador_id=ba.id, kind="general"
            )
            assert cnt == 1, f"BA {ba.id} thread count {cnt}"
            bodies = await self._messages_for_ambassador(ba)
            assert bodies == ["Shift reminder team!"], (ba.id, bodies)

        # The foreign BA got nothing.
        foreign_bodies = await self._messages_for_ambassador(self.ba_foreign)
        assert foreign_bodies == []

    @pytest.mark.asyncio
    async def test_rebroadcast_reuses_thread_no_duplicate(self):
        """Broadcasting twice to the same BA must reuse the existing 1:1
        thread (idempotent creation) — two messages, one thread."""
        ba1_uuid = await self._amb_uuid(self.ba1)
        payload = {
            "input": {
                "body": "first",
                "ambassadorUuids": [ba1_uuid],
                "tenantId": str(self.tenant.id),
            }
        }
        r1 = await self._execute_mutation_authenticated(
            BROADCAST_MUTATION, payload, self.admin, self.endpoint_path
        )
        assert r1.errors is None and r1.data["broadcastChatMessage"]["success"]

        payload["input"]["body"] = "second"
        r2 = await self._execute_mutation_authenticated(
            BROADCAST_MUTATION, payload, self.admin, self.endpoint_path
        )
        assert r2.errors is None and r2.data["broadcastChatMessage"]["success"]

        # One thread, both messages.
        cnt = await self._thread_count(
            tenant_id=self.tenant.id, ambassador_id=self.ba1.id, kind="general"
        )
        assert cnt == 1
        bodies = await self._messages_for_ambassador(self.ba1)
        assert sorted(bodies) == ["first", "second"]
        assert (
            r1.data["broadcastChatMessage"]["threadUuids"]
            == r2.data["broadcastChatMessage"]["threadUuids"]
        )

    @pytest.mark.asyncio
    async def test_cross_tenant_targets_are_dropped(self):
        """Passing a foreign-tenant BA and a foreign-tenant group while
        acting in self.tenant must reach NEITHER — strict isolation."""
        foreign_ba_uuid = await self._amb_uuid(self.ba_foreign)
        foreign_group_uuid = await self._group_uuid(self.foreign_group)

        result = await self._execute_mutation_authenticated(
            BROADCAST_MUTATION,
            {
                "input": {
                    "body": "should not arrive",
                    "ambassadorUuids": [foreign_ba_uuid],
                    "groupUuids": [foreign_group_uuid],
                    "tenantId": str(self.tenant.id),
                }
            },
            self.admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        data = result.data["broadcastChatMessage"]
        # Nothing matched in the active tenant.
        assert data["recipientCount"] == 0, data
        assert data["threadUuids"] == []
        foreign_bodies = await self._messages_for_ambassador(self.ba_foreign)
        assert foreign_bodies == []

    @pytest.mark.asyncio
    async def test_broadcast_without_tenant_in_scope_is_blocked(self):
        """An unrestricted admin with NO tenant in scope must broadcast
        to nobody — the same hard stop recapEventOptions enforces."""
        ba1_uuid = await self._amb_uuid(self.ba1)
        result = await self._execute_mutation_authenticated(
            BROADCAST_MUTATION,
            {
                "input": {
                    "body": "no tenant",
                    "ambassadorUuids": [ba1_uuid],
                    # no tenantId
                }
            },
            self.admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        data = result.data["broadcastChatMessage"]
        assert data["success"] is False
        assert data["recipientCount"] == 0
        bodies = await self._messages_for_ambassador(self.ba1)
        assert bodies == []

    @pytest.mark.asyncio
    async def test_broadcast_with_attachment_persists_and_url_resolves(self):
        """Broadcast an image attachment with an empty body. The message
        persists, the attachment row persists with kind=image, and its
        public (non-signed) URL resolves."""
        from chats.models import ChatMessage, ChatMessageAttachment

        ba1_uuid = await self._amb_uuid(self.ba1)
        result = await self._execute_mutation_authenticated(
            BROADCAST_MUTATION,
            {
                "input": {
                    "body": "",  # empty — attachment carries the message
                    "ambassadorUuids": [ba1_uuid],
                    "tenantId": str(self.tenant.id),
                    "attachments": [
                        {
                            "file": "chat_attachments/flyer.png",
                            "originalFilename": "flyer.png",
                            "contentType": "image/png",
                            "byteSize": 2048,
                        }
                    ],
                }
            },
            self.admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        assert result.data["broadcastChatMessage"]["recipientCount"] == 1

        @sync_to_async
        def _load():
            msg = ChatMessage.objects.filter(
                thread__ambassador_id=self.ba1.id
            ).first()
            atts = list(
                ChatMessageAttachment.objects.filter(message_id=msg.id)
            )
            return msg, atts

        msg, atts = await _load()
        assert msg is not None
        assert msg.body == ""
        assert len(atts) == 1
        att = atts[0]
        assert att.kind == ChatMessageAttachment.KIND_IMAGE
        assert att.content_type == "image/png"
        assert att.byte_size == 2048
        # blob stored, not a signed URL
        assert str(att.file) == "chat_attachments/flyer.png"

        # Public URL resolves through the GraphQL field (recap-photo path).
        from chats.types import ChatMessageAttachment as AttType

        url = await AttType.url(att)
        assert url is not None
        assert url.endswith("/chat_attachments/flyer.png")
        assert url.startswith("https://storage.googleapis.com/")

    @pytest.mark.asyncio
    async def test_send_message_attachment_only_allowed(self):
        """sendChatMessage with empty body but an attachment is accepted
        (body-optional-when-attachment), and a PDF attachment infers
        kind=pdf from its content type and renders its url + filename."""
        ba1_uuid = await self._amb_uuid(self.ba1)
        # Admin opens the 1:1 thread first.
        opened = await self._execute_mutation_authenticated(
            OPEN_THREAD_MUTATION,
            {"input": {"ambassadorUuid": ba1_uuid}},
            self.admin,
            self.endpoint_path,
        )
        assert opened.errors is None, f"errored: {opened.errors}"
        thread_uuid = opened.data["openChatThread"]["uuid"]

        result = await self._execute_mutation_authenticated(
            SEND_MUTATION,
            {
                "input": {
                    "threadUuid": thread_uuid,
                    "attachments": [
                        {
                            "file": "chat_attachments/contract.pdf",
                            "originalFilename": "contract.pdf",
                            "contentType": "application/pdf",
                            "byteSize": 99,
                        }
                    ],
                }
            },
            self.admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        msg = result.data["sendChatMessage"]
        assert msg["body"] == ""
        assert len(msg["attachments"]) == 1
        att = msg["attachments"][0]
        assert att["kind"] == "pdf"
        assert att["originalFilename"] == "contract.pdf"
        assert att["url"].endswith("/chat_attachments/contract.pdf")

    @pytest.mark.asyncio
    async def test_send_message_empty_body_and_no_attachment_rejected(self):
        """A send with neither body nor attachment must be rejected."""
        ba1_uuid = await self._amb_uuid(self.ba1)
        opened = await self._execute_mutation_authenticated(
            OPEN_THREAD_MUTATION,
            {"input": {"ambassadorUuid": ba1_uuid}},
            self.admin,
            self.endpoint_path,
        )
        thread_uuid = opened.data["openChatThread"]["uuid"]

        result = await self._execute_mutation_authenticated(
            SEND_MUTATION,
            {"input": {"threadUuid": thread_uuid, "body": "   "}},
            self.admin,
            self.endpoint_path,
        )
        assert result.errors is not None
        assert "body or an attachment" in str(result.errors)

    @pytest.mark.asyncio
    async def test_single_recipient_broadcast_is_broadcast_of_one(self):
        """The composer's single-recipient send path is just a one-target
        broadcast — recipientCount 1, one thread, one message."""
        ba2_uuid = await self._amb_uuid(self.ba2)
        result = await self._execute_mutation_authenticated(
            BROADCAST_MUTATION,
            {
                "input": {
                    "body": "just you",
                    "ambassadorUuids": [ba2_uuid],
                    "tenantId": str(self.tenant.id),
                }
            },
            self.admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        data = result.data["broadcastChatMessage"]
        assert data["recipientCount"] == 1
        bodies = await self._messages_for_ambassador(self.ba2)
        assert bodies == ["just you"]
