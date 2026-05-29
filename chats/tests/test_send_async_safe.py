"""Regression: chat send/broadcast/open must NOT raise
SynchronousOnlyOperation when the client selects the fields it actually
asks for.

The live bug (both BA mobile + admin web):

    "You cannot call this from an async context - use a thread or
     sync_to_async"

…fired on sendChatMessage / broadcastChatMessage. The message sometimes
persisted but the mutation errored *resolving its return value*. Root
cause: ChatMessage.senderName was a SYNC resolver that touched the lazy
`sender` FK. The object handed back from the mutation is a freshly
`.create()`'d ChatMessage with no select_related("sender"), so reading
`self.sender` evaluated the FK with a synchronous DB query inside
Strawberry's async executor → SynchronousOnlyOperation. PR #636 only
fixed the ChatThread FK resolvers; this covers the message return path.

These tests run the mutations THROUGH THE SCHEMA (real async execution),
selecting exactly what the web (SendChatMessageMutation /
ChatThreadQuery) and mobile (SEND_CHAT_MESSAGE_MUTATION) operations
request — including senderName, senderIsAmbassador, the thread's
ambassadorUuid/ambassadorName, and attachments{url} — and assert NO
async error surfaces.

The attachment `url` may resolve to None locally if the GCS bucket env
is unset; that's environmental — we assert on success / no-async-error,
not on a live URL.
"""

import pytest
from datetime import datetime, timedelta, timezone as _tz

from asgiref.sync import sync_to_async

from ambassadors.tests.base import AmbassadorsGraphQLTestCase


# The web SendChatMessageMutation selection set, verbatim in spirit:
# id/uuid/body/senderIsAmbassador/senderName/createdAt + attachments{url}.
# senderName is the field that used to blow up.
SEND_MUTATION = """
mutation Send($input: SendChatMessageInput!) {
  sendChatMessage(input: $input) {
    id
    uuid
    body
    senderIsAmbassador
    senderName
    createdAt
    attachments {
      id
      uuid
      kind
      url
      originalFilename
      contentType
      byteSize
    }
  }
}
"""

OPEN_THREAD_MUTATION = """
mutation Open($input: OpenChatThreadInput!) {
  openChatThread(input: $input) {
    id
    uuid
    kind
    ambassadorUuid
    ambassadorName
    jobUuid
    jobName
    unreadForAdmin
  }
}
"""

# The web BroadcastChatMessageMutation selection set.
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

# Task 2: the composer's recipient picker source.
CHAT_RECIPIENTS_QUERY = """
query Recipients($tenantId: ID, $q: String) {
  chatRecipientAmbassadors(tenantId: $tenantId, q: $q) {
    uuid
    name
    email
  }
}
"""

# Same roster, now also selecting the Relay `id` — the recap
# "FILLING FOR A BA?" picker reuses this roster and feeds the id to
# createCustomRecap's ambassadorId, so the id must decode to the BA pk.
CHAT_RECIPIENTS_WITH_ID_QUERY = """
query RecipientsWithId($tenantId: ID, $q: String) {
  chatRecipientAmbassadors(tenantId: $tenantId, q: $q) {
    id
    uuid
    name
  }
}
"""

# Task 3: archive (soft delete) a thread.
ARCHIVE_MUTATION = """
mutation Archive($input: ArchiveChatThreadInput!) {
  archiveChatThread(input: $input) {
    uuid
    archivedAt
  }
}
"""

# The thread list — used to prove archive hides / restore shows.
CHAT_THREADS_QUERY = """
query Threads($includeArchived: Boolean!) {
  chatThreads(includeArchived: $includeArchived) {
    uuid
    archivedAt
  }
}
"""

# The web ChatThreadQuery messages selection — proves the read path
# (which DOES select_related sender) also resolves senderName cleanly
# now that the resolver is async.
THREAD_QUERY = """
query Thread($uuid: ID!) {
  chatThread(uuid: $uuid) {
    id
    uuid
    ambassadorUuid
    ambassadorName
    unreadForAdmin
    messages(first: 50) {
      id
      uuid
      body
      senderIsAmbassador
      senderName
      createdAt
      attachments { id uuid kind url originalFilename }
    }
  }
}
"""


def _no_async_error(result) -> bool:
    """True unless an error mentioning the async-context failure is
    present. We tolerate environmental errors elsewhere, but a
    SynchronousOnlyOperation is the regression we're guarding."""
    if not result.errors:
        return True
    blob = " ".join(str(e) for e in result.errors).lower()
    return not (
        "async context" in blob
        or "synchronousonly" in blob
        or "use a thread or sync_to_async" in blob
    )


@pytest.mark.django_db(transaction=True)
class TestSendAsyncSafe(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.client_endpoint = "/api/v1/graphql/clients"
        self.mobile_endpoint = "/api/v1/graphql/mobile"

        self.tenant = self.create_tenant(name="Girl Beer")

        # Admin (spark-admin) acting in self.tenant, + TenantedUser so the
        # 1:1 send membership gate passes.
        self.admin = self.create_user(
            username="async-admin",
            email="async-admin@test.com",
            role=self.roles["spark_admin"],
        )
        self.create_tenanted_user(self.admin, self.tenant)

        now = datetime.now(_tz.utc)
        # A BA with event history in the tenant (the broadcast-targetable
        # mechanism) AND a real name so senderName has something to render.
        self.ba_user = self.create_user(
            username="async-ba-user",
            email="async-ba@test.com",
            role=self.roles["ambassador"],
            first_name="Dana",
            last_name="Scully",
        )
        self.ba = self.create_ambassador(user=self.ba_user)
        self.event = self.create_event(
            name="Whole Foods Burbank",
            tenant=self.tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )
        self._link_ba_to_tenant(self.ba, self.tenant, self.event)

    # ----------------------------------------------------------- helpers
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

    @sync_to_async
    def _amb_uuid(self, ambassador):
        return str(ambassador.uuid)

    async def _open_thread_as_admin(self):
        self.schema = self._client_schema
        ba_uuid = await self._amb_uuid(self.ba)
        opened = await self._execute_mutation_authenticated(
            OPEN_THREAD_MUTATION,
            {"input": {"ambassadorUuid": ba_uuid}},
            self.admin,
            self.client_endpoint,
        )
        assert _no_async_error(opened), f"open async-errored: {opened.errors}"
        assert opened.errors is None, opened.errors
        thread = opened.data["openChatThread"]
        # The thread FK resolvers (#636) must also resolve cleanly here.
        assert thread["ambassadorUuid"]
        assert thread["ambassadorName"] == "Dana Scully"
        return thread["uuid"]

    @property
    def _client_schema(self):
        from config.schema_client import schema_clients

        return schema_clients

    @property
    def _mobile_schema(self):
        from config.schema_mobile import schema_mobile

        return schema_mobile

    # ------------------------------------------------------------- tests
    @pytest.mark.asyncio
    async def test_admin_send_text_message_resolves_sender_name(self):
        """The exact failing web path: admin sends a text message and the
        return selects senderName/senderIsAmbassador. No async error."""
        self.schema = self._client_schema
        thread_uuid = await self._open_thread_as_admin()

        result = await self._execute_mutation_authenticated(
            SEND_MUTATION,
            {"input": {"threadUuid": thread_uuid, "body": "Hey, you free Sat?"}},
            self.admin,
            self.client_endpoint,
        )
        assert _no_async_error(result), (
            f"sendChatMessage raised the async-context error: {result.errors}"
        )
        assert result.errors is None, f"errored: {result.errors}"
        msg = result.data["sendChatMessage"]
        assert msg["body"] == "Hey, you free Sat?"
        assert msg["senderIsAmbassador"] is False
        # Admin's name — resolves through the (now async) FK resolver
        # without a SynchronousOnlyOperation.
        assert msg["senderName"] is not None
        assert msg["attachments"] == []

    @pytest.mark.asyncio
    async def test_ba_send_text_message_through_mobile_schema(self):
        """The BA mobile failing path: BA sends through the mobile schema
        selecting senderName. No async error; sender flagged as BA."""
        # BA opens their own thread (ambassador caller → own row).
        self.schema = self._mobile_schema
        opened = await self._execute_mutation_authenticated(
            OPEN_THREAD_MUTATION,
            {"input": {}},
            self.ba_user,
            self.mobile_endpoint,
        )
        assert _no_async_error(opened), f"open async-errored: {opened.errors}"
        assert opened.errors is None, f"errored: {opened.errors}"
        thread_uuid = opened.data["openChatThread"]["uuid"]

        result = await self._execute_mutation_authenticated(
            SEND_MUTATION,
            {"input": {"threadUuid": thread_uuid, "body": "On my way!"}},
            self.ba_user,
            self.mobile_endpoint,
        )
        assert _no_async_error(result), (
            f"BA sendChatMessage raised the async-context error: {result.errors}"
        )
        assert result.errors is None, f"errored: {result.errors}"
        msg = result.data["sendChatMessage"]
        assert msg["body"] == "On my way!"
        assert msg["senderIsAmbassador"] is True
        assert msg["senderName"] == "Dana Scully"

    @pytest.mark.asyncio
    async def test_admin_send_with_attachment_resolves_url_no_async_error(self):
        """Attachment-bearing send selecting attachments{url} + senderName
        must not async-error. url MAY be None locally (no GCS bucket env)
        — we assert on no-async-error, not a live URL."""
        self.schema = self._client_schema
        thread_uuid = await self._open_thread_as_admin()

        result = await self._execute_mutation_authenticated(
            SEND_MUTATION,
            {
                "input": {
                    "threadUuid": thread_uuid,
                    "body": "",
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
            self.client_endpoint,
        )
        assert _no_async_error(result), (
            f"attachment send raised the async-context error: {result.errors}"
        )
        assert result.errors is None, f"errored: {result.errors}"
        msg = result.data["sendChatMessage"]
        assert len(msg["attachments"]) == 1
        assert msg["attachments"][0]["kind"] == "image"
        assert msg["senderName"] is not None

    @pytest.mark.asyncio
    async def test_broadcast_then_read_back_resolves_sender_name(self):
        """broadcastChatMessage fans out without async error, and reading
        the resulting thread back (messages{senderName}) — the path that
        select_related's sender — also resolves cleanly."""
        self.schema = self._client_schema
        ba_uuid = await self._amb_uuid(self.ba)
        bc = await self._execute_mutation_authenticated(
            BROADCAST_MUTATION,
            {
                "input": {
                    "body": "Team shift reminder!",
                    "ambassadorUuids": [ba_uuid],
                    "tenantId": str(self.tenant.id),
                }
            },
            self.admin,
            self.client_endpoint,
        )
        assert _no_async_error(bc), f"broadcast async-errored: {bc.errors}"
        assert bc.errors is None, f"errored: {bc.errors}"
        data = bc.data["broadcastChatMessage"]
        assert data["success"] is True
        assert data["recipientCount"] == 1
        thread_uuid = data["threadUuids"][0]

        # Read the thread back selecting messages{senderName} — the read
        # path (select_related sender) must also be async-clean.
        read = await self._execute_query_authenticated(
            THREAD_QUERY,
            {"uuid": thread_uuid},
            self.admin,
            self.client_endpoint,
        )
        assert _no_async_error(read), f"thread read async-errored: {read.errors}"
        assert read.errors is None, f"errored: {read.errors}"
        thread = read.data["chatThread"]
        assert thread["ambassadorName"] == "Dana Scully"
        bodies = [m["body"] for m in thread["messages"]]
        assert "Team shift reminder!" in bodies
        for m in thread["messages"]:
            # senderName resolved for every message with no async error.
            assert m["senderName"] is not None

    # ---------------------------------------------- Task 2: recipient picker
    @pytest.mark.asyncio
    async def test_recipient_picker_lists_tenant_bas(self):
        """chatRecipientAmbassadors returns the active tenant's event-history
        BAs (the fix for the composer's "No ambassadors found"), even though
        the BA has no TenantedUser row."""
        self.schema = self._client_schema
        result = await self._execute_query_authenticated(
            CHAT_RECIPIENTS_QUERY,
            {"tenantId": str(self.tenant.id)},
            self.admin,
            self.client_endpoint,
        )
        assert result.errors is None, f"errored: {result.errors}"
        recips = result.data["chatRecipientAmbassadors"]
        names = [r["name"] for r in recips]
        assert "Dana Scully" in names, recips

    @pytest.mark.asyncio
    async def test_recipient_picker_exposes_decodable_relay_id(self):
        """Each roster row carries the Ambassador's Relay global id, and it
        decodes back to the BA's pk — this is what lets the recap
        "FILLING FOR A BA?" picker reuse this roster as an ambassadorId
        source (resolve_id_to_int rejects raw uuids)."""
        from utils.graphql.mixins import decode_global_id

        self.schema = self._client_schema
        result = await self._execute_query_authenticated(
            CHAT_RECIPIENTS_WITH_ID_QUERY,
            {"tenantId": str(self.tenant.id)},
            self.admin,
            self.client_endpoint,
        )
        assert result.errors is None, f"errored: {result.errors}"
        rows = result.data["chatRecipientAmbassadors"]
        dana = next(r for r in rows if r["name"] == "Dana Scully")
        assert dana["id"], dana
        assert decode_global_id(dana["id"]) == self.ba.id

    @pytest.mark.asyncio
    async def test_recipient_picker_search_matches_name(self):
        """The `q` term filters BAs by name/email server-side."""
        self.schema = self._client_schema
        hit = await self._execute_query_authenticated(
            CHAT_RECIPIENTS_QUERY,
            {"tenantId": str(self.tenant.id), "q": "scu"},
            self.admin,
            self.client_endpoint,
        )
        assert hit.errors is None, f"errored: {hit.errors}"
        assert [r["name"] for r in hit.data["chatRecipientAmbassadors"]] == [
            "Dana Scully"
        ]

        miss = await self._execute_query_authenticated(
            CHAT_RECIPIENTS_QUERY,
            {"tenantId": str(self.tenant.id), "q": "zzz-nobody"},
            self.admin,
            self.client_endpoint,
        )
        assert miss.errors is None
        assert miss.data["chatRecipientAmbassadors"] == []

    @pytest.mark.asyncio
    async def test_recipient_picker_no_tenant_returns_empty(self):
        """No tenant in scope → empty list, not a cross-tenant leak
        (mirrors recapEventOptions' hard stop)."""
        self.schema = self._client_schema
        result = await self._execute_query_authenticated(
            CHAT_RECIPIENTS_QUERY,
            {},  # no tenantId; spark-admin has no implicit single tenant
            self.admin,
            self.client_endpoint,
        )
        assert result.errors is None, f"errored: {result.errors}"
        assert result.data["chatRecipientAmbassadors"] == []

    # ------------------------------------------------- Task 3: archive thread
    @pytest.mark.asyncio
    async def test_archive_thread_hides_then_restore_shows(self):
        """archiveChatThread soft-deletes (drops from the default list) and
        is reversible — messages are never purged."""
        self.schema = self._client_schema
        thread_uuid = await self._open_thread_as_admin()
        # Seed a message so we can prove it survives the archive.
        await self._execute_mutation_authenticated(
            SEND_MUTATION,
            {"input": {"threadUuid": thread_uuid, "body": "keep me"}},
            self.admin,
            self.client_endpoint,
        )

        # Archive.
        arch = await self._execute_mutation_authenticated(
            ARCHIVE_MUTATION,
            {"input": {"threadUuid": thread_uuid, "archived": True}},
            self.admin,
            self.client_endpoint,
        )
        assert arch.errors is None, f"errored: {arch.errors}"
        assert arch.data["archiveChatThread"]["archivedAt"] is not None

        # Gone from the default (non-archived) list.
        listed = await self._execute_query_authenticated(
            CHAT_THREADS_QUERY,
            {"includeArchived": False},
            self.admin,
            self.client_endpoint,
        )
        assert listed.errors is None
        uuids = [t["uuid"] for t in listed.data["chatThreads"]]
        assert thread_uuid not in uuids

        # Still present when archived are included → not purged.
        listed_all = await self._execute_query_authenticated(
            CHAT_THREADS_QUERY,
            {"includeArchived": True},
            self.admin,
            self.client_endpoint,
        )
        assert thread_uuid in [t["uuid"] for t in listed_all.data["chatThreads"]]

        # The message survived the soft delete.
        msgs = await self._messages_for_thread(thread_uuid)
        assert "keep me" in msgs

        # Restore → back in the default list.
        restore = await self._execute_mutation_authenticated(
            ARCHIVE_MUTATION,
            {"input": {"threadUuid": thread_uuid, "archived": False}},
            self.admin,
            self.client_endpoint,
        )
        assert restore.errors is None, f"errored: {restore.errors}"
        assert restore.data["archiveChatThread"]["archivedAt"] is None
        listed2 = await self._execute_query_authenticated(
            CHAT_THREADS_QUERY,
            {"includeArchived": False},
            self.admin,
            self.client_endpoint,
        )
        assert thread_uuid in [t["uuid"] for t in listed2.data["chatThreads"]]

    @pytest.mark.asyncio
    async def test_ba_cannot_archive_thread(self):
        """A BA caller is refused — archive is admin-only."""
        self.schema = self._client_schema
        thread_uuid = await self._open_thread_as_admin()

        # Switch to the mobile schema as the BA and attempt to archive.
        self.schema = self._mobile_schema
        result = await self._execute_mutation_authenticated(
            ARCHIVE_MUTATION,
            {"input": {"threadUuid": thread_uuid, "archived": True}},
            self.ba_user,
            self.mobile_endpoint,
        )
        # Either the field isn't on the mobile mutation surface, or it is
        # and rejects the BA — both are acceptable "BA can't archive".
        assert result.errors is not None
        blob = " ".join(str(e) for e in result.errors).lower()
        assert (
            "only admins" in blob
            or "cannot query field" in blob
            or "didn't know how to handle" in blob
        ), result.errors

    @sync_to_async
    def _messages_for_thread(self, thread_uuid):
        from chats.models import ChatMessage, ChatThread

        t = ChatThread.objects.filter(uuid=thread_uuid).first()
        if t is None:
            return []
        return list(
            ChatMessage.objects.filter(thread_id=t.id).values_list(
                "body", flat=True
            )
        )
