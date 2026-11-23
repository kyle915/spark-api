"""Shared dismissals for Today's attention strip items.

Ignite staff discard a notice for **every** admin — stored as
:class:`brand_insights.models.AttentionDismissal`, keyed by
``tenant`` + ``fingerprint`` (``{bucket}:{ref_id}``). Restoring stamps
``restored_at`` instead of deleting so who discarded what stays on record.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.utils import timezone

VALID_BUCKETS: frozenset[str] = frozenset(
    {"receipt_checks", "missing_recaps", "unstaffed", "rebooks"}
)


class AttentionDismissalError(ValueError):
    """A dismiss / restore request that cannot be carried out as asked."""


def fingerprint(bucket: str, ref_id: str) -> str:
    return f"{bucket}:{ref_id}"


def active_dismissals() -> QuerySet:
    from brand_insights.models import AttentionDismissal

    return AttentionDismissal.objects.filter(restored_at__isnull=True)


def dismissed_refs_by_bucket(tenant_id: int) -> dict[str, set[str]]:
    """Active dismissed ``ref_id`` values per bucket for one tenant."""
    out: dict[str, set[str]] = {b: set() for b in VALID_BUCKETS}
    for bucket, ref_id in active_dismissals().filter(tenant_id=tenant_id).values_list(
        "bucket", "ref_id"
    ):
        if bucket in out and ref_id:
            out[bucket].add(str(ref_id))
    return out


def dismiss_attention_items(
    *,
    user,
    tenant,
    items: list[tuple[str, str]],
) -> list:
    """Permanently discard attention notices for all admins on this tenant.

    ``items`` is a list of ``(bucket, ref_id)``. Idempotent: already-active
    fingerprints are skipped. Returns the rows written (new only).
    """
    from brand_insights.models import AttentionDismissal

    if not items:
        raise AttentionDismissalError("Nothing to discard.")

    cleaned: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for bucket, ref_id in items:
        b = (bucket or "").strip()
        r = (ref_id or "").strip()
        if b not in VALID_BUCKETS:
            raise AttentionDismissalError(f"Unknown attention bucket: {bucket!r}.")
        if not r:
            raise AttentionDismissalError("Each item needs a ref id.")
        fp = fingerprint(b, r)
        if fp in seen:
            continue
        seen.add(fp)
        cleaned.append((b, r, fp))

    written: list[AttentionDismissal] = []
    with transaction.atomic():
        active_fps = set(
            active_dismissals()
            .filter(tenant_id=tenant.id, fingerprint__in=[fp for _, _, fp in cleaned])
            .values_list("fingerprint", flat=True)
        )
        for bucket, ref_id, fp in cleaned:
            if fp in active_fps:
                continue
            row = AttentionDismissal(
                tenant=tenant,
                bucket=bucket,
                ref_id=ref_id,
                fingerprint=fp,
                dismissed_by=user,
            )
            try:
                with transaction.atomic():
                    row.save()
            except IntegrityError:
                continue
            written.append(row)
    return written


def restore_attention_items(
    *,
    user,
    tenant,
    fingerprints: list[str] | None = None,
    items: list[tuple[str, str]] | None = None,
) -> int:
    """Undo discards so the notices can reappear for every admin."""
    fps: set[str] = set()
    if fingerprints:
        fps.update(str(f).strip() for f in fingerprints if str(f).strip())
    if items:
        for bucket, ref_id in items:
            b = (bucket or "").strip()
            r = (ref_id or "").strip()
            if b and r:
                fps.add(fingerprint(b, r))
    if not fps:
        raise AttentionDismissalError("Nothing to restore.")

    now = timezone.now()
    return (
        active_dismissals()
        .filter(tenant_id=tenant.id, fingerprint__in=fps)
        .update(restored_at=now, restored_by=user)
    )
