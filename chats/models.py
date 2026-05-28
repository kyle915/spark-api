"""Spark chat data model.

Two kinds of conversations sit on the same `ChatThread` row:

  - kind="general": persistent BA ↔ tenant-admin direct message thread.
    One per (tenant, ambassador). Used for "hey can I get next
    Saturday off" style chatter that doesn't belong to a specific gig.

  - kind="job": per-shift / per-job thread pinned to a Job row. Created
    on demand the first time the BA or an admin opens chat from a job
    context. Auto-archives N days after the related event ends (the
    archive sweep is a cron — not implemented in this initial drop;
    threads are kept open until then).

Messages live on `ChatMessage`. We denormalize `sender_is_ambassador` at
write-time because reading `user.role` inside the async GraphQL
resolvers is unreliable (the FK isn't hydrated by the JWT request user)
— the same gotcha that bit IsClientOrSparkAdmin and the tenant/user
resolvers. Caching the boolean at the write boundary lets read-side
filters (`unread for this BA`, `unread for any tenant-admin`) stay
in pure SQL with no per-row user-role lookup.

Unread state is tracked with two nullable timestamps on each message —
one for the admin side and one for the BA side. The sender's side is
filled at write time (they read their own message); the other side
stays null until the recipient opens the thread and the
`markChatThreadRead` mutation sweeps it forward.

The previous JobChatRoom / ChatroomAmbassadorMessage / ChatroomCompanieMessage
models were scaffolding and never exposed via GraphQL. They're replaced
in-place here (the accompanying migration drops them).
"""
from uuid6 import uuid7
from django.db import models
from django.conf import settings
from ambassadors.models import Ambassador
from tenants.models import Tenant
from jobs.models import Job


class ChatThread(models.Model):
    """A conversation between an Ambassador and the tenant-admin side.

    Exactly one thread per (tenant, ambassador, kind, job) tuple — see
    the partial UniqueConstraints below. `job` is nullable for general
    threads and required for job threads (enforced in the service
    layer; can't be expressed cleanly as a model-level check across
    both kinds without two partial uniques).
    """

    KIND_GENERAL = "general"
    KIND_JOB = "job"
    KIND_CHOICES = (
        (KIND_GENERAL, "General BA ↔ admin DM"),
        (KIND_JOB, "Per-job thread"),
    )

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    kind = models.CharField(
        max_length=16, choices=KIND_CHOICES, default=KIND_GENERAL
    )

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="chat_threads",
    )
    ambassador = models.ForeignKey(
        Ambassador,
        on_delete=models.RESTRICT,
        null=False,
        related_name="chat_threads",
    )
    job = models.ForeignKey(
        Job,
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="chat_threads",
    )

    # Recency + preview, both updated by sendChatMessage so the thread
    # list can sort + preview without a join into ChatMessage.
    last_message_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_message_preview = models.CharField(max_length=255, null=True, blank=True)
    last_message_sender_is_ambassador = models.BooleanField(default=False)

    # Soft archive — non-null means the thread is hidden from the
    # default thread list. Cron sweeps job-kind threads N days after
    # the event ends; admins can also archive manually.
    archived_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="chat_threads_created_by",
    )
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-last_message_at", "-created_at")
        constraints = [
            # One general thread per (tenant, ambassador). Partial because
            # job-kind rows have a non-null job and we want them to slot
            # in separately.
            models.UniqueConstraint(
                fields=["tenant", "ambassador"],
                condition=models.Q(kind="general"),
                name="chats_thread_one_general_per_pair",
            ),
            # One job thread per (tenant, ambassador, job). Excludes
            # the general rows where job IS NULL.
            models.UniqueConstraint(
                fields=["tenant", "ambassador", "job"],
                condition=models.Q(kind="job"),
                name="chats_thread_one_job_per_triple",
            ),
        ]


class ChatMessage(models.Model):
    """A single message inside a ChatThread.

    sender_is_ambassador is set at write-time from the resolver — the
    service layer flips it True when the BA's user is the sender,
    False for anyone admin-side. Read-side queries filter on this
    instead of traversing sender.role (which fails under async-context
    JWT user hydration).
    """

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    thread = models.ForeignKey(
        ChatThread,
        on_delete=models.RESTRICT,
        null=False,
        related_name="messages",
    )

    body = models.TextField()

    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="chat_messages_sent",
    )
    sender_is_ambassador = models.BooleanField()

    # Two-sided unread tracking. The sender's side is auto-filled at
    # write time (you've read your own message); the other side stays
    # null until markChatThreadRead sweeps it forward when the
    # recipient opens the thread.
    read_by_admin_at = models.DateTimeField(null=True, blank=True)
    read_by_ambassador_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)

    class Meta:
        ordering = ("created_at",)
        indexes = [
            # Powers per-thread paginated load (newest-first lists hop
            # back through this ordering with ORDER BY DESC LIMIT).
            models.Index(fields=["thread", "created_at"]),
            # Powers unread-count badges:
            #   admin unread = sender_is_ambassador=True AND read_by_admin_at IS NULL
            #   BA unread    = sender_is_ambassador=False AND read_by_ambassador_at IS NULL
            models.Index(fields=["thread", "sender_is_ambassador"]),
        ]
