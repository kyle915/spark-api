"""GraphQL input shapes for the chat mutations."""
import strawberry
from utils.graphql.inputs import SparkGraphQLInput


@strawberry.input
class SendChatMessageInput(SparkGraphQLInput):
    """Send a message into an existing thread.

    `thread_uuid` is required — the client should resolve the thread
    first via openChatThread (which get-or-creates one for a given
    job/general context). We deliberately don't accept ambassador+job
    here so we don't fork on which "side" is sending: the resolver
    derives `sender_is_ambassador` from the request user.
    """

    thread_uuid: str
    body: str


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
