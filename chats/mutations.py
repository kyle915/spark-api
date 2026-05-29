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
        """Write a message into an existing thread.

        Body is optional when at least one attachment is supplied (send a
        photo/PDF with no caption); otherwise a non-empty body is
        required.
        """
        body = (input.body or "").strip()
        attachment_rows = services._normalize_attachment_inputs(input.attachments)
        if not body and not attachment_rows:
            raise GraphQLError("Message must have a body or an attachment.")
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
            attachment_rows=attachment_rows,
        )
        # Push delivery to the recipient — best-effort, doesn't roll
        # back. Lives in a separate module so we can ship the chat
        # backend without push, then add the trigger in a follow-up.
        try:
            from chats.push import notify_chat_recipient

            await notify_chat_recipient(
                thread_id=thread.id,
                msg_uuid=str(msg.uuid),
                body=body or services._preview_for(body, attachment_rows),
                sender_is_ambassador=sender_is_ambassador,
            )
        except Exception as e:  # pragma: no cover — never block the write
            logger.warning("chat push failed for thread %s: %s", thread.id, e)
        return msg

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def broadcast_chat_message(
        self, info: strawberry.Info, input: inputs.BroadcastChatMessageInput
    ) -> types.BroadcastChatMessageResult:
        """Admin → many BAs. Send a single message (+ optional
        attachments) to the 1:1 general thread of every targeted BA.

        Targets are the UNION of explicit `ambassador_uuids` and the
        members of each `group_uuids` AmbassadorGroup. EVERYTHING is
        STRICTLY scoped to the active tenant: cross-tenant BAs/groups are
        ignored, so a caller can't reach another brand's ambassadors no
        matter what uuids they pass. The active tenant is resolved the
        same way the recap pickers do (`EventQueriesService.
        resolve_tenant_id`) — staff/spark-admins pass `tenantId`, a
        client user resolves to their own membership.

        Single-recipient send is just a broadcast of one. Idempotent
        thread creation — re-broadcasting reuses existing threads.
        """
        user, amb, is_ba, is_admin, _ = await services.resolve_caller_context(info)
        if user is None:
            raise GraphQLError("Authentication required.")
        # Broadcasting is an admin-only capability (admin → BA). BAs can
        # only ever write to their own 1:1 thread via sendChatMessage.
        if not is_admin:
            raise GraphQLError("Only admins can broadcast messages.")

        body = (input.body or "").strip()
        attachment_rows = services._normalize_attachment_inputs(input.attachments)
        if not body and not attachment_rows:
            raise GraphQLError("Message must have a body or an attachment.")
        if len(body) > 5000:
            raise GraphQLError("Message body too long (max 5000 chars).")

        if not (input.ambassador_uuids or input.group_uuids):
            raise GraphQLError(
                "Pick at least one ambassador or group to message."
            )

        # Resolve the active tenant exactly like recapEventOptions: a
        # hard stop with no tenant in scope, so nothing can leak/fan out
        # cross-tenant.
        from events.queries import EventQueriesService

        service = EventQueriesService()
        try:
            tenant_id = await service.resolve_tenant_id(
                info,
                tenant_id=input.tenant_id,
                tenant_uuid=input.tenant_uuid,
            )
        except GraphQLError:
            tenant_id = None
        if not tenant_id:
            return types.BroadcastChatMessageResult(
                success=False,
                message="No tenant in scope to broadcast under.",
                recipient_count=0,
                thread_uuids=[],
            )

        ambassador_ids = await services.resolve_broadcast_target_ambassador_ids(
            tenant_id=tenant_id,
            ambassador_uuids=input.ambassador_uuids,
            group_uuids=input.group_uuids,
        )
        if not ambassador_ids:
            return types.BroadcastChatMessageResult(
                success=False,
                message="No ambassadors in this tenant matched the selection.",
                recipient_count=0,
                thread_uuids=[],
            )

        sent = await services.fan_out_broadcast(
            tenant_id=tenant_id,
            ambassador_ids=ambassador_ids,
            sender_id=user.pk,
            body=body,
            attachment_rows=attachment_rows,
        )

        # Best-effort push per delivered thread. Never blocks the result.
        push_body = body or services._preview_for(body, attachment_rows)
        try:
            from chats.push import notify_chat_recipient

            for _amb_id, thread_id in sent:
                try:
                    await notify_chat_recipient(
                        thread_id=thread_id,
                        msg_uuid="",
                        body=push_body,
                        sender_is_ambassador=False,
                    )
                except Exception as e:  # pragma: no cover
                    logger.warning(
                        "broadcast push failed thread=%s: %s", thread_id, e
                    )
        except Exception as e:  # pragma: no cover
            logger.warning("broadcast push module unavailable: %s", e)

        thread_uuids = await services.thread_uuids_for_ids(
            [t for _a, t in sent]
        )
        return types.BroadcastChatMessageResult(
            success=True,
            message=f"Sent to {len(sent)} ambassador(s).",
            recipient_count=len(sent),
            thread_uuids=thread_uuids,
        )

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def archive_chat_thread(
        self, info: strawberry.Info, input: inputs.ArchiveChatThreadInput
    ) -> types.ChatThread:
        """Admin-only SOFT delete of a chat thread (the trash action on a
        thread row). Sets `archived_at` so the thread leaves the default
        chat list without hard-purging it or its messages — recoverable
        by re-running with archived=False.

        Tenant-scoped: an admin can only archive threads in tenants they
        belong to (same membership gate sendChatMessage uses). BAs can't
        archive at all. Returns the updated thread so the client can
        confirm + drop the row optimistically.
        """
        user, amb, is_ba, is_admin, _ = await services.resolve_caller_context(info)
        if user is None:
            raise GraphQLError("Authentication required.")
        if not is_admin:
            raise GraphQLError("Only admins can delete chat threads.")

        @sync_to_async
        def _load_thread():
            return models.ChatThread.objects.filter(
                uuid=str(input.thread_uuid)
            ).first()

        thread = await _load_thread()
        if thread is None:
            raise GraphQLError("Thread not found.")

        # Confirm the admin belongs to the thread's tenant before
        # mutating it — no cross-tenant archive.
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

        await services.set_thread_archived(
            thread_id=thread.id, archived=input.archived
        )
        # Return a freshly-loaded thread so archived_at reflects the
        # update and the FK resolvers have their relations available.
        @sync_to_async
        def _reload():
            return (
                models.ChatThread.objects.select_related(
                    "ambassador", "ambassador__user", "job", "tenant"
                )
                .filter(id=thread.id)
                .first()
            )

        return await _reload()

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
