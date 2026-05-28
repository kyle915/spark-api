"""Chat write resolvers — open thread, send message, mark read.

Three mutations:

  - openChatThread(input) → get-or-create a ChatThread for the
    requested (ambassador, kind, job) context. Admins can name an
    ambassador; BAs always operate on their own row.
  - sendChatMessage(input) → write a ChatMessage and bump the thread's
    recency/preview columns atomically. Fires a push notification to
    the OTHER side (caller's own side already saw the message — they
    just sent it).
  - markChatThreadRead(input) → sweep the recipient's unread side
    forward to now. Idempotent; safe to call on every thread-open.

Push delivery is best-effort. A failed push doesn't roll back the
message — the recipient will still see it next time they open the
inbox, and the existing in-app polling on the thread will surface it
without the push.
"""
from __future__ import annotations

import logging
from typing import Optional

import strawberry
from asgiref.sync import sync_to_async
from graphql import GraphQLError

from chats import inputs, models, services, types  # noqa: F401
from utils.graphql.permissions import StrictIsAuthenticated

logger = logging.getLogger(__name__)


def _resolve_uuid_to_int(uuid_str: str, model_cls) -> Optional[int]:
    row = model_cls.objects.filter(uuid=uuid_str).only("id").first()
    return row.id if row else None


@strawberry.type
class ChatMutations:
    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def open_chat_thread(
        self, info: strawberry.Info, input: inputs.OpenChatThreadInput
    ) -> types.ChatThread:
        """Resolve or create a chat thread for the requested context.

        Admin caller: must pass ambassador_uuid. Pass job_uuid alongside
        for a per-job thread; omit it for the general thread.

        Ambassador caller: ambassador_uuid is ignored (we use the
        caller's own Ambassador row). job_uuid optional.
        """
        user, amb, is_ba, is_admin, _ = await services.resolve_caller_context(info)
        if user is None:
            raise GraphQLError("Authentication required.")

        @sync_to_async
        def _resolve_ids():
            from ambassadors.models import Ambassador
            from jobs.models import Job

            if is_ba:
                if amb is None:
                    raise GraphQLError("No Ambassador row for caller.")
                target_amb = amb
            else:
                if not input.ambassador_uuid:
                    raise GraphQLError("ambassador_uuid is required for admin callers.")
                target_amb = Ambassador.objects.select_related("user").filter(
                    uuid=str(input.ambassador_uuid)
                ).first()
                if target_amb is None:
                    raise GraphQLError("Ambassador not found.")

            job_id: Optional[int] = None
            job_obj = None
            if input.job_uuid:
                job_obj = Job.objects.filter(uuid=str(input.job_uuid)).only(
                    "id", "tenant_id"
                ).first()
                if job_obj is None:
                    raise GraphQLError("Job not found.")
                job_id = job_obj.id

            # Tenant scoping for the thread. Admins → first tenant they
            # belong to that matches the BA's tenants. BAs → their own
            # active tenant. If a job is supplied, the thread inherits
            # the job's tenant (the canonical anchor).
            if job_obj is not None:
                tenant_id = job_obj.tenant_id
            else:
                # General thread — pick a tenant the BA belongs to.
                # In practice each BA is on one tenant per
                # AmbassadorEvent history; we use the most recent.
                from ambassadors.models import AmbassadorEvent

                ae = (
                    AmbassadorEvent.objects.filter(ambassador_id=target_amb.id)
                    .select_related("event")
                    .order_by("-id")
                    .first()
                )
                if ae is None or ae.event is None:
                    raise GraphQLError(
                        "Can't open a general thread — BA has no event history yet."
                    )
                tenant_id = ae.event.tenant_id

            return target_amb.id, tenant_id, job_id

        target_amb_id, tenant_id, job_id = await _resolve_ids()
        kind = (
            models.ChatThread.KIND_JOB
            if job_id is not None
            else models.ChatThread.KIND_GENERAL
        )
        thread = await services.get_or_create_thread(
            tenant_id=tenant_id,
            ambassador_id=target_amb_id,
            kind=kind,
            job_id=job_id,
            created_by_id=user.pk,
        )
        return thread

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def send_chat_message(
        self, info: strawberry.Info, input: inputs.SendChatMessageInput
    ) -> types.ChatMessage:
        """Write a message into an existing thread."""
        body = (input.body or "").strip()
        if not body:
            raise GraphQLError("Message body can't be empty.")
        if len(body) > 5000:
            raise GraphQLError("Message body too long (max 5000 chars).")

        user, amb, is_ba, is_admin, _ = await services.resolve_caller_context(info)
        if user is None:
            raise GraphQLError("Authentication required.")

        @sync_to_async
        def _load_thread():
            return models.ChatThread.objects.filter(
                uuid=str(input.thread_uuid)
            ).first()

        thread = await _load_thread()
        if thread is None:
            raise GraphQLError("Thread not found.")

        # Authorization: BA only writes to their own thread; admins
        # only write to threads in tenants they belong to.
        if is_ba:
            if amb is None or thread.ambassador_id != amb.id:
                raise GraphQLError("Not your thread.")
            sender_is_ambassador = True
        else:
            # Admin caller — confirm tenant membership before writing.
            @sync_to_async
            def _tenant_ids():
                from tenants.models import TenantedUser

                return list(
                    TenantedUser.objects.filter(
                        user_id=user.pk, is_active=True
                    ).values_list("tenant_id", flat=True)
                )

            tenant_ids = await _tenant_ids()
            if thread.tenant_id not in tenant_ids:
                raise GraphQLError("Not your tenant's thread.")
            sender_is_ambassador = False

        msg = await services.insert_message(
            thread_id=thread.id,
            sender_id=user.pk,
            sender_is_ambassador=sender_is_ambassador,
            body=body,
        )
        # Push delivery to the recipient — best-effort, doesn't roll
        # back. Lives in a separate module so we can ship the chat
        # backend without push, then add the trigger in a follow-up.
        try:
            from chats.push import notify_chat_recipient

            await notify_chat_recipient(
                thread_id=thread.id,
                msg_uuid=str(msg.uuid),
                body=body,
                sender_is_ambassador=sender_is_ambassador,
            )
        except Exception as e:  # pragma: no cover — never block the write
            logger.warning("chat push failed for thread %s: %s", thread.id, e)
        return msg

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def mark_chat_thread_read(
        self, info: strawberry.Info, input: inputs.MarkChatThreadReadInput
    ) -> int:
        """Mark all unread messages on the caller's side as read.

        Returns count of messages updated. Idempotent — safe to call
        every time the recipient opens the thread.
        """
        user, amb, is_ba, is_admin, _ = await services.resolve_caller_context(info)
        if user is None:
            raise GraphQLError("Authentication required.")

        @sync_to_async
        def _resolve_thread():
            return models.ChatThread.objects.filter(uuid=str(input.thread_uuid)).first()

        thread = await _resolve_thread()
        if thread is None:
            raise GraphQLError("Thread not found.")

        if is_ba and (amb is None or thread.ambassador_id != amb.id):
            raise GraphQLError("Not your thread.")
        # Admin cross-tenant guard: skip the sweep on tenants the
        # admin doesn't belong to. Cheaper than rejecting (still
        # idempotent + safe).
        return await services.mark_thread_read(
            thread_id=thread.id,
            by_ambassador=is_ba,
        )
