"""
Django signals for dashboard cache invalidation.

This module handles automatic cache invalidation when models change
that affect dashboard queries.
"""
import logging

from django.contrib.auth import get_user_model
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.core.cache import cache

from events import models as event_models
from jobs import models as job_models
from ambassadors import models as ambassador_models
from tenants import models as tenant_models

logger = logging.getLogger(__name__)


def _invalidate_dashboard_cache(tenant_id: int, query_names: list[str]):
    """
    Invalidate dashboard cache for specific queries and tenant.
    
    Since Django's default cache doesn't support wildcard deletion,
    we use a version-based approach: increment a version number
    that's part of the cache key.
    
    Args:
        tenant_id: The tenant ID
        query_names: List of query names to invalidate
    """
    for query_name in query_names:
        # Invalidate by setting a version that changes
        # The cache key includes this version, so changing it invalidates all keys
        version_key = f"dashboard:version:{query_name}:{tenant_id}"
        current_version = cache.get(version_key, 0)
        cache.set(version_key, current_version + 1, timeout=None)  # Never expire


@receiver([post_save, post_delete], sender=event_models.Event)
def invalidate_cache_on_event_change(sender, instance, **kwargs):
    """Invalidate cache when Event is created, updated, or deleted."""
    tenant_id = instance.tenant_id
    _invalidate_dashboard_cache(tenant_id, [
        'events_stats',
        'events_time_series',
        'ambassadors_stats',
        'event_detail',
    ])


@receiver([post_save, post_delete], sender=event_models.Request)
def invalidate_cache_on_request_change(sender, instance, **kwargs):
    """Invalidate cache when Request is created, updated, or deleted."""
    tenant_id = instance.tenant_id
    _invalidate_dashboard_cache(tenant_id, [
        'request_stats',
        'request_time_series',
        'event_detail',
    ])


@receiver([post_save, post_delete], sender=job_models.Job)
def invalidate_cache_on_job_change(sender, instance, **kwargs):
    """Invalidate cache when Job is created, updated, or deleted."""
    tenant_id = instance.tenant_id
    _invalidate_dashboard_cache(tenant_id, [
        'ambassadors_stats',
        'request_stats',
        'event_detail',
    ])


@receiver([post_save, post_delete], sender=ambassador_models.AmbassadorEvent)
def invalidate_cache_on_ambassador_event_change(sender, instance, **kwargs):
    """Invalidate cache when AmbassadorEvent is created, updated, or deleted."""
    tenant_id = instance.tenant_id
    _invalidate_dashboard_cache(tenant_id, [
        'ambassadors_stats',
        'event_detail',
    ])


@receiver([post_save, post_delete], sender=job_models.AmbassadorJob)
def invalidate_cache_on_ambassador_job_change(sender, instance, **kwargs):
    """Invalidate cache when AmbassadorJob is created, updated, or deleted."""
    tenant_id = instance.tenant_id
    _invalidate_dashboard_cache(tenant_id, [
        'ambassadors_stats',
        'request_stats',
        'event_detail',
    ])


@receiver([post_save, post_delete], sender=event_models.RequestStatus)
def invalidate_cache_on_request_status_change(sender, instance, **kwargs):
    """Invalidate cache when RequestStatus is created, updated, or deleted."""
    tenant_id = instance.tenant_id
    _invalidate_dashboard_cache(tenant_id, [
        'request_stats',
        'request_time_series',
    ])


@receiver([post_save, post_delete], sender=event_models.EventStatus)
def invalidate_cache_on_event_status_change(sender, instance, **kwargs):
    """Invalidate cache when EventStatus is created, updated, or deleted."""
    tenant_id = instance.tenant_id
    _invalidate_dashboard_cache(tenant_id, [
        'events_stats',
        'events_time_series',
    ])


@receiver(post_save, sender=tenant_models.Tenant)
def link_admins_to_new_tenant(sender, instance, created, raw=False, **kwargs):
    """Grant every spark-admin access to a newly-created tenant.

    Ignite admins are scoped by TenantedUser membership, so a brand-new
    client (e.g. Girl Beer) used to be invisible to existing admins until
    someone re-ran the backfill command — which is how Nevena ended up
    missing Girl Beer. This auto-links all spark-admins the moment a tenant
    is created, so "all admins see all clients" stays true without manual
    steps. Duplicate-safe and best-effort: a failure here must never block
    tenant creation.
    """
    if not created or raw:
        return
    try:
        User = get_user_model()
        admins = User.objects.filter(role__slug=tenant_models.Role.SPARK_ADMIN_SLUG)
        created_count = 0
        for admin in admins:
            existing = tenant_models.TenantedUser.objects.filter(
                user=admin, tenant=instance
            )
            if existing.exists():
                # Reactivate if a stale inactive link exists; never .get()
                # (some (user, tenant) pairs are duplicated in the data).
                existing.filter(is_active=False).update(is_active=True)
            else:
                tenant_models.TenantedUser.objects.create(
                    user=admin, tenant=instance, is_active=True
                )
                created_count += 1
        if created_count:
            logger.info(
                "Auto-linked %s admin(s) to new tenant %s (id=%s)",
                created_count,
                getattr(instance, "name", "?"),
                instance.pk,
            )
    except Exception:
        # Convenience link-up must never break tenant creation.
        logger.exception(
            "Failed to auto-link admins to new tenant id=%s", instance.pk
        )

