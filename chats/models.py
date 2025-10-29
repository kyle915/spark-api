from uuid6 import uuid7
from django.db import models
from django.conf import settings
from ambassadors.models import Ambassador
from tenants.models import Tenant
from jobs.models import Job


# ChatRooms
class JobChatRoom(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="job_chat_rooms",
    )

    ambassador = models.ForeignKey(
        Ambassador, on_delete=models.RESTRICT, null=False, related_name="job_chat_rooms"
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="job_chat_rooms_users",
    )

    job = models.ForeignKey(
        Job, on_delete=models.RESTRICT, null=False, related_name="job_chat_rooms"
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="job_chat_rooms_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="job_chat_rooms_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class ChatroomAmbassadorMessage(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    message = models.TextField()

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="chatroom_ambassador_messages",
    )

    ambassador = models.ForeignKey(
        Ambassador,
        on_delete=models.RESTRICT,
        null=False,
        related_name="chatroom_ambassador_messages",
    )

    job_chatroom = models.ForeignKey(
        JobChatRoom,
        on_delete=models.RESTRICT,
        null=False,
        related_name="chatroom_ambassador_messages",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="chatroom_ambassador_messages_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="chatroom_ambassador_messages_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class ChatroomCompanieMessage(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    message = models.TextField()

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="chatroom_companies_messages",
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="chatroom_companies_messages",
    )

    job_chatroom = models.ForeignKey(
        JobChatRoom,
        on_delete=models.RESTRICT,
        null=False,
        related_name="chatroom_companies_messages",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="chatroom_companies_messages_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="chatroom_companies_messages_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)
