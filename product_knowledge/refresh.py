"""Refresh one brand's knowledge: its documents and/or its website."""

from __future__ import annotations

import logging

from django.utils import timezone

from . import fetch as fetch_mod
from .crawler import crawl_website, normalize_site_url
from .documents import refresh_documents
from .models import KnowledgeRefresh

logger = logging.getLogger(__name__)

SCOPES = ("all", "website", "documents")
# The weekly cron gives a brand a few minutes; the staff "Refresh now"
# button waits on the request, so it gets a shorter budget.
CRON_BUDGET_SECONDS = 240
INTERACTIVE_BUDGET_SECONDS = 75


def refresh_tenant(
    tenant, *, scope: str = "all", budget_seconds: float = CRON_BUDGET_SECONDS
) -> dict:
    """Run the refresh and record it. Never raises; returns per-scope stats."""
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")
    deadline = fetch_mod.Deadline(budget_seconds)
    out: dict = {}
    fetcher = fetch_mod.make_fetcher()
    try:
        plan = []
        if scope in ("all", "documents"):
            plan.append((KnowledgeRefresh.SCOPE_DOCUMENTS, refresh_documents))
        if scope in ("all", "website") and normalize_site_url(tenant.brand_website_url):
            plan.append((KnowledgeRefresh.SCOPE_WEBSITE, crawl_website))
        for name, fn in plan:
            log = KnowledgeRefresh.objects.create(tenant_id=tenant.id, scope=name)
            try:
                stats = fn(tenant, fetcher, deadline=deadline)
            except Exception as exc:  # noqa: BLE001 — one brand never stops the batch
                logger.exception(
                    "knowledge refresh failed tenant=%s scope=%s", tenant.id, name
                )
                stats = {
                    "complete": False,
                    "message": f"Refresh failed ({type(exc).__name__}).",
                }
            log.finished_at = timezone.now()
            log.ok = bool(stats.get("complete"))
            log.stats = stats
            log.message = str(stats.get("message") or "")[:300]
            log.save()
            out[name] = stats
    finally:
        fetcher.close()
    return out


def tenants_to_refresh():
    """Brands that have a website set or any document a refresh could read."""
    from academy.models import TrainingResource
    from jobs.models import BriefingTemplate, JobBriefingAttachment
    from tenants.models import Tenant

    from .models import KnowledgeSource

    ids = set(Tenant.objects.exclude(brand_website_url="").values_list("id", flat=True))
    ids |= set(
        TrainingResource.objects.filter(hub__is_active=True, published=True)
        .exclude(kind="video")
        .values_list("hub__tenant_id", flat=True)
    )
    ids |= set(
        BriefingTemplate.objects.filter(is_archived=False).values_list(
            "tenant_id", flat=True
        )
    )
    ids |= set(
        JobBriefingAttachment.objects.values_list(
            "job__tenant_id", flat=True
        ).distinct()
    )
    ids |= set(
        Tenant.objects.filter(checkin_resources__isnull=False).values_list(
            "id", flat=True
        )
    )
    ids |= set(KnowledgeSource.objects.values_list("tenant_id", flat=True))
    return sorted(i for i in ids if i)
