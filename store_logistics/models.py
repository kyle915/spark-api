"""Persistent door/account logistics notes for a physical store.

Stored per ``(tenant, place_key)`` so notes survive across past and future
requests/events at the same street address or map cell, and surface
automatically on Store Profile / StoreSheet as well as BA shift briefings.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from uuid6 import uuid7


class StoreLogisticsNote(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="store_logistics_notes",
    )
    place_key = models.CharField(max_length=128)
    store_key_hint = models.CharField(max_length=128, blank=True, default="", db_index=True)
    store_name = models.CharField(max_length=255, blank=True, default="")
    address = models.TextField(blank=True, default="")
    notes = models.TextField(blank=True, default="")

    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="store_logistics_notes_updated",
    )
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "place_key"],
                name="uniq_store_logistics_note_tenant_place",
            )
        ]
        indexes = [
            models.Index(fields=["tenant", "store_key_hint"]),
        ]

    def __str__(self) -> str:
        label = self.store_name or self.store_key_hint or self.place_key
        return f"StoreLogisticsNote(tenant={self.tenant_id} store={label})"
