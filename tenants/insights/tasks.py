"""
RQ jobs for AI insights generation.
"""
import logging
from datetime import date, timedelta

from django_rq import job
from django.utils import timezone
from rq import Retry

from tenants.models import Tenant
from tenants.insights.service import InsightsService
from utils.queues import Queues

logger = logging.getLogger(__name__)

# Batch size for processing tenants to avoid memory issues
TENANT_BATCH_SIZE = 10


@job("default", retry=Retry(max=3, interval=[60, 120, 240]))
def generate_insights_for_tenant(
    tenant_id: int, from_date: date = None, to_date: date = None
):
    """
    Generate insights for a specific tenant.

    Args:
        tenant_id: ID of the tenant to generate insights for
        from_date: Start date for feedback analysis (defaults to 24 hours ago)
        to_date: End date for feedback analysis (defaults to now)
    """
    try:
        tenant = Tenant.objects.get(id=tenant_id)

        # Set default date range if not provided (last 24 hours)
        if not to_date:
            to_date = timezone.now().date()
        if not from_date:
            from_date = to_date - timedelta(days=1)

        logger.info(
            f"Generating insights for tenant {tenant_id} "
            f"(date range: {from_date} to {to_date})"
        )

        # Initialize service and generate insights
        service = InsightsService(tenant)
        insights = service.generate_insights(from_date=from_date, to_date=to_date)

        logger.info(
            f"Successfully generated insights {insights.id} for tenant {tenant_id} "
            f"with {insights.total_feedback_count} feedback records analyzed"
        )

    except Tenant.DoesNotExist:
        logger.error(f"Tenant {tenant_id} not found")
        # Don't retry on missing tenant
        raise
    except ValueError as e:
        logger.warning(
            f"Cannot generate insights for tenant {tenant_id}: {e}. "
            "This may be expected if no feedback records exist."
        )
        # Don't retry on validation errors (e.g., no feedback records)
        return
    except Exception as exc:
        logger.error(
            f"Error generating insights for tenant {tenant_id}: {exc}",
            exc_info=True,
        )
        # RQ will automatically retry on exception (up to max retries with exponential backoff)
        raise


@job("default", retry=Retry(max=3, interval=[60, 120, 240]))
def generate_insights_for_all_tenants(from_date: date = None, to_date: date = None):
    """
    Generate insights for all active tenants.

    Args:
        from_date: Start date for feedback analysis (defaults to 24 hours ago)
        to_date: End date for feedback analysis (defaults to now)
    """
    try:
        # Set default date range if not provided (last 24 hours)
        if not to_date:
            to_date = timezone.now().date()
        if not from_date:
            from_date = to_date - timedelta(days=1)

        # Get all tenants
        tenants_qs = Tenant.objects.all().values_list("id", flat=True)

        queues: Queues = Queues()
        offset = 0
        total_queued = 0

        while True:
            tenant_ids_page = list(tenants_qs[offset : offset + TENANT_BATCH_SIZE])

            if not tenant_ids_page:
                break

            for tenant_id in tenant_ids_page:
                queues.default.add(
                    generate_insights_for_tenant, tenant_id, from_date, to_date
                )

            total_queued += len(tenant_ids_page)
            offset += TENANT_BATCH_SIZE

            logger.debug(
                f"Queued batch of {len(tenant_ids_page)} tenants for insights generation "
                f"(total queued: {total_queued})"
            )

        logger.info(
            f"Queued insights generation for {total_queued} tenants "
            f"(date range: {from_date} to {to_date})"
        )

    except Exception as exc:
        logger.error(f"Error queuing insights generation for all tenants: {exc}")
        # RQ will automatically retry on exception (up to max retries with exponential backoff)
        raise
