"""Recap signals — keep the recap-data sheet export fresh on every save.

When a tenant opts in (Tenant.recap_export_on_submit), a recap save mirrors the
tenant's recap data into its export sheet immediately, instead of waiting for
the nightly cron. Pushed via the queue when one exists; falls back to inline
(Cloud Run has no Redis worker — see project notes). A Sheets failure never
breaks the recap save.
"""
from __future__ import annotations

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from recaps.models import CustomRecap
from utils.queues import Queues

logger = logging.getLogger(__name__)

queues: Queues = Queues()


@receiver(post_save, sender=CustomRecap)
def mirror_recap_export_on_save(sender, instance: CustomRecap, **kwargs):
    tenant = getattr(instance, "tenant", None)
    if tenant is None or not getattr(tenant, "recap_export_on_submit", False):
        return
    try:
        from recaps.recap_sheet_export import refresh_recap_export

        try:
            queues.default.add(refresh_recap_export, tenant)
        except Exception:
            try:
                refresh_recap_export(tenant)
            except Exception as exc:
                logger.warning(
                    "recap export inline failed for tenant=%s: %s",
                    getattr(tenant, "id", None),
                    exc,
                )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "recap export signal failed for tenant=%s: %s",
            getattr(tenant, "id", None),
            exc,
        )
