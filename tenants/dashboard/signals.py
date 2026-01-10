"""
Django signals for dashboard cache invalidation.

This module handles automatic cache invalidation when models change
that affect dashboard queries.
"""
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.core.cache import cache

from events import models as event_models
from jobs import models as job_models
from ambassadors import models as ambassador_models


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

