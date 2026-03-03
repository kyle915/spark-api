"""
RQ jobs for dashboard-related background work (e.g. goals creation).
"""
import logging

from django_rq import job
from rq import Retry

from tenants.dashboard.goals_service import ensure_goals_for_tenant_users
from tenants.models import Tenant
from utils.queues import Queues

logger = logging.getLogger(__name__)

# Chunk size when iterating tenants for all-tenants job (avoids loading all IDs into memory).
ENQUEUE_TENANTS_CHUNK_SIZE = 500


@job("default", retry=Retry(max=3, interval=[60, 120, 240]))
def create_goals_for_tenant(tenant_id: int, year: int) -> int:
    """
    Create Goal rows for every user in the tenant for the given year.
    Only creates missing goals; existing goals are left unchanged.

    Args:
        tenant_id: ID of the tenant.
        year: Year (e.g. 2025).

    Returns:
        Number of new Goal rows created.
    """
    try:
        created = ensure_goals_for_tenant_users(tenant_id, year)
        logger.info(
            f"Created {created} goals for tenant {tenant_id} year {year}"
        )
        return created
    except Exception as exc:
        logger.error(
            f"Error creating goals for tenant {tenant_id} year {year}: {exc}",
            exc_info=True,
        )
        raise


@job("default", retry=Retry(max=2, interval=[120, 300]))
def create_goals_for_all_tenants(year: int) -> int:
    """
    Enqueue one create_goals_for_tenant(tenant_id, year) job per tenant.
    Designed for 1000+ tenants: this task only enqueues jobs and returns quickly;
    actual work is done by per-tenant workers.

    Args:
        year: Year (e.g. 2025).

    Returns:
        Number of jobs enqueued (one per tenant).
    """
    queue = Queues().default
    enqueued = 0
    tenant_ids = Tenant.objects.values_list("id", flat=True).iterator(
        chunk_size=ENQUEUE_TENANTS_CHUNK_SIZE
    )
    for tenant_id in tenant_ids:
        queue.add(create_goals_for_tenant, tenant_id, year)
        enqueued += 1
    logger.info(f"Enqueued {enqueued} create_goals_for_tenant jobs for year {year}")
    return enqueued
