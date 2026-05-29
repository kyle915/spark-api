"""GraphQL input shapes for the chat mutations."""
from typing import List, Optional

import strawberry
from utils.graphql.inputs import SparkGraphQLInput


@strawberry.input
class ChatAttachmentInput(SparkGraphQLInput):
    """One file to attach to a chat message.

    `file` is the GCS blob path (or full URL) the client already
    uploaded via the `getUploadUrl` → PUT flow — exactly the same
    mechanism recap photos use. The server stores the blob name and
    serves it back via the non-signed public URL.

    `kind` is one of image | pdf | file; if omitted the server infers
    it from `content_type` / the filename extension.
    """

    file: str
    kind: Optional[str] = None
    original_filename: Optional[str] = None
    content_type: Optional[str] = None
    byte_size: Optional[int] = None


@strawberry.input
class SendChatMessageInput(SparkGraphQLInput):
    """Send a message into an existing thread.

    `thread_uuid` is required — the client should resolve the thread
    first via openChatThread (which get-or-creates one for a given
    job/general context). We deliberately don't accept ambassador+job
    here so we don't fork on which "side" is sending: the resolver
    derives `sender_is_ambassador` from the request user.

    `body` is optional when `attachments` carries at least one file
    (send a photo/PDF with no caption).
    """

    thread_uuid: str
    body: Optional[str] = None
    attachments: Optional[List[ChatAttachmentInput]] = None


@strawberry.input
class BroadcastChatMessageInput(SparkGraphQLInput):
    """Admin → many BAs. Fans a single message (with optional
    attachments) out to the 1:1 thread of every targeted BA.

    Targets are the UNION of:
      - `ambassador_uuids`: explicit BAs, AND
      - `group_uuids`: every member of each AmbassadorGroup.

    Everything is STRICTLY scoped to the active tenant — cross-tenant
    BAs / groups are ignored. `tenant_id` is the dashboard tenant the
    admin is acting in (same arg the recap pickers take); staff/spark-
    admins pass it, a client user resolves to their own membership.
    """

    body: Optional[str] = None
    attachments: Optional[List[ChatAttachmentInput]] = None
    ambassador_uuids: Optional[List[str]] = None
    group_uuids: Optional[List[str]] = None
    tenant_id: Optional[strawberry.ID] = None
    tenant_uuid: Optional[strawberry.ID] = None


@strawberry.input
class OpenChatThreadInput(SparkGraphQLInput):
    """Resolve or create a thread.

    Exactly one of the two contexts:
      - ambassador_uuid + job_uuid  → kind="job"
      - ambassador_uuid only        → kind="general"

    Used by both sides:
      - Admin opening chat to a BA from /ba/:uuid or a job detail page
      - BA opening chat from the mobile shift card or general inbox
        (when BA is the caller, ambassador_uuid is ignored — we use
        the caller's own Ambassador row)
    """

    ambassador_uuid: str | None = None
    job_uuid: str | None = None


@strawberry.input
class MarkChatThreadReadInput(SparkGraphQLInput):
    thread_uuid: str
