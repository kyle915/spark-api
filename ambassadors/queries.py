import logging
import asyncio
from typing import Annotated, List
import strawberry
from enum import Enum
from asgiref.sync import sync_to_async
from graphql import GraphQLError
from django.db.models import Exists, Model, OuterRef, QuerySet

from ambassadors import types
from ambassadors import models
from ambassadors import inputs
from jobs import models as job_models
from utils.graphql.permissions import StrictIsAuthenticated, IsClientOrSparkAdmin
from events import models as event_models
from events import types as event_types
from utils.graphql.mixins import SparkGraphQLMixin, resolve_id_to_int
from utils.graphql.queries import BaseQueriesService
from utils.graphql.inputs import SparkGraphQLInput
from utils.graphql.relay import (
    CountableConnection,
    connection_from_queryset_async,
)


@strawberry.enum
class AmbassadorEventStatus(str, Enum):
    APPROVED = "approved"
    DECLINED = "declined"
    CANCELED = "canceled"


def _resolve_filter_id(value: strawberry.ID | None, label: str) -> int | None:
    """Resolve relay/global IDs used in filters to database IDs."""
    if value in (None, ""):
        return None
    try:
        return resolve_id_to_int(value)
    except (TypeError, ValueError, GraphQLError) as exc:
        raise GraphQLError(f"Invalid {label} ID.") from exc


def _resolve_filter_id_list(values: list[strawberry.ID], label: str) -> list[int]:
    """Resolve a list of relay/global IDs used in filters to database IDs."""
    try:
        return [resolve_id_to_int(value) for value in values]
    except (TypeError, ValueError, GraphQLError) as exc:
        raise GraphQLError(f"Invalid {label} ID.") from exc


@strawberry.input
class AmbassadorEventsFiltersInput:
    """Filters for ambassador-scoped events."""

    ambassador_uuid: strawberry.ID | None = None
    event_id: strawberry.ID | None = None
    types: list[strawberry.ID] | None = None
    statuses: list[AmbassadorEventStatus] | None = None
    start_date: str | None = None
    end_date: str | None = None


class BaseAmbassadorQueriesService(SparkGraphQLMixin):
    """Service for ambassador queries."""

    ordering: tuple[str, ...] = ("-created_at",)

    def get_model(self) -> Model:
        """Get the model for the service."""
        raise NotImplementedError("Subclasses must implement this method.")

    def get_queryset(self) -> QuerySet:
        """Get the queryset for the service."""
        return self.get_model().objects.all()

    def get_filtered_queryset(self, q: str | None = None) -> QuerySet:
        """Get the filtered queryset for the service."""
        queryset = self.get_queryset()
        if q:
            queryset = queryset.filter(name__icontains=q)
        return queryset

    def get_ordered_queryset(
        self,
        q: str | None = None,
        ordering: tuple[str, ...] | None = None,
    ) -> QuerySet:
        """Return the filtered queryset with ordering applied."""
        queryset = self.get_filtered_queryset(q)
        ordering = ordering or self.ordering
        if ordering:
            queryset = queryset.order_by(*ordering)
        return queryset

    async def get_connection(
        self,
        *,
        q: str | None = None,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        default_limit: int = 10,
        max_limit: int = 50,
        ordering: tuple[str, ...] | None = None,
        queryset: QuerySet | None = None,
    ) -> CountableConnection[Model]:
        """Return a Relay compliant connection for the queryset."""
        if queryset is None:
            queryset = self.get_ordered_queryset(q, ordering)
        try:
            return await connection_from_queryset_async(
                queryset,
                first=first,
                after=after,
                last=last,
                before=before,
                default_limit=default_limit,
                max_limit=max_limit,
            )
        except ValueError as exc:
            raise GraphQLError(str(exc)) from exc

    async def get_record(self, id: strawberry.ID) -> Model | None:
        """Get a single record."""
        try:
            return await sync_to_async(self.get_model().objects.get)(id=id)
        except self.get_model().DoesNotExist:
            raise GraphQLError("Record not found.")

    async def get_record_by_uuid(self, uuid: str) -> Model | None:
        """Get a single record by UUID."""
        try:
            return await sync_to_async(self.get_model().objects.get)(uuid=uuid)
        except self.get_model().DoesNotExist:
            raise GraphQLError("Record not found.")


class AmbassadorsTenantQueriesService(BaseQueriesService):
    """Base service with tenant resolution adjusted for ambassador role."""

    async def resolve_query_tenant_id(
        self,
        info: strawberry.Info,
        *,
        filters: SparkGraphQLInput | None = None,
    ) -> int | None:
        """Allow spark-admin/ambassador to query any tenant; clients stay restricted."""
        user = await self.get_user(info)
        filters_tenant_id = getattr(filters, "tenant_id", None) if filters else None
        role_slug = self.get_role_slug(user)
        resolved_tenant_id: int | None = None

        if filters_tenant_id is not None:
            try:
                resolved_tenant_id = resolve_id_to_int(filters_tenant_id)
            except (TypeError, ValueError, GraphQLError) as exc:
                raise GraphQLError("Invalid tenant ID.") from exc

        if role_slug in {"spark-admin", "ambassador"}:
            if filters_tenant_id is None:
                return None
            tenant = await self._get_tenant_without_membership(
                tenant_id=resolved_tenant_id
            )
            return tenant.id

        try:
            tenant = await self.get_user_tenant(
                info,
                tenant_id=resolved_tenant_id,
                user=user,
            )
            return tenant.id
        except GraphQLError as exc:
            membership_error = "not a member of this tenant" in str(exc).lower()
            if membership_error and role_slug != "client":
                raise GraphQLError("Tenant access denied.") from exc
            raise


class FileTypeQueriesService(BaseAmbassadorQueriesService):
    """Service for file type queries."""

    def get_model(self) -> type[models.FileType]:
        """Get the model for the service."""
        return models.FileType


class AmbassadorEventQueriesService(BaseAmbassadorQueriesService):
    """Service for ambassador event queries."""

    def get_model(self) -> type[models.AmbassadorEvent]:
        """Get the model for the service."""
        return models.AmbassadorEvent

    def get_ambassador_queryset(self, user, filter_by_user: bool = True) -> QuerySet:
        """Return ambassador events, optionally filtered by user."""
        queryset = self.get_model().objects.select_related(
            "ambassador__user",
            "event__request",
            "event__status",
            "event__event_type",
            "event__timezone",
        )

        if filter_by_user:
            queryset = queryset.filter(ambassador__user=user)

        return queryset.distinct()


@strawberry.type
class FileTypeQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def file_types(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.FileType]:
        """Get all file types using Relay pagination."""
        service = FileTypeQueriesService()
        user = await service.get_user(info)

        queryset = service.get_ordered_queryset(q=q)

        return await service.get_connection(
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
            queryset=queryset,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def file_type(
        self, info: strawberry.Info, uuid: strawberry.ID
    ) -> types.FileType | None:
        """Get a single file type by UUID."""
        try:
            service = FileTypeQueriesService()
            user = await service.get_user(info)
            file_type = await service.get_record_by_uuid(str(uuid))
            return file_type
        except GraphQLError:
            return None


@strawberry.type
class AmbassadorEventQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def my_upcoming_shifts(
        self,
        info: strawberry.Info,
        within_days: int = 14,
    ) -> List[types.ShiftOfferDetails]:
        """Accepted (is_approved=True) AmbassadorEvent rows for the
        current BA whose event date is in the next N days (default 14).

        Powers the "Upcoming" section on the spark-mobile Shifts tab.
        Sorted by event start_time ascending (next-up first). Returns
        empty for non-ambassador users; cross-tenant access blocked
        via the Ambassador→user relationship.
        """
        from datetime import timedelta
        from django.utils import timezone

        user = info.context.request.user
        if not getattr(user, "is_authenticated", False):
            return []

        from ambassadors import models as a_models
        from ambassadors import types as a_types

        cutoff = timezone.now() + timedelta(days=max(1, within_days))

        def _fetch() -> List:
            try:
                ambassador = a_models.Ambassador.objects.get(user=user)
            except a_models.Ambassador.DoesNotExist:
                return []
            qs = (
                a_models.AmbassadorEvent.objects.select_related(
                    "event",
                    "event__retailer",
                    "event__state",
                )
                .filter(
                    ambassador=ambassador,
                    is_approved=True,
                    event__start_time__gte=timezone.now(),
                    event__start_time__lte=cutoff,
                )
                .order_by("event__start_time")
            )
            out: List = []
            for ae in qs:
                ev = ae.event
                venue = (
                    getattr(ev, "name", None)
                    or getattr(getattr(ev, "retailer", None), "name", None)
                )
                state_code = getattr(getattr(ev, "state", None), "code", None)
                out.append(
                    a_types.ShiftOfferDetails(
                        ambassador_event_uuid=strawberry.ID(str(ae.uuid)),
                        event_uuid=strawberry.ID(str(ev.uuid)),
                        event_name=venue or "(shift)",
                        venue=venue,
                        address=getattr(ev, "address", None),
                        date=ev.date.isoformat() if getattr(ev, "date", None) else None,
                        start_time=(
                            ev.start_time.isoformat()
                            if getattr(ev, "start_time", None)
                            else None
                        ),
                        end_time=(
                            ev.end_time.isoformat()
                            if getattr(ev, "end_time", None)
                            else None
                        ),
                        state_code=state_code,
                        is_approved=True,
                    )
                )
            return out

        return await sync_to_async(_fetch, thread_sensitive=True)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def shift_offer(
        self,
        info: strawberry.Info,
        ambassador_event_uuid: strawberry.ID,
    ) -> types.ShiftOfferDetails | None:
        """Fetch a single shift offer (AmbassadorEvent invitation) for
        the current BA. Powers the mobile ShiftOfferScreen that deep-
        links from the "New shift offered" push notification."""
        from .services import ShiftOfferService

        return await ShiftOfferService.get_offer(
            str(ambassador_event_uuid), info
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def my_earnings_stats(
        self,
        info: strawberry.Info,
        within_days: int = 30,
    ) -> types.MyEarningsStats:
        """Honest BA-facing earnings preview: completed shift count + an
        hour estimate over the last `withinDays` days.

        Approximations (until payroll is wired):
        - "Completed" = AmbassadorEvent.is_approved=True AND event.date
          < now. Doesn't require an attendance row, so it works even
          where the BA forgot to clock out.
        - "Hours" = sum of (event.end_time - event.start_time) across
          those shifts. Treats start/end as same-day local; rolls over
          midnight as +24h. None when the window has zero shifts.
        """
        from datetime import datetime, timedelta, timezone as _tz

        service = AmbassadorEventQueriesService()
        user = await service.get_user(info)

        days = max(1, min(int(within_days), 365))
        now = datetime.now(_tz.utc)
        cutoff = now - timedelta(days=days)

        qs = (
            service.get_model()
            .objects.select_related("event")
            .filter(
                ambassador__user=user,
                is_approved=True,
                event__date__gte=cutoff,
                event__date__lte=now,
            )
        )
        rows = await sync_to_async(list)(qs)

        shifts_count = len(rows)
        if shifts_count == 0:
            return types.MyEarningsStats(
                shifts_count=0,
                hours_estimate=None,
                within_days=days,
            )

        total_seconds = 0.0
        for ae in rows:
            ev = ae.event
            start = getattr(ev, "start_time", None)
            end = getattr(ev, "end_time", None)
            if not start or not end:
                continue
            # start/end are TimeField — combine with a sentinel date
            # to compute the delta. Roll over midnight by adding 24h.
            s_secs = (start.hour * 3600) + (start.minute * 60) + start.second
            e_secs = (end.hour * 3600) + (end.minute * 60) + end.second
            delta = e_secs - s_secs
            if delta < 0:
                delta += 24 * 3600
            total_seconds += float(delta)

        hours = total_seconds / 3600.0
        return types.MyEarningsStats(
            shifts_count=shifts_count,
            hours_estimate=round(hours, 2),
            within_days=days,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def ambassador_events(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: AmbassadorEventsFiltersInput | None = None,
    ) -> CountableConnection[types.AmbassadorEventType]:
        """Return ambassador events with ambassador and user nested.

        If user role is 'ambassador', only returns events for the logged ambassador.
        Otherwise, returns all ambassador events (for admins, clients, etc).
        """
        service = AmbassadorEventQueriesService()
        user = await service.get_user(info)

        # Check if user role is ambassador
        role_slug = service.get_role_slug(user)
        filter_by_user = role_slug == "ambassador"

        queryset = service.get_ambassador_queryset(user, filter_by_user=filter_by_user)
        if q:
            queryset = queryset.filter(event__name__icontains=q)

        if filters:
            if filters.ambassador_uuid:
                queryset = queryset.filter(ambassador__uuid=filters.ambassador_uuid)
            if filters.event_id:
                event_id = _resolve_filter_id(filters.event_id, "event")
                queryset = queryset.filter(event_id=event_id)
            if filters.types:
                type_ids = _resolve_filter_id_list(filters.types, "event type")
                queryset = queryset.filter(event__event_type_id__in=type_ids)
            if filters.statuses:
                status_slugs = [status.value for status in filters.statuses]
                queryset = queryset.filter(event__status__slug__in=status_slugs)
            if filters.start_date:
                queryset = queryset.filter(event__request__date__gte=filters.start_date)
            if filters.end_date:
                queryset = queryset.filter(event__request__date__lte=filters.end_date)

        queryset = queryset.order_by(*service.ordering)

        return await service.get_connection(
            queryset=queryset,
            first=first,
            after=after,
            last=last,
            before=before,
        )


@strawberry.type
class AmbassadorManagementQueries:
    """Queries for managing ambassadors and invitations (client/spark-admin only)."""

    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def sent_invitations(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        filters: inputs.AmbassadorInvitationFiltersInput | None = None,
    ) -> CountableConnection[types.AmbassadorInvitationType]:
        """Get sent invitations for a tenant (client/spark-admin only)."""
        from .services import AmbassadorInvitationQueriesService

        service = AmbassadorInvitationQueriesService()
        return await service.get_sent_invitations(
            info=info,
            first=first,
            after=after,
            last=last,
            before=before,
            filters=filters,
        )

    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def available_ambassadors(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        filters: inputs.AmbassadorFiltersInput | None = None,
    ) -> CountableConnection[types.Ambassador]:
        """Get available ambassadors for a tenant (client/spark-admin only)."""
        from .services import AmbassadorQueriesService

        service = AmbassadorQueriesService()
        return await service.get_available_ambassadors(
            info=info,
            first=first,
            after=after,
            last=last,
            before=before,
            filters=filters,
        )

    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def pending_ambassadors(
        self,
        info: strawberry.Info,
        first: int | None = 50,
        after: str | None = None,
    ) -> CountableConnection[types.Ambassador]:
        """Ambassadors waiting for admin approval — newest first.

        Pulls every Ambassador with is_active=False whose user is
        also active (i.e. signed up but not yet approved). Admin
        front-end uses this to render the Pending queue on /people.
        """
        from utils.graphql.relay import connection_from_queryset_async
        from ambassadors.models import Ambassador as AmbassadorModel

        qs = (
            AmbassadorModel.objects.filter(
                is_active=False,
                user__is_active=True,
            )
            .select_related("user")
            .order_by("-created_at")
        )
        return await connection_from_queryset_async(
            qs, first=first, after=after, last=None, before=None
        )

    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def invited_groups_by_job(
        self,
        info: strawberry.Info,
        filters: inputs.AmbassadorGroupFiltersInput | None = None,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
    ) -> CountableConnection[types.AmbassadorGroup]:
        """Get groups that include ambassadors invited to a given job."""
        from .services import AmbassadorInvitationQueriesService

        service = AmbassadorInvitationQueriesService()
        return await service.get_invited_groups_by_job(
            info=info,
            filters=filters,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def active_ambassadors(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: inputs.ActiveAmbassadorFiltersInput | None = None,
    ) -> CountableConnection[types.Ambassador]:
        """Get all active ambassadors (client/spark-admin only)."""
        from .services import AmbassadorQueriesService

        service = AmbassadorQueriesService()
        return await service.get_active_ambassadors(
            info=info,
            first=first,
            after=after,
            last=last,
            before=before,
            q=q,
            filters=filters,
        )

    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def ambassadors(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: inputs.AmbassadorFiltersInput | None = None,
    ) -> CountableConnection[types.Ambassador]:
        """List ambassadors with filters for status, rating, name, email, address and about_me."""
        from .services import AmbassadorQueriesService

        service = AmbassadorQueriesService()
        return await service.get_ambassadors(
            info=info,
            first=first,
            after=after,
            last=last,
            before=before,
            q=q,
            filters=filters,
        )

    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def ambassador(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.Ambassador | None:
        """Get a single ambassador by id or uuid (client/spark-admin only)."""
        from .services import AmbassadorQueriesService

        if not id and not uuid:
            raise GraphQLError("Either id or uuid must be provided")

        service = AmbassadorQueriesService()

        try:
            if id:
                ambassador = await sync_to_async(
                    models.Ambassador.objects.select_related(
                        "user", "location", "location__state"
                    ).get
                )(id=id)
            else:
                ambassador = await sync_to_async(
                    models.Ambassador.objects.select_related(
                        "user", "location", "location__state"
                    ).get
                )(uuid=uuid)
            return ambassador
        except models.Ambassador.DoesNotExist:
            return None


@strawberry.type
class AmbassadorProfileQueries:
    """Aggregate query for full ambassador profile."""

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def ambassador_profile(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
        user_id: strawberry.ID | None = None,
    ) -> types.AmbassadorProfile | None:
        """Return ambassador profile with related data in a single query."""
        if id is None and uuid is None and user_id is None:
            raise GraphQLError("Either id, uuid, or user_id must be provided")

        if id is not None:
            filters = {"id": id}
        elif uuid is not None:
            filters = {"uuid": uuid}
        else:
            try:
                resolved_user_id = resolve_id_to_int(user_id)
            except (TypeError, ValueError, GraphQLError) as exc:
                raise GraphQLError("Invalid user ID.") from exc
            filters = {"user_id": resolved_user_id}

        try:
            ambassador = await models.Ambassador.objects.select_related(
                "user", "location", "location__state"
            ).aget(
                **filters
            )
        except models.Ambassador.DoesNotExist:
            return None

        ambassador_id = ambassador.id

        async def fetch_reviews():
            queryset = models.AmbassadorReview.objects.filter(
                ambassador_id=ambassador_id
            )
            return await sync_to_async(list)(queryset)

        async def fetch_files():
            queryset = models.AmbassadorFile.objects.select_related("file_type").filter(
                ambassador_id=ambassador_id
            )
            return await sync_to_async(list)(queryset)

        async def fetch_traits():
            queryset = models.AmbassadorTrait.objects.filter(
                ambassador_id=ambassador_id
            )
            return await sync_to_async(list)(queryset)

        async def fetch_skills():
            queryset = models.AmbassadorSkill.objects.select_related("skill").filter(
                ambassador_id=ambassador_id
            )
            return await sync_to_async(list)(queryset)

        async def fetch_notes():
            queryset = models.AmbassadorNote.objects.filter(ambassador_id=ambassador_id)
            return await sync_to_async(list)(queryset)

        async def fetch_work_history():
            queryset = models.AmbassadorWorkHistory.objects.filter(
                ambassador_id=ambassador_id
            )
            return await sync_to_async(list)(queryset)

        (
            reviews,
            files,
            traits,
            skills,
            notes,
            work_history,
        ) = await asyncio.gather(
            fetch_reviews(),
            fetch_files(),
            fetch_traits(),
            fetch_skills(),
            fetch_notes(),
            fetch_work_history(),
        )

        return types.AmbassadorProfile(
            ambassador=ambassador,
            reviews=reviews,
            files=files,
            traits=traits,
            skills=skills,
            notes=notes,
            work_history=work_history,
        )


@strawberry.type
class AmbassadorReviewQueries:
    """Queries for ambassador reviews."""

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def ambassador_reviews(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        filters: inputs.AmbassadorReviewFiltersInput | None = None,
    ) -> CountableConnection[types.AmbassadorReviewType]:
        """Get ambassador reviews with filters (authenticated users only)."""
        from .services import AmbassadorReviewQueriesService

        service = AmbassadorReviewQueriesService()
        return await service.get_ambassador_reviews(
            info=info,
            first=first,
            after=after,
            last=last,
            before=before,
            filters=filters,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def ambassador_review(
        self,
        info: strawberry.Info,
        review_id: strawberry.ID,
    ) -> types.AmbassadorReviewType | None:
        """Get a single ambassador review by ID (authenticated users only)."""
        from .models import AmbassadorReview

        try:

            @sync_to_async
            def get_review():
                return AmbassadorReview.objects.select_related(
                    "ambassador", "client", "tenant"
                ).get(pk=int(review_id))

            return await get_review()
        except (AmbassadorReview.DoesNotExist, ValueError, TypeError):
            return None


@strawberry.type
class AmbassadorNoteQueries:
    """Queries for ambassador notes."""

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def ambassador_notes(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        filters: inputs.AmbassadorNoteFiltersInput | None = None,
    ) -> CountableConnection[types.AmbassadorNoteType]:
        """Get ambassador notes with filters (authenticated users only)."""
        from .services import AmbassadorNoteQueriesService

        service = AmbassadorNoteQueriesService()
        return await service.get_ambassador_notes(
            info=info,
            first=first,
            after=after,
            last=last,
            before=before,
            filters=filters,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def ambassador_note(
        self,
        info: strawberry.Info,
        note_id: strawberry.ID,
    ) -> types.AmbassadorNoteType | None:
        """Get a single ambassador note by ID (authenticated users only)."""
        from .models import AmbassadorNote

        try:

            @sync_to_async
            def get_note():
                return AmbassadorNote.objects.select_related(
                    "ambassador", "tenant", "created_by", "updated_by"
                ).get(pk=int(note_id))

            return await get_note()
        except (AmbassadorNote.DoesNotExist, ValueError, TypeError):
            return None


@strawberry.type
class SkillQueries:
    """Queries for skills."""

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def skills(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        filters: inputs.SkillFiltersInput | None = None,
    ) -> CountableConnection[types.SkillType]:
        """Get skills with filters (authenticated users only)."""
        from .services import SkillQueriesService

        service = SkillQueriesService()
        return await service.get_skills(
            info=info,
            first=first,
            after=after,
            last=last,
            before=before,
            filters=filters,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def skill(
        self,
        info: strawberry.Info,
        skill_id: strawberry.ID | None = None,
        skill_uuid: strawberry.ID | None = None,
    ) -> types.SkillType | None:
        """Get a single skill by ID or UUID (authenticated users only)."""
        from .models import Skill

        if skill_id is None and skill_uuid is None:
            return None

        try:
            if skill_uuid is not None:

                @sync_to_async
                def get_by_uuid():
                    return Skill.objects.get(uuid=str(skill_uuid))

                skill = await get_by_uuid()
            else:
                skill = await Skill.objects._by_id(skill_id)
            return skill
        except (Skill.DoesNotExist, ValueError, TypeError):
            return None


@strawberry.type
class AmbassadorSkillQueries:
    """Queries for ambassador skills."""

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def ambassador_skills(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        filters: inputs.AmbassadorSkillFiltersInput | None = None,
    ) -> CountableConnection[types.AmbassadorSkillType]:
        """Get ambassador skills with filters (authenticated users only)."""
        from .services import AmbassadorSkillQueriesService

        service = AmbassadorSkillQueriesService()
        return await service.get_ambassador_skills(
            info=info,
            first=first,
            after=after,
            last=last,
            before=before,
            filters=filters,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def ambassador_skill(
        self,
        info: strawberry.Info,
        ambassador_skill_id: strawberry.ID,
    ) -> types.AmbassadorSkillType | None:
        """Get a single ambassador skill by ID (authenticated users only)."""
        from .models import AmbassadorSkill

        try:
            ambassador_skill = await AmbassadorSkill.objects._by_id(ambassador_skill_id)
            return ambassador_skill
        except (AmbassadorSkill.DoesNotExist, ValueError, TypeError):
            return None


class AttendanceTypeQueriesService(BaseQueriesService):
    """Service for attendance type queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.AttendanceType


class AttendanceStatusQueriesService(AmbassadorsTenantQueriesService):
    """Service for attendance status queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.AttendanceStatus


class SourceQueriesService(BaseQueriesService):
    """Service for source queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Source


class GroupTypeQueriesService(BaseQueriesService):
    """Service for group type queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.GroupType


class AmbassadorGroupQueriesService(BaseQueriesService):
    """Service for ambassador group queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.AmbassadorGroup

    def apply_filters(
        self,
        queryset: QuerySet,
        filters: inputs.AmbassadorGroupFiltersInput | None,
    ) -> QuerySet:
        """Apply ambassador group filters to queryset."""
        if not filters:
            return queryset

        job_id = getattr(filters, "job_id", None)
        if job_id:
            try:
                job_id = resolve_id_to_int(job_id)
                queryset = queryset.filter(job_links__job_id=job_id).distinct()
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid job ID.")
        job_uuid = getattr(filters, "job_uuid", None)
        if job_uuid:
            queryset = queryset.filter(job_links__job__uuid=job_uuid).distinct()

        return queryset


class AttendanceQueriesService(BaseQueriesService):
    """Service for attendance queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Attendance

    def get_filtered_queryset(
        self, tenant_id: int | None = None, q: str | None = None
    ) -> QuerySet:
        """
        Override default filtering to avoid name__icontains lookups.

        Attendance has no name field, so we just return the base queryset.
        """
        return self.get_queryset()

    def apply_filters(
        self,
        queryset: QuerySet,
        filters: inputs.AttendanceFiltersInput | None,
    ) -> QuerySet:
        """Apply attendance filters to queryset."""
        if not filters:
            return queryset

        if filters.tenant_id:
            try:
                tenant_id = resolve_id_to_int(filters.tenant_id)
                queryset = queryset.filter(tenant_id=tenant_id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid tenant ID.")
        if filters.job_id:
            try:
                job_id = resolve_id_to_int(filters.job_id)
                queryset = queryset.filter(job_id=job_id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid job ID.")
        if filters.ambassador_job_id:
            try:
                ambassador_job_id = resolve_id_to_int(filters.ambassador_job_id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid ambassador job ID.")
            ambassador_job_match = job_models.AmbassadorJob.objects.filter(
                id=ambassador_job_id,
                ambassador_id=OuterRef("ambassador_id"),
                job_id=OuterRef("job_id"),
            )
            queryset = queryset.annotate(
                ambassador_job_match=Exists(ambassador_job_match)
            ).filter(ambassador_job_match=True)
        if filters.event_id:
            try:
                event_id = resolve_id_to_int(filters.event_id)
                queryset = queryset.filter(event_id=event_id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid event ID.")
        if filters.attendance_status_id:
            try:
                attendance_status_id = resolve_id_to_int(filters.attendance_status_id)
                queryset = queryset.filter(attendance_status_id=attendance_status_id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid attendance status ID.")
        if filters.source_id:
            try:
                source_id = resolve_id_to_int(filters.source_id)
                queryset = queryset.filter(source_id=source_id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid source ID.")
        if filters.attendace_type_id:
            try:
                attendace_type_id = resolve_id_to_int(filters.attendace_type_id)
                queryset = queryset.filter(attendace_type_id=attendace_type_id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid attendance type ID.")
        return queryset


@strawberry.type
class AttendanceQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def attendance_types(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.AttendanceType]:
        service = AttendanceTypeQueriesService()
        await service.get_user(info)
        return await service.get_connection(
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def attendance_type(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> types.AttendanceType | None:
        try:
            service = AttendanceTypeQueriesService()
            await service.get_user(info)
            return await service.get_record(id)
        except GraphQLError:
            return None

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def attendance_statuses(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.AttendanceStatus]:
        service = AttendanceStatusQueriesService()
        tenant_id = await service.resolve_query_tenant_id(info)
        return await service.get_connection(
            tenant_id=tenant_id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def attendance_status(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> types.AttendanceStatus | None:
        try:
            service = AttendanceStatusQueriesService()
            tenant_id = await service.resolve_query_tenant_id(info)
            return await service.get_record(id, tenant_id)
        except GraphQLError:
            return None

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def sources(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.Source]:
        service = SourceQueriesService()
        await service.get_user(info)
        return await service.get_connection(
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def source(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> types.Source | None:
        try:
            service = SourceQueriesService()
            await service.get_user(info)
            return await service.get_record(id)
        except GraphQLError:
            return None

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def attendances(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        filters: inputs.AttendanceFiltersInput | None = None,
    ) -> CountableConnection[types.Attendance]:
        service = AttendanceQueriesService()
        await service.get_user(info)
        queryset = service.get_queryset()
        queryset = service.apply_filters(queryset, filters)
        queryset = queryset.order_by(*service.ordering)

        return await service.get_connection(
            queryset=queryset,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def attendance(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> types.Attendance | None:
        try:
            service = AttendanceQueriesService()
            await service.get_user(info)
            return await service.get_record(id)
        except GraphQLError:
            return None


@strawberry.type
class GroupTypeQueries:
    """Queries for group types."""

    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def group_types(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        filters: inputs.GroupTypeFiltersInput | None = None,
    ) -> CountableConnection[types.GroupType]:
        """Get group types with filters (authenticated users only)."""
        service = GroupTypeQueriesService()
        await service.get_user(info)

        q = filters.search if filters else None
        queryset = service.get_ordered_queryset(q=q)

        return await service.get_connection(
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
            queryset=queryset,
        )

    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def group_type(
        self,
        info: strawberry.Info,
        group_type_id: strawberry.ID,
    ) -> types.GroupType | None:
        """Get a single group type by ID (authenticated users only)."""
        try:
            service = GroupTypeQueriesService()
            await service.get_user(info)
            return await service.get_record(group_type_id)
        except GraphQLError:
            return None


@strawberry.type
class AmbassadorGroupQueries:
    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def ambassador_groups(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        filters: inputs.AmbassadorGroupFiltersInput | None = None,
    ) -> CountableConnection[types.AmbassadorGroup]:
        """Get ambassador groups with filters (client/spark-admin only)."""
        service = AmbassadorGroupQueriesService()
        tenant_id = await service.resolve_query_tenant_id(info, filters=filters)
        await service.get_user(info)

        q = filters.search if filters else None
        queryset = service.get_ordered_queryset(tenant_id=tenant_id, q=q)
        queryset = service.apply_filters(queryset, filters)

        return await service.get_connection(
            tenant_id=tenant_id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
            queryset=queryset,
        )

    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def ambassador_group(
        self,
        info: strawberry.Info,
        group_id: strawberry.ID,
    ) -> types.AmbassadorGroup | None:
        """Get a single ambassador group by ID (client/spark-admin only)."""
        try:
            service = AmbassadorGroupQueriesService()
            tenant_id = await service.resolve_query_tenant_id(info)
            await service.get_user(info)
            return await service.get_record(id=group_id, tenant_id=tenant_id)
        except GraphQLError:
            return None


@strawberry.type
class AttendanceMobileQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def attendance_types(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.AttendanceType]:
        service = AttendanceTypeQueriesService()
        await service.get_user(info)
        return await service.get_connection(
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def attendance_type(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> types.AttendanceType | None:
        try:
            service = AttendanceTypeQueriesService()
            await service.get_user(info)
            return await service.get_record(id)
        except GraphQLError:
            return None

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def attendances_mobile(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        filters: inputs.AttendanceFiltersInput | None = None,
    ) -> CountableConnection[types.Attendance]:
        service = AttendanceQueriesService()
        user = await service.get_user(info)

        queryset = service.get_queryset().filter(ambassador__user=user)

        queryset = service.apply_filters(queryset, filters)
        queryset = queryset.order_by(*service.ordering)

        return await service.get_connection(
            queryset=queryset,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def attendance_mobile(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> types.Attendance | None:
        service = AttendanceQueriesService()
        user = await service.get_user(info)

        try:
            return await sync_to_async(service.get_model().objects.get)(
                id=id,
                ambassador__user=user,
            )
        except GraphQLError:
            return None
        except service.get_model().DoesNotExist:
            return None
