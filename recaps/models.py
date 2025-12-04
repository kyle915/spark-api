from uuid6 import uuid7
from django.db import models
from django.conf import settings
from ambassadors.models import FileType
from events.models import Event


class RecapFile(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=100, null=False)
    file = models.FileField(upload_to='recap_files/', null=True)
    approved = models.BooleanField(default=False)

    file_type = models.ForeignKey(
        FileType,
        on_delete=models.RESTRICT,
        null=False,
        related_name="recap_files",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="recap_files_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="recap_files_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class Recap(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=100, null=False)

    event = models.ForeignKey(
        Event, on_delete=models.RESTRICT, null=False, related_name="recaps"
    )
    recap_file = models.ForeignKey(
        RecapFile,
        on_delete=models.RESTRICT,
        null=False,
        related_name="recaps",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="recaps_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="recaps_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

class RecapRecapFile(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    
    recap_file = models.ForeignKey(
        RecapFile,
        on_delete=models.RESTRICT,
        null=False,
        related_name="recap_recap_file",
    )

    recap = models.ForeignKey(
        Recap,
        on_delete=models.RESTRICT,
        null=False,
        related_name="recap_recap_file",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="recap_recap_file_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="recap_recap_file_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)