"""Writing extracted text into ``KnowledgeSource`` rows, under a per-brand cap."""

from __future__ import annotations

import hashlib

from django.db.models import Sum
from django.db.models.functions import Length
from django.utils import timezone

from .extract import clip
from .models import KnowledgeSource

# Hard ceiling on stored text per brand (all sources together). Retrieval
# reads all of it per question, and the crawl is bounded anyway; this stops a
# huge PDF or a sprawling site from crowding everything else out.
TENANT_TEXT_MAX_CHARS = 400_000


def sha256(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    return hashlib.sha256(data).hexdigest()


def _room_left(tenant_id: int, ref: str) -> int:
    used = (
        KnowledgeSource.objects.filter(tenant_id=tenant_id, excluded=False)
        .exclude(ref=ref)
        .aggregate(n=Sum(Length("text")))["n"]
        or 0
    )
    return max(0, TENANT_TEXT_MAX_CHARS - used)


def existing(tenant_id: int, ref: str) -> KnowledgeSource | None:
    return KnowledgeSource.objects.filter(tenant_id=tenant_id, ref=ref).first()


def touch(row: KnowledgeSource) -> None:
    row.checked_at = timezone.now()
    row.save(update_fields=["checked_at", "updated_at"])


def save_source(
    *,
    tenant_id: int,
    kind: str,
    ref: str,
    url: str = "",
    title: str = "",
    text: str = "",
    raw_hash: str = "",
    error: str = "",
) -> tuple[KnowledgeSource, bool]:
    """Create or update one source. Returns ``(row, text_changed)``.

    ``error`` marks the source unreadable but keeps any text it had, so a
    transient outage doesn't blank a page that answered questions yesterday.
    """
    now = timezone.now()
    row = existing(tenant_id, ref)
    if row is None:
        row = KnowledgeSource(tenant_id=tenant_id, ref=ref[:600], kind=kind)
    row.kind = kind
    row.url = (url or "")[:1024]
    if title:
        row.title = title[:300]
    row.checked_at = now
    changed = False
    if error:
        row.status = KnowledgeSource.STATUS_ERROR if not row.text else row.status
        row.error = error[:300]
    else:
        room = _room_left(tenant_id, ref)
        if len(text) > room:
            text = clip(text, room)
        new_hash = sha256(text)
        changed = new_hash != row.content_hash
        row.text = text
        row.content_hash = new_hash
        row.raw_hash = raw_hash or new_hash
        row.status = (
            KnowledgeSource.STATUS_OK if text.strip() else KnowledgeSource.STATUS_EMPTY
        )
        row.error = "" if text.strip() or room else "brand-text-cap-reached"
        if changed or row.extracted_at is None:
            row.extracted_at = now
    row.save()
    return row, changed
