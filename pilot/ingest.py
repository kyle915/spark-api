"""Automated POS CSV ingestion: feed configs, inbound drops, processing."""

from __future__ import annotations

import hashlib
import secrets
from typing import Any

from django.db import transaction
from django.utils import timezone

from pilot.models import PosFeedConfig, PosFeedDrop, RetailerSalesImport
from pilot.permissions import ResolvedWorkspaceAccess, log_workspace_audit
from pilot.pos import import_retailer_pos_csv, system_workspace_access
from pilot.pos_presets import POS_PRESETS, list_pos_presets


def mint_ingest_token() -> str:
    return secrets.token_urlsafe(24)


def upsert_pos_feed_config(
    access: ResolvedWorkspaceAccess,
    *,
    retailer_banner: str,
    preset_key: str = "",
    source_kind: str = PosFeedConfig.SOURCE_EMAIL_DROP,
    enabled: bool = True,
    default_feed_completeness: str = RetailerSalesImport.COMPLETENESS_COMPLETE,
    notes: str = "",
    remint_token: bool = False,
    feed_uuid: str | None = None,
) -> PosFeedConfig:
    if not access.can_manage_pos:
        raise PermissionError(
            "You do not have permission to configure automated POS feeds."
        )
    banner = (retailer_banner or "").strip()
    if not banner:
        raise ValueError("Retailer banner is required.")
    preset = (preset_key or "").strip()
    if preset and preset not in POS_PRESETS:
        raise ValueError(f"Unknown POS preset_key: {preset}")
    if source_kind not in {
        PosFeedConfig.SOURCE_EMAIL_DROP,
        PosFeedConfig.SOURCE_MANUAL_UPLOAD,
        PosFeedConfig.SOURCE_SFTP,
    }:
        raise ValueError(f"Invalid source_kind: {source_kind}")
    if default_feed_completeness not in {
        RetailerSalesImport.COMPLETENESS_COMPLETE,
        RetailerSalesImport.COMPLETENESS_PARTIAL,
        RetailerSalesImport.COMPLETENESS_DELAYED,
    }:
        raise ValueError(
            f"Invalid default_feed_completeness: {default_feed_completeness}"
        )

    cfg: PosFeedConfig | None = None
    if feed_uuid:
        cfg = PosFeedConfig.objects.filter(
            tenant=access.tenant, uuid=feed_uuid
        ).first()
        if cfg is None:
            raise ValueError("POS feed config not found.")
    else:
        cfg = PosFeedConfig.objects.filter(
            tenant=access.tenant,
            retailer_banner__iexact=banner,
            preset_key=preset,
        ).first()

    created = False
    if cfg is None:
        cfg = PosFeedConfig(
            tenant=access.tenant,
            retailer_banner=banner,
            preset_key=preset,
            ingest_token=mint_ingest_token(),
            created_by=access.user,
        )
        created = True
    else:
        cfg.retailer_banner = banner
        cfg.preset_key = preset
        if remint_token or not cfg.ingest_token:
            cfg.ingest_token = mint_ingest_token()

    cfg.source_kind = source_kind
    cfg.enabled = bool(enabled)
    cfg.default_feed_completeness = default_feed_completeness
    cfg.notes = (notes or "")[:4000]
    cfg.save()

    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="pos",
        action="pos_feed_config_upserted" if not created else "pos_feed_config_created",
        summary=(
            f"{'Created' if created else 'Updated'} POS feed for "
            f"{cfg.retailer_banner} (preset={cfg.preset_key or 'auto'}, "
            f"enabled={cfg.enabled})."
        ),
        metadata={
            "feed_uuid": str(cfg.uuid),
            "preset_key": cfg.preset_key,
            "source_kind": cfg.source_kind,
            "enabled": cfg.enabled,
        },
    )
    return cfg


def list_pos_feed_configs(access: ResolvedWorkspaceAccess) -> list[PosFeedConfig]:
    if not access.can_view_brand_consolidated:
        raise PermissionError("Outside providers cannot view POS feed configs.")
    return list(
        PosFeedConfig.objects.filter(tenant=access.tenant).order_by(
            "retailer_banner", "id"
        )
    )


def list_recent_pos_feed_drops(
    access: ResolvedWorkspaceAccess, *, limit: int = 40
) -> list[PosFeedDrop]:
    if not access.can_view_brand_consolidated:
        raise PermissionError("Outside providers cannot view POS feed drops.")
    return list(
        PosFeedDrop.objects.select_related("feed_config", "sales_import")
        .filter(tenant=access.tenant)
        .order_by("-received_at")[:limit]
    )


@transaction.atomic
def enqueue_pos_feed_drop(
    *,
    ingest_token: str,
    csv_text: str,
    filename: str = "pos.csv",
    from_email: str = "",
    message_id: str = "",
) -> PosFeedDrop:
    """Accept an inbound CSV for a feed token. Does not import yet."""
    token = (ingest_token or "").strip()
    cfg = (
        PosFeedConfig.objects.select_related("tenant")
        .filter(ingest_token=token, enabled=True)
        .first()
    )
    if cfg is None:
        raise PermissionError("Invalid or disabled POS feed token.")
    cleaned = (csv_text or "").lstrip("\ufeff")
    if not cleaned.strip():
        raise ValueError("POS CSV payload is empty.")
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()

    # Idempotent skip when the exact same file was already accepted recently.
    prior = (
        PosFeedDrop.objects.filter(
            feed_config=cfg,
            file_sha256=digest,
            status__in={
                PosFeedDrop.STATUS_PENDING,
                PosFeedDrop.STATUS_PROCESSING,
                PosFeedDrop.STATUS_PROCESSED,
            },
        )
        .order_by("-received_at")
        .first()
    )
    if prior is not None:
        return prior

    return PosFeedDrop.objects.create(
        tenant=cfg.tenant,
        feed_config=cfg,
        status=PosFeedDrop.STATUS_PENDING,
        filename=(filename or "pos.csv")[:255],
        csv_text=cleaned,
        file_sha256=digest,
        source_from_email=(from_email or "")[:254],
        source_message_id=(message_id or "")[:255],
    )


def _claim_pos_feed_drop(drop: PosFeedDrop) -> PosFeedDrop | None:
    """Atomically transition pending → processing. Returns None if lost race."""
    claimed = (
        PosFeedDrop.objects.filter(
            pk=drop.pk, status=PosFeedDrop.STATUS_PENDING
        ).update(status=PosFeedDrop.STATUS_PROCESSING)
    )
    if claimed != 1:
        return None
    drop.refresh_from_db()
    return drop


def process_pos_feed_drop(drop: PosFeedDrop) -> PosFeedDrop:
    """Run ``import_retailer_pos_csv`` for one pending drop.

    Atomically claims the row (pending → processing) before import so a
    concurrent ``process=true`` webhook and the hourly cron cannot both
    create active ``RetailerSalesImport`` versions for the same drop.
    """
    if drop.status == PosFeedDrop.STATUS_PROCESSED and drop.sales_import_id:
        return drop
    if drop.status in {
        PosFeedDrop.STATUS_FAILED,
        PosFeedDrop.STATUS_SKIPPED,
        PosFeedDrop.STATUS_PROCESSING,
    }:
        # Another worker owns processing, or this drop already finished.
        # Re-read so callers see the latest committed state.
        if drop.status == PosFeedDrop.STATUS_PROCESSING:
            drop.refresh_from_db()
        return drop

    claimed = _claim_pos_feed_drop(drop)
    if claimed is None:
        drop.refresh_from_db()
        return drop
    drop = claimed

    cfg = drop.feed_config
    if not cfg.enabled:
        drop.status = PosFeedDrop.STATUS_SKIPPED
        drop.error_message = "Feed config is disabled."
        drop.processed_at = timezone.now()
        drop.save(
            update_fields=["status", "error_message", "processed_at"]
        )
        return drop

    try:
        access = system_workspace_access(drop.tenant, actor=cfg.created_by)
        imp = import_retailer_pos_csv(
            access,
            retailer_banner=cfg.retailer_banner,
            csv_text=drop.csv_text,
            filename=drop.filename,
            preset_key=cfg.preset_key or None,
            feed_completeness=cfg.default_feed_completeness,
        )
        drop.status = PosFeedDrop.STATUS_PROCESSED
        drop.sales_import = imp
        drop.error_message = ""
        drop.processed_at = timezone.now()
        drop.save(
            update_fields=[
                "status",
                "sales_import",
                "error_message",
                "processed_at",
            ]
        )
        PosFeedConfig.objects.filter(pk=cfg.pk).update(
            last_ingested_at=drop.processed_at
        )
    except Exception as exc:  # noqa: BLE001 — persist failure, don't raise
        drop.status = PosFeedDrop.STATUS_FAILED
        drop.error_message = str(exc)[:4000]
        drop.processed_at = timezone.now()
        drop.save(
            update_fields=["status", "error_message", "processed_at"]
        )
    return drop


def process_pending_pos_feed_drops(
    *,
    tenant_id: int | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    qs = PosFeedDrop.objects.select_related(
        "feed_config", "tenant"
    ).filter(status=PosFeedDrop.STATUS_PENDING)
    if tenant_id is not None:
        qs = qs.filter(tenant_id=tenant_id)
    pending = list(qs.order_by("received_at", "id")[:limit])
    counts = {
        "pending": len(pending),
        "processed": 0,
        "failed": 0,
        "skipped": 0,
    }
    for drop in pending:
        process_pos_feed_drop(drop)
        drop.refresh_from_db(fields=["status"])
        if drop.status == PosFeedDrop.STATUS_PROCESSED:
            counts["processed"] += 1
        elif drop.status == PosFeedDrop.STATUS_FAILED:
            counts["failed"] += 1
        elif drop.status == PosFeedDrop.STATUS_SKIPPED:
            counts["skipped"] += 1
    return counts


def pos_presets_payload() -> list[dict[str, Any]]:
    return list_pos_presets()
