from django.db import models
from asgiref.sync import sync_to_async

from tenants.models import User
from utils.models import BaseManager


class DefaultStatusManager(models.Manager):
    """
    Custom manager for `DefaultStatus` that provides helper shortcuts.
    """

    def get_default(self, tenant_id: int | None = None) -> models.Model | None:
        """
        Return the default status.

        If a tenant is provided, the lookup will be scoped to that tenant.
        Returns `None` when no default status exists.
        """
        queryset = self.get_queryset().filter(is_default=True)

        if tenant_id is not None:
            queryset = queryset.filter(tenant_id=tenant_id)

        return queryset.first()


class ClientManager(BaseManager, models.Manager):
    """Manager for Client model with async support."""
    pass


class RequestStatusManager(DefaultStatusManager):
    """
    Custom manager for `RequestStatus` that provides helper shortcuts.
    """

    def get_for_approval(self, tenant=None) -> models.Model | None:
        """
        Return the status for approval.

        If a tenant is provided, the lookup will be scoped to that tenant.
        Returns `None` when no status for approval exists.
        """
        queryset = self.get_queryset().filter(create_event=True)

        if tenant is not None:
            queryset = queryset.filter(tenant=tenant)

        return queryset.first()

    def get_by_slug(self, slug: str, tenant=None) -> models.Model | None:
        """
        Return the status by slug.

        If a tenant is provided, the lookup will be scoped to that tenant.
        Returns `None` when no status with the given slug exists.
        """
        queryset = self.get_queryset().filter(slug=slug)

        if tenant is not None:
            queryset = queryset.filter(tenant=tenant)

        return queryset.first()


class EventStatusManager(DefaultStatusManager):
    pass


class EventTypeManager(DefaultStatusManager):
    pass


class EventManager(models.Manager):
    """
    Custom manager for `Event` that provides helper shortcuts.
    """

    # Request status slugs that mean "this activation is going ahead", so a
    # materialized Event should land as the tenant's APPROVED EventStatus
    # rather than the default (which is "pending" on every tenant). Mirrors
    # the explicit approved-status resolution `create_event_with_request`
    # already does — keeping the Master Tracker (reads Request.status) and
    # the Event detail page (reads Event.status) in agreement.
    APPROVED_REQUEST_STATUS_SLUGS = frozenset({"approved", "scheduled"})

    async def from_request(
        self,
        request: models.Model,
        created_by: User,
        event_type: models.Model | None = None,
        status: models.Model | None = None,
    ) -> models.Model:
        """Create an event from a request.

        When ``status`` is not supplied, the resolution is:
          * If the parent request is already approved/scheduled (its own
            RequestStatus slug ∈ {approved, scheduled}), use the tenant's
            APPROVED EventStatus (slug="approved") — so an Event created off
            an approved request never silently inherits the tenant default
            "pending". This is the same approved status
            `create_event_with_request` sets explicitly.
          * Otherwise fall back to the tenant's default EventStatus (then any
            tenant/global row) so an Event always materializes.
        Callers may still pass an explicit ``status`` to override this.
        """
        from .models import EventType, EventStatus

        async def get_object_or_default(
            model_class: models.Model,
            tenant_id: int,
            model: models.Model | None = None,
        ) -> models.Model:
            if model:
                return model
            # Prefer the tenant's flagged default. Onboarding frequently
            # forgets to flag one (is_default=True) — which used to make
            # event creation hard-fail on the `if not status` guard below,
            # so an approved request silently never got an Event (and thus
            # no Assign-BA / Post-to-board / auto-job). Fall back to ANY
            # row for the tenant, then any global row, so an Event always
            # materializes. Mirrors the auto-job signal's default-or-first.
            obj = await sync_to_async(model_class.objects.get_default)(
                tenant_id=tenant_id
            )
            if obj:
                return obj
            obj = await sync_to_async(
                lambda: model_class.objects.filter(tenant_id=tenant_id)
                .order_by("id")
                .first()
            )()
            if obj:
                return obj
            return await sync_to_async(
                lambda: model_class.objects.order_by("id").first()
            )()

        async def get_approved_event_status(
            tenant_id: int,
        ) -> models.Model | None:
            """The tenant's approved EventStatus (slug="approved"), or None.
            Same lookup `create_event_with_request` uses, but null-safe
            (filter().first() instead of .get()) so a tenant missing the row
            falls through to the default rather than hard-failing."""
            return await sync_to_async(
                lambda: EventStatus.objects.filter(
                    slug="approved", tenant_id=tenant_id
                )
                .order_by("id")
                .first()
            )()

        async def request_is_approved() -> bool:
            """True when the parent request's own RequestStatus slug means the
            activation is going ahead (approved/scheduled). Resolved via
            ``status_id`` through sync_to_async so this is safe even when the
            request was fetched without select_related('status') — never
            touches the lazy FK descriptor in the async context."""
            from .models import RequestStatus

            status_id = getattr(request, "status_id", None)
            if not status_id:
                return False
            slug = await sync_to_async(
                lambda: RequestStatus.objects.filter(id=status_id)
                .values_list("slug", flat=True)
                .first()
            )()
            return (slug or "") in self.APPROVED_REQUEST_STATUS_SLUGS

        event_type = await get_object_or_default(EventType, request.tenant_id, event_type)

        # No explicit status: when the parent request is already approved/
        # scheduled, prefer the approved EventStatus over the tenant default
        # so the Event's status agrees with the Request's status. Falls back
        # to the default resolution if the tenant has no "approved" row.
        if status is None and await request_is_approved():
            status = await get_approved_event_status(request.tenant_id)

        status = await get_object_or_default(EventStatus, request.tenant_id, status)

        if not status:
            raise ValueError("Event status not found.")

        return await sync_to_async(self.create)(
            request=request,
            tenant_id=request.tenant_id,
            created_by=created_by,
            event_type=event_type,
            status=status,
            name=request.name,
            start_time=request.start_time,
            end_time=request.end_time,
            address=request.address,
        )
