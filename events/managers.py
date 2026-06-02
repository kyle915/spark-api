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
        created_by: User | None = None,
        event_type: models.Model | None = None,
        status: models.Model | None = None,
    ) -> models.Model:
        """Create an event from a request.

        Tolerant of the "lighter" Request the CLIENT / public EXPRESS
        create-request form produces (no retailer / location / state /
        distributor / event_type, possibly no ``created_by``). It copies
        whatever scheduling + location data the request has and leaves the
        rest null, so an express-but-valid request (e.g. a client-created
        Vons / Liquid Death request) always materializes an Event instead of
        hard-failing — which is what made it invisible to Missing Recaps and
        the recap picker before.

        ``created_by`` resolution (``Event.created_by`` is NOT NULL, but a
        client/public express ``Request.created_by`` is legitimately null):
          * the explicit ``created_by`` arg, else
          * the request's own ``created_by`` / ``approved_by`` /
            ``rmm_asigned``, else
          * a superuser (then any user) so the Event still materializes.
        If NO user exists at all we raise — the Event genuinely can't be
        created — but that can't happen on a real tenant.

        ``status`` resolution (when not supplied):
          * If the parent request is already approved/scheduled (its own
            RequestStatus slug ∈ {approved, scheduled}), use the tenant's
            APPROVED EventStatus (slug="approved") — so an Event created off
            an approved request never silently inherits the tenant default
            "pending". This is the same approved status
            `create_event_with_request` sets explicitly. Resolved null-safely
            (filter().first()), so a tenant that never had an "approved" row
            falls through to the default below instead of hard-failing.
          * Otherwise fall back to the tenant's default EventStatus (then any
            tenant/global row) so an Event always materializes.
        Callers may still pass an explicit ``status`` to override this.

        ``end_time``: Missing Recaps keys on ``Event.end_time``. We copy the
        request's ``end_time`` when present; otherwise we fall back to its
        ``start_time`` so the Event still has a non-null end and isn't
        invisible there either.
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

        async def resolve_created_by(explicit: User | None) -> User:
            """Resolve a NON-NULL user for ``Event.created_by`` (NOT NULL).

            A client / public EXPRESS request legitimately has
            ``Request.created_by = NULL`` (no authenticated user filed it), so
            blindly copying it used to raise
            ``IntegrityError: null value in column "created_by_id"`` and leave
            the request eventless. Resolution order: the explicit arg → the
            request's own creator / approver / assigned RMM → any superuser →
            any user. Only when the DB has NO users at all do we raise (which
            can't happen on a real tenant)."""
            if explicit is not None:
                return explicit

            def _resolve() -> User | None:
                # FK ids are always available without select_related.
                for attr in ("created_by_id", "approved_by_id", "rmm_asigned_id"):
                    uid = getattr(request, attr, None)
                    if uid:
                        u = User.objects.filter(id=uid).first()
                        if u:
                            return u
                # Last resort so the Event still materializes: a superuser,
                # then any user. Mirrors create_tenant_and_roles' system-user
                # fallback.
                return (
                    User.objects.filter(is_superuser=True).order_by("id").first()
                    or User.objects.order_by("id").first()
                )

            resolved = await sync_to_async(_resolve)()
            if resolved is None:
                raise ValueError(
                    "Cannot create Event: no user available for created_by "
                    f"(request_id={getattr(request, 'id', None)})."
                )
            return resolved

        def derive_end_time():
            """Missing Recaps keys on ``Event.end_time``. Prefer the request's
            own end_time; if it's absent (an express request that only carried
            a start_time) fall back to the start_time so the event still has a
            non-null end and isn't invisible on Missing Recaps. Returns None
            only when there's no start_time either (nothing to derive from)."""
            end = getattr(request, "end_time", None)
            if end is not None:
                return end
            return getattr(request, "start_time", None)

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

        # Resolve a non-null Event.created_by FIRST. Event.created_by is NOT
        # NULL, but an express Request.created_by is legitimately null — this
        # is the fix for the IntegrityError that left express requests
        # eventless (and made the bulk repair fail on request #1051).
        created_by = await resolve_created_by(created_by)

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

        # Copy the location/retailer FKs straight across by id when the
        # request carries them (an express request omits all of these, so they
        # stay null — all three are nullable on Event). Using *_id avoids any
        # lazy FK access in this async context.
        return await sync_to_async(self.create)(
            request=request,
            tenant_id=request.tenant_id,
            created_by=created_by,
            event_type=event_type,
            status=status,
            name=request.name,
            start_time=request.start_time,
            end_time=derive_end_time(),
            # Event.address is NOT NULL; an express request always has a
            # (possibly empty) address string, but coalesce to "" so a request
            # with a NULL address (legacy/edge) still materializes instead of
            # tripping the not-null constraint.
            address=getattr(request, "address", None) or "",
            retailer_id=getattr(request, "retailer_id", None),
            distributor_id=getattr(request, "distributor_id", None),
            location_id=getattr(request, "location_id", None),
            state_id=getattr(request, "state_id", None),
            coordinates=getattr(request, "coordinates", None) or None,
            timezone_id=getattr(request, "timezone_id", None),
            rmm_asigned_id=getattr(request, "rmm_asigned_id", None),
            date=getattr(request, "date", None),
        )
