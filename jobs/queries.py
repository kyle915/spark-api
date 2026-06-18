import strawberry
from enum import Enum
from graphql import GraphQLError
from django.db.models import Prefetch
from asgiref.sync import sync_to_async

from utils.graphql.inputs import BaseTenantInput, SparkGraphQLInput
from utils.graphql.permissions import (
    StrictIsAuthenticated,
    _is_admin_access,
    resolve_request_user_access,
)
from utils.graphql.relay import CountableConnection
from utils.graphql.queries import BaseQueriesService
from utils.graphql.mixins import resolve_id_to_int, SparkGraphQLMixin
from jobs import models
from django.db.models import QuerySet
from django.db.models import Model
from django.db.models import BooleanField, Exists, F, OuterRef, Q, Value
from django.db.models.functions import ACos, Cos, Radians, Sin
from jobs import types
from jobs.inputs import JobFiltersInput, JobStatusFilter, RateTypeFiltersInput
from ambassadors import models as ambassador_models
from events.inputs import CoordinatesFilterInput, DistanceUnit


def _apply_job_date_filters(queryset: QuerySet, filters: JobFiltersInput) -> QuerySet:
    """Apply optional date range filters using job start_date."""
    if filters.start_date:
        queryset = queryset.filter(start_date__date__gte=filters.start_date)
    if filters.end_date:
        queryset = queryset.filter(start_date__date__lte=filters.end_date)
    return queryset


def _apply_ba_board_filters(queryset: QuerySet, filters) -> QuerySet:
    """Marketplace filters for the BA job board (my_available_jobs) and
    the new-gig digest matcher: state code, date range, and minimum
    hourly rate. All optional — a null/empty `filters` is a no-op.

    State matches `Job.event.state.code` case-insensitively. Min pay
    compares against `Job.hourly_rate`; jobs with a null rate are kept
    (we don't hide a gig just because pay isn't filled in yet)."""
    if filters is None:
        return queryset
    if getattr(filters, "start_date", None) or getattr(filters, "end_date", None):
        queryset = _apply_job_date_filters(queryset, filters)
    state_code = (getattr(filters, "state_code", None) or "").strip()
    if state_code:
        queryset = queryset.filter(event__state__code__iexact=state_code)
    min_rate = getattr(filters, "min_hourly_rate", None)
    if min_rate is not None:
        from decimal import Decimal
        from django.db.models import Q
        queryset = queryset.filter(
            Q(hourly_rate__gte=Decimal(str(min_rate)))
            | Q(hourly_rate__isnull=True)
        )
    return queryset


def _apply_available_job_filters(queryset: QuerySet, filters: JobFiltersInput) -> QuerySet:
    """Apply optional filters specific to available jobs."""
    if filters.location_id:
        try:
            location_id = resolve_id_to_int(filters.location_id)
        except (TypeError, ValueError, GraphQLError) as exc:
            raise GraphQLError("Invalid location ID.") from exc
        queryset = queryset.filter(
            Q(event__retailer__location_id=location_id)
            | Q(event__distributor__location_id=location_id)
            | Q(event__request__retailer__location_id=location_id)
            | Q(event__request__distributor__location_id=location_id)
        )

    if filters.coordinates:
        if len(filters.coordinates.coordinates) != 2:
            raise GraphQLError("Coordinates must contain latitude and longitude.")
        lat = filters.coordinates.coordinates[0]
        lon = filters.coordinates.coordinates[1]
        range_value = filters.coordinates.range
        if range_value < 0:
            raise GraphQLError("Range must be a non-negative value.")

        # Calculate distance in miles using event coordinates.
        distance_expr = 3959 * ACos(
            Cos(Radians(lat))
            * Cos(Radians(F("event__coordinates__0")))
            * Cos(Radians(F("event__coordinates__1")) - Radians(lon))
            + Sin(Radians(lat)) * Sin(Radians(F("event__coordinates__0")))
        )

        # Input range is treated as miles; if km is sent, convert to miles.
        range_in_miles = (
            range_value * 0.621371
            if filters.coordinates.unit == DistanceUnit.KILOMETERS
            else range_value
        )
        queryset = queryset.annotate(event_distance=distance_expr).filter(
            event_distance__lte=range_in_miles
        )

    return queryset


class JobsBaseQueriesService(BaseQueriesService):
    """Jobs-specific base service to adjust tenant error handling."""

    async def resolve_query_tenant_id(
        self,
        info: strawberry.Info,
        *,
        filters: SparkGraphQLInput | None = None,
    ) -> int | None:
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

        if role_slug == "client":
            tenant = await self.get_user_tenant(
                info,
                tenant_id=resolved_tenant_id,
                user=user,
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
            if membership_error:
                raise GraphQLError("Tenant access denied.") from exc
            raise


# Status Queries
class StatusQueriesService(JobsBaseQueriesService):
    """Service for status queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Status


@strawberry.type
class StatusQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def ambassador_job_statuses(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.Status]:
        """Get all statuses."""
        service = StatusQueriesService()
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
    async def ambassador_job_status(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.Status | None:
        """Get a single status."""
        try:
            service = StatusQueriesService()
            return await service.get_single_record(
                info, id=id, uuid=str(uuid) if uuid else None
            )
        except GraphQLError:
            return None


# CompanyFile Queries
class CompanyFileQueriesService(JobsBaseQueriesService):
    """Service for company file queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.CompanyFile


@strawberry.type
class CompanyFileQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def company_files(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.CompanyFile]:
        """Get all company files."""
        service = CompanyFileQueriesService()
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
    async def company_file(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.CompanyFile | None:
        """Get a single company file."""
        try:
            service = CompanyFileQueriesService()
            return await service.get_single_record(
                info, id=id, uuid=str(uuid) if uuid else None
            )
        except GraphQLError:
            return None


# Company Queries
class CompanyQueriesService(JobsBaseQueriesService):
    """Service for company queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Company

    def get_filtered_queryset(
        self, tenant_id: int | None = None, q: str | None = None
    ) -> QuerySet:
        """Filter companies; exclude entries without name to satisfy GraphQL non-null."""
        queryset = super().get_filtered_queryset(tenant_id, q)
        return queryset.exclude(name__isnull=True)


@strawberry.type
class CompanyQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def companies(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.Company]:
        """Get all companies."""
        service = CompanyQueriesService()
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
    async def company(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.Company | None:
        """Get a single company."""
        try:
            service = CompanyQueriesService()
            return await service.get_single_record(
                info, id=id, uuid=str(uuid) if uuid else None
            )
        except GraphQLError:
            return None


# CompanyReview Queries
class CompanyReviewQueriesService(JobsBaseQueriesService):
    """Service for company review queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.CompanyReview


@strawberry.type
class CompanyReviewQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def company_reviews(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.CompanyReview]:
        """Get all company reviews."""
        service = CompanyReviewQueriesService()
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
    async def company_review(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.CompanyReview | None:
        """Get a single company review."""
        try:
            service = CompanyReviewQueriesService()
            return await service.get_single_record(
                info, id=id, uuid=str(uuid) if uuid else None
            )
        except GraphQLError:
            return None


# PayTiming Queries
class PayTimingQueriesService(JobsBaseQueriesService):
    """Service for pay timing queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.PayTiming


@strawberry.type
class PayTimingQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def pay_timings(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.PayTiming]:
        """Get all pay timings."""
        service = PayTimingQueriesService()
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
    async def pay_timing(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.PayTiming | None:
        """Get a single pay timing."""
        try:
            service = PayTimingQueriesService()
            return await service.get_single_record(
                info, id=id, uuid=str(uuid) if uuid else None
            )
        except GraphQLError:
            return None


# ReviewScore Queries
class ReviewScoreQueriesService(JobsBaseQueriesService):
    """Service for review score queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.ReviewScore


@strawberry.type
class ReviewScoreQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def review_scores(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.ReviewScore]:
        """Get all review scores."""
        service = ReviewScoreQueriesService()
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
    async def review_score(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.ReviewScore | None:
        """Get a single review score."""
        try:
            service = ReviewScoreQueriesService()
            return await service.get_single_record(
                info, id=id, uuid=str(uuid) if uuid else None
            )
        except GraphQLError:
            return None


# JobTitle Queries
class JobTitleQueriesService(JobsBaseQueriesService):
    """Service for job title queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.JobTitle


@strawberry.type
class JobTitleQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def job_titles(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.JobTitle]:
        """Get all job titles."""
        service = JobTitleQueriesService()
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
    async def job_title(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.JobTitle | None:
        """Get a single job title."""
        try:
            service = JobTitleQueriesService()
            return await service.get_single_record(
                info, id=id, uuid=str(uuid) if uuid else None
            )
        except GraphQLError:
            return None


# RateType Queries
class RateTypeQueriesService(JobsBaseQueriesService):
    """Service for rate type queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.RateType


@strawberry.type
class RateTypeQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def rate_types(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: RateTypeFiltersInput | None = None,
    ) -> CountableConnection[types.RateType]:
        """Get all rate types."""
        service = RateTypeQueriesService()
        tenant_id = await service.resolve_query_tenant_id(info, filters=filters)
        return await service.get_connection(
            tenant_id=tenant_id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def rate_type(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.RateType | None:
        """Get a single rate type."""
        try:
            service = RateTypeQueriesService()
            return await service.get_single_record(
                info, id=id, uuid=str(uuid) if uuid else None
            )
        except GraphQLError:
            return None


# Rate Queries
class RateQueriesService(JobsBaseQueriesService):
    """Service for rate queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Rate


@strawberry.type
class RateQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def rates(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.Rate]:
        """Get all rates."""
        service = RateQueriesService()
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
    async def rate(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.Rate | None:
        """Get a single rate."""
        try:
            service = RateQueriesService()
            return await service.get_single_record(
                info, id=id, uuid=str(uuid) if uuid else None
            )
        except GraphQLError:
            return None


# Job Queries
class JobQueriesService(JobsBaseQueriesService):
    """Service for job queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Job

    async def get_record(
        self,
        id: strawberry.ID | None = None,
        tenant_id: strawberry.ID | None = None,
        uuid: str | None = None,
    ) -> Model | None:
        """Get a single record by id or uuid with prefetched relations."""
        if id is None and uuid is None:
            raise GraphQLError("Record identifier is required.")

        filters: dict[str, object] = {}
        if id is not None:
            try:
                filters["id"] = resolve_id_to_int(id)
            except (TypeError, ValueError, GraphQLError) as exc:
                raise GraphQLError("Invalid ID.") from exc
        if uuid is not None:
            filters["uuid"] = uuid
        if tenant_id is not None:
            filters["tenant_id"] = tenant_id

        queryset = self.get_queryset()
        try:
            return await sync_to_async(queryset.get)(**filters)
        except self.get_model().DoesNotExist:
            raise GraphQLError("Record not found.")
        except self.get_model().MultipleObjectsReturned:
            raise GraphQLError("Multiple records found for the given identifier.")

    def get_queryset(self) -> QuerySet:
        """Get jobs with related attendance and ambassadors prefetched."""
        return (
            self.get_model()
            .objects.select_related(
                "job_title",
                "other_title",
                "event",
                "event__timezone",
                "rate",
            )
            .prefetch_related(
                "job_requirements",
                Prefetch(
                    "attendance",
                    queryset=ambassador_models.Attendance.objects.select_related(
                        "ambassador",
                        "ambassador__user",
                    ),
                ),
                Prefetch(
                    "ambassador_jobs",
                    queryset=models.AmbassadorJob.objects.select_related(
                        "ambassador",
                        "ambassador__user",
                        "status",
                        "rate",
                    ),
                ),
            )
            # A job whose parent request was soft-deleted (deleted_at set) must
            # disappear everywhere this queryset feeds — the admin Jobs list AND
            # the BA job board (both go through here). Jobs on events with no
            # request (bulk / born-approved) keep showing: the nullable-FK join
            # leaves deleted_at NULL for them, so they aren't excluded.
            .exclude(event__request__deleted_at__isnull=False)
        )


@strawberry.type
class JobQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def jobs(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: JobFiltersInput | None = None,
    ) -> CountableConnection[types.Job]:
        """Get all jobs."""
        service = JobQueriesService()
        tenant_id = await service.resolve_query_tenant_id(info, filters=filters)
        queryset = service.get_ordered_queryset(
            tenant_id=tenant_id,
            q=q,
        )

        if filters:
            if filters.event_id:
                try:
                    event_id = resolve_id_to_int(filters.event_id)
                    queryset = queryset.filter(event_id=event_id)
                except (TypeError, ValueError, GraphQLError):
                    raise GraphQLError("Invalid event ID.")
            queryset = _apply_job_date_filters(queryset, filters)
            status_values = []
            if filters.statuses:
                status_values.extend([status.value for status in filters.statuses])
            if filters.status:
                status_values.append(filters.status.value)
            if status_values:
                status_filter_kwargs = {
                    "ambassador_jobs__status__slug__in": status_values
                }
                if tenant_id:
                    status_filter_kwargs["ambassador_jobs__status__tenant_id"] = (
                        tenant_id
                    )
                queryset = queryset.filter(**status_filter_kwargs).distinct()
            if filters.edited is not None:
                queryset = queryset.filter(updated_by__isnull=not filters.edited)

        return await service.get_connection(
            tenant_id=tenant_id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
            queryset=queryset,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def job(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.Job | None:
        """Get a single job."""
        try:
            service = JobQueriesService()
            user = await service.get_user(info)
            role_slug = service.get_role_slug(user)
            return await service.get_single_record(
                info,
                id=id,
                uuid=str(uuid) if uuid else None,
                enforce_tenant=role_slug != "ambassador",
            )
        except GraphQLError:
            return None


# JobFile Queries


class JobFileQueriesService(JobsBaseQueriesService):
    """Service for job file queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.JobFile


@strawberry.type
class JobFileQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def job_files(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.JobFile]:
        """Get all job files."""
        service = JobFileQueriesService()
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
    async def job_file(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.JobFile | None:
        """Get a single job file."""
        try:
            service = JobFileQueriesService()
            return await service.get_single_record(
                info, id=id, uuid=str(uuid) if uuid else None
            )
        except GraphQLError:
            return None


# JobRequirementType Queries
class JobRequirementTypeQueriesService(JobsBaseQueriesService):
    """Service for job requirement type queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.JobRequirementType


@strawberry.type
class JobRequirementTypeQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def job_requirement_types(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.JobRequirementType]:
        """Get all job requirement types."""
        service = JobRequirementTypeQueriesService()
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
    async def job_requirement_type(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.JobRequirementType | None:
        """Get a single job requirement type."""
        try:
            service = JobRequirementTypeQueriesService()
            return await service.get_single_record(
                info, id=id, uuid=str(uuid) if uuid else None
            )
        except GraphQLError:
            return None


# JobRequirement Queries
class JobRequirementQueriesService(JobsBaseQueriesService):
    """Service for job requirement queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.JobRequirement


@strawberry.type
class JobRequirementQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def job_requirements(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.JobRequirement]:
        """Get all job requirements."""
        service = JobRequirementQueriesService()
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
    async def job_requirement(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.JobRequirement | None:
        """Get a single job requirement."""
        try:
            service = JobRequirementQueriesService()
            return await service.get_single_record(
                info, id=id, uuid=str(uuid) if uuid else None
            )
        except GraphQLError:
            return None


# JobRequirementFile Queries
class JobRequirementFileQueriesService(JobsBaseQueriesService):
    """Service for job requirement file queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.JobRequirementFile


@strawberry.type
class JobRequirementFileQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def job_requirement_files(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.JobRequirementFile]:
        """Get all job requirement files."""
        service = JobRequirementFileQueriesService()
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
    async def job_requirement_file(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.JobRequirementFile | None:
        """Get a single job requirement file."""
        try:
            service = JobRequirementFileQueriesService()
            return await service.get_single_record(
                info, id=id, uuid=str(uuid) if uuid else None
            )
        except GraphQLError:
            return None


# AmbassadorJob Queries
class AmbassadorJobQueriesService(JobsBaseQueriesService):
    """Service for ambassador job queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.AmbassadorJob

    def get_queryset(self) -> QuerySet:
        """Get ambassador jobs with related event timezone loaded."""
        return self.get_model().objects.select_related(
            "ambassador",
            "ambassador__user",
            "job",
            "job__event",
            "job__event__timezone",
            "status",
            "rate",
            "tenant",
        )

    def get_filtered_queryset(
        self, tenant_id: int | None = None, q: str | None = None
    ) -> QuerySet:
        """Filter ambassador jobs by tenant and related searchable fields."""
        queryset = self.get_queryset()
        if tenant_id:
            queryset = queryset.filter(tenant_id=tenant_id)
        if q:
            queryset = queryset.filter(
                Q(job__name__icontains=q)
                | Q(job__code__icontains=q)
                | Q(ambassador__user__first_name__icontains=q)
                | Q(ambassador__user__last_name__icontains=q)
            )
        return queryset

    async def get_record(
        self,
        id: strawberry.ID | None = None,
        tenant_id: strawberry.ID | None = None,
        uuid: str | None = None,
    ) -> Model | None:
        """Get a single ambassador job with related event timezone loaded."""
        if id is None and uuid is None:
            raise GraphQLError("Record identifier is required.")

        filters: dict[str, object] = {}
        if id is not None:
            try:
                filters["id"] = resolve_id_to_int(id)
            except (TypeError, ValueError, GraphQLError) as exc:
                raise GraphQLError("Invalid ID.") from exc
        if uuid is not None:
            filters["uuid"] = uuid
        if tenant_id is not None:
            filters["tenant_id"] = tenant_id

        try:
            return await sync_to_async(self.get_queryset().get)(**filters)
        except self.get_model().DoesNotExist:
            raise GraphQLError("Record not found.")
        except self.get_model().MultipleObjectsReturned:
            raise GraphQLError("Multiple records found for the given identifier.")


@strawberry.enum
class AmbassadorJobStatusFilter(str, Enum):
    APPROVED = "approved"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    PENDING = "pending"
    INVITED = "invited"


@strawberry.input
class AmbassadorJobFiltersInput(BaseTenantInput):
    status: AmbassadorJobStatusFilter | None = None
    statuses: list[AmbassadorJobStatusFilter] | None = None
    status_id: strawberry.ID | None = None
    status_slug: str | None = None
    accepted_terms: bool | None = None
    time_blocks_15m: int | None = None
    job_id: strawberry.ID | None = None
    start_date: str | None = None
    end_date: str | None = None
    coordinates: CoordinatesFilterInput | None = None


@strawberry.type
class AmbassadorJobQueries:
    @staticmethod
    def _apply_ambassador_job_filters(
        queryset: QuerySet,
        filters: AmbassadorJobFiltersInput,
        tenant_id: int | None = None,
    ) -> QuerySet:
        """Apply ambassador-job filters for mobile and web queries."""
        if filters.accepted_terms is not None:
            queryset = queryset.filter(accepted_terms=filters.accepted_terms)
        if filters.time_blocks_15m is not None:
            queryset = queryset.filter(time_blocks_15m=filters.time_blocks_15m)
        if filters.job_id:
            try:
                job_id = resolve_id_to_int(filters.job_id)
                queryset = queryset.filter(job_id=job_id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid job ID.")
        if filters.start_date:
            queryset = queryset.filter(job__start_date__date__gte=filters.start_date)
        if filters.end_date:
            queryset = queryset.filter(job__start_date__date__lte=filters.end_date)
        if filters.coordinates:
            if len(filters.coordinates.coordinates) != 2:
                raise GraphQLError(
                    "Coordinates must contain latitude and longitude."
                )
            lat = filters.coordinates.coordinates[0]
            lon = filters.coordinates.coordinates[1]
            range_val = filters.coordinates.range
            unit = filters.coordinates.unit
            earth_radius = 6371 if unit == DistanceUnit.KILOMETERS else 3959
            distance_expr = earth_radius * ACos(
                Cos(Radians(lat))
                * Cos(Radians(F("job__event__coordinates__0")))
                * Cos(Radians(F("job__event__coordinates__1")) - Radians(lon))
                + Sin(Radians(lat)) * Sin(Radians(F("job__event__coordinates__0")))
            )
            queryset = queryset.annotate(event_distance=distance_expr).filter(
                event_distance__lte=range_val
            )
        if filters.status_id:
            try:
                status_id = resolve_id_to_int(filters.status_id)
                queryset = queryset.filter(status_id=status_id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid status ID.")
        elif filters.status_slug:
            status_filter_kwargs = {"status__slug": filters.status_slug}
            if tenant_id:
                status_filter_kwargs["status__tenant_id"] = tenant_id
            queryset = queryset.filter(**status_filter_kwargs)
        else:
            status_values = []
            if filters.statuses:
                status_values.extend([status.value for status in filters.statuses])
            if filters.status:
                status_values.append(filters.status.value)
            if status_values:
                status_filter_kwargs = {"status__slug__in": status_values}
                if tenant_id:
                    status_filter_kwargs["status__tenant_id"] = tenant_id
                queryset = queryset.filter(**status_filter_kwargs)
        return queryset

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def available_jobs(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: JobFiltersInput | None = None,
    ) -> CountableConnection[types.Job]:
        """Get all available jobs."""
        service = JobQueriesService()
        tenant_id = await service.resolve_query_tenant_id(info, filters=filters)
        user = await service.get_user(info)
        role_slug = service.get_role_slug(user)
        queryset = service.get_ordered_queryset(
            tenant_id=tenant_id, q=q, ordering=("start_date",)
        )
        queryset = queryset.filter(
            ongoing=True, closed=False, public=True
        ).prefetch_related("job_requirements")

        if role_slug == "ambassador":
            queryset = queryset.annotate(
                applied=Exists(
                    models.AmbassadorJob.objects.filter(
                        job_id=OuterRef("pk"),
                        ambassador__user=user,
                    )
                )
            )
        else:
            queryset = queryset.annotate(
                applied=Value(False, output_field=BooleanField())
            )
        if filters and filters.event_id:
            try:
                event_id = resolve_id_to_int(filters.event_id)
                queryset = queryset.filter(event_id=event_id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid event ID.")
        if filters:
            queryset = _apply_job_date_filters(queryset, filters)
            queryset = _apply_available_job_filters(queryset, filters)
        if filters and filters.edited is not None:
            queryset = queryset.filter(updated_by__isnull=not filters.edited)
        return await service.get_connection(
            tenant_id=tenant_id,
            first=first,
            after=after,
            last=last,
            before=before,
            queryset=queryset,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def ambassador_jobs(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: AmbassadorJobFiltersInput | None = None,
    ) -> CountableConnection[types.AmbassadorJob]:
        """Get all ambassador jobs."""
        service = AmbassadorJobQueriesService()
        tenant_id = await service.resolve_query_tenant_id(info, filters=filters)
        queryset = service.get_ordered_queryset(
            tenant_id=tenant_id,
            q=q,
        )

        if filters:
            queryset = AmbassadorJobQueries._apply_ambassador_job_filters(
                queryset=queryset,
                filters=filters,
                tenant_id=tenant_id,
            )

        return await service.get_connection(
            tenant_id=tenant_id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
            queryset=queryset,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def ambassador_jobs_mobile(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: AmbassadorJobFiltersInput | None = None,
    ) -> CountableConnection[types.AmbassadorJob]:
        """Get ambassador jobs for the logged ambassador user (mobile)."""
        service = AmbassadorJobQueriesService()
        tenant_id = await service.resolve_query_tenant_id(info, filters=filters)
        user = await service.get_user(info)

        queryset = service.get_ordered_queryset(
            tenant_id=tenant_id,
            q=q,
        ).filter(ambassador__user=user)

        if filters:
            queryset = AmbassadorJobQueries._apply_ambassador_job_filters(
                queryset=queryset,
                filters=filters,
                tenant_id=tenant_id,
            )

        return await service.get_connection(
            tenant_id=tenant_id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
            queryset=queryset,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def ambassador_job(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.AmbassadorJob | None:
        """Get a single ambassador job."""
        try:
            service = AmbassadorJobQueriesService()
            user = await service.get_user(info)
            role_slug = service.get_role_slug(user)
            return await service.get_single_record(
                info,
                id=id,
                uuid=str(uuid) if uuid else None,
                enforce_tenant=role_slug != "ambassador",
            )
        except GraphQLError:
            return None

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def ambassador_job_mobile(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
        filters: AmbassadorJobFiltersInput | None = None,
    ) -> types.AmbassadorJob | None:
        """Get a single ambassador job limited to the logged ambassador user."""
        if id is None and uuid is None and filters is None:
            return None

        service = AmbassadorJobQueriesService()
        user = await service.get_user(info)
        tenant_id = await service.resolve_query_tenant_id(info, filters=filters)
        lookup_filters: dict[str, int | str] = {}
        if id is not None:
            try:
                lookup_filters["id"] = resolve_id_to_int(id)
            except (TypeError, ValueError, GraphQLError):
                return None
        if uuid is not None:
            lookup_filters["uuid"] = str(uuid)

        try:
            queryset = models.AmbassadorJob.objects.select_related(
                "ambassador__user",
                "job",
                "job__event",
                "job__event__timezone",
                "status",
                "rate",
                "tenant",
            ).filter(
                ambassador__user=user,
                **lookup_filters,
            )
            if tenant_id:
                queryset = queryset.filter(tenant_id=tenant_id)
            if filters is not None:
                queryset = AmbassadorJobQueries._apply_ambassador_job_filters(
                    queryset=queryset,
                    filters=filters,
                    tenant_id=tenant_id,
                )
            return await sync_to_async(queryset.get)()
        except models.AmbassadorJob.DoesNotExist:
            return None
        except models.AmbassadorJob.MultipleObjectsReturned:
            return None


# Client/Spark AmbassadorJob Queries (for managing applicants)
@strawberry.type
class ClientSparkAmbassadorJobQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def job_applicants(
        self,
        info: strawberry.Info,
        job_id: strawberry.ID,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.AmbassadorJob]:
        """Get all ambassadors for a specific job."""
        service = AmbassadorJobQueriesService()
        tenant_id = await service.resolve_query_tenant_id(info)

        # Get base queryset filtered by tenant
        queryset = service.get_queryset()
        if tenant_id:
            queryset = queryset.filter(tenant_id=tenant_id)

        # Filter by job_id
        resolved_job_id = resolve_id_to_int(job_id)
        queryset = queryset.filter(job_id=resolved_job_id)

        # Note: q parameter is not used here as AmbassadorJob doesn't have a 'name' field
        # If search is needed, it could be implemented by filtering on related ambassador or job fields

        # Apply ordering
        queryset = queryset.order_by(*service.ordering)

        return await service.get_connection(
            tenant_id=tenant_id,
            first=first,
            after=after,
            last=last,
            before=before,
            queryset=queryset,
        )


# CompanyToAmbassadorReview Queries
class CompanyToAmbassadorReviewQueriesService(JobsBaseQueriesService):
    """Service for company to ambassador review queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.CompanyToAmbassadorReview


@strawberry.type
class CompanyToAmbassadorReviewQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def company_to_ambassador_reviews(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.CompanyToAmbassadorReview]:
        """Get all company to ambassador reviews."""
        service = CompanyToAmbassadorReviewQueriesService()
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
    async def company_to_ambassador_review(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.CompanyToAmbassadorReview | None:
        """Get a single company to ambassador review."""
        try:
            service = CompanyToAmbassadorReviewQueriesService()
            return await service.get_single_record(
                info, id=id, uuid=str(uuid) if uuid else None
            )
        except GraphQLError:
            return None


# AmbassadorToAmbassadorReview Queries
class AmbassadorToAmbassadorReviewQueriesService(JobsBaseQueriesService):
    """Service for ambassador to ambassador review queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.AmbassadorToAmbassadorReview


@strawberry.type
class AmbassadorToAmbassadorReviewQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def ambassador_to_ambassador_reviews(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.AmbassadorToAmbassadorReview]:
        """Get all ambassador to ambassador reviews."""
        service = AmbassadorToAmbassadorReviewQueriesService()
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
    async def ambassador_to_ambassador_review(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.AmbassadorToAmbassadorReview | None:
        """Get a single ambassador to ambassador review."""
        try:
            service = AmbassadorToAmbassadorReviewQueriesService()
            return await service.get_single_record(
                info, id=id, uuid=str(uuid) if uuid else None
            )
        except GraphQLError:
            return None


# QuestionType Queries
class QuestionTypeQueriesService(JobsBaseQueriesService):
    """Service for question type queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.QuestionType


@strawberry.type
class QuestionTypeQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def question_types(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.QuestionType]:
        """Get all question types."""
        service = QuestionTypeQueriesService()
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
    async def question_type(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.QuestionType | None:
        """Get a single question type."""
        try:
            service = QuestionTypeQueriesService()
            return await service.get_single_record(
                info, id=id, uuid=str(uuid) if uuid else None
            )
        except GraphQLError:
            return None


# JobRequirementQuestion Queries
class JobRequirementQuestionQueriesService(JobsBaseQueriesService):
    """Service for job requirement question queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.JobRequirementQuestion


@strawberry.type
class JobRequirementQuestionQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def job_requirement_questions(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.JobRequirementQuestion]:
        """Get all job requirement questions."""
        service = JobRequirementQuestionQueriesService()
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
    async def job_requirement_question(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.JobRequirementQuestion | None:
        """Get a single job requirement question."""
        try:
            service = JobRequirementQuestionQueriesService()
            return await service.get_single_record(
                info, id=id, uuid=str(uuid) if uuid else None
            )
        except GraphQLError:
            return None


# QuestionOption Queries
class QuestionOptionQueriesService(JobsBaseQueriesService):
    """Service for question option queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.QuestionOption


@strawberry.type
class QuestionOptionQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def question_options(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.QuestionOption]:
        """Get all question options."""
        service = QuestionOptionQueriesService()
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
    async def question_option(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.QuestionOption | None:
        """Get a single question option."""
        try:
            service = QuestionOptionQueriesService()
            return await service.get_single_record(
                info, id=id, uuid=str(uuid) if uuid else None
            )
        except GraphQLError:
            return None


# JobRequirementAnswer Queries
class JobRequirementAnswerQueriesService(JobsBaseQueriesService):
    """Service for job requirement answer queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.JobRequirementAnswer


@strawberry.type
class JobRequirementAnswerQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def job_requirement_answers(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.JobRequirementAnswer]:
        """Get all job requirement answers."""
        service = JobRequirementAnswerQueriesService()
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
    async def job_requirement_answer(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.JobRequirementAnswer | None:
        """Get a single job requirement answer."""
        try:
            service = JobRequirementAnswerQueriesService()
            return await service.get_single_record(
                info, id=id, uuid=str(uuid) if uuid else None
            )
        except GraphQLError:
            return None


# -------------------------------------------------------------------
# Job lifecycle queries — applications + favorites
# -------------------------------------------------------------------


class _FavoriteAmbassadorScope(SparkGraphQLMixin):
    """Tenant-scoping shell for the Favorites query + mutations.

    Same posture as ``recaps/report_types.py`` ``_CampaignReportService``
    and ``tenants/forms.py`` ``_FormScope``: clients are pinned to their
    OWN tenant (any ``tenant_id`` argument is ignored so they can never
    read or mutate another brand's roster), while admins (spark-admin /
    staff / superuser / ``@igniteproductions.co``) may target ANY tenant
    via ``tenant_id``. Lives off the resolvers so the query and both
    mutations resolve the concrete tenant the same way without
    duplicating the role logic.
    """

    async def resolve_target_tenant_id(
        self, info: strawberry.Info, requested_tenant_id
    ) -> int | None:
        """The CONCRETE tenant id the caller may operate on, or None.

        * **client** — always their own bound tenant; ``requested_tenant_id``
          is ignored so a client can never reach another brand's favorites.
        * **admin** — the requested tenant id (global id or int), or None
          when none/garbage was passed (callers turn that into a safe
          ``[]`` / ``success=False`` rather than raising).
        """
        user = await self.get_user(info)
        role_slug, is_staff, is_super, email = await resolve_request_user_access(
            user
        )

        if not _is_admin_access(role_slug, is_staff, is_super, email):
            tenant = await self.get_user_tenant(info)
            return tenant.id

        if requested_tenant_id is None:
            return None
        raw = str(requested_tenant_id).strip()
        if not raw:
            return None
        try:
            return resolve_id_to_int(raw)
        except Exception:
            return None


@strawberry.type
class FavoriteAmbassadorQueries:
    """Per-tenant favorite-BA roster. Drives the Favorites tab."""

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def favorite_ambassadors(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID | None = None,
    ) -> list[types.TenantFavoriteAmbassador]:
        """Return every favorited BA for the caller's tenant.

        Tenant-scoped: clients see only their OWN tenant's roster (the
        ``tenantId`` argument is overridden to their tenant); admins see
        the requested tenant's roster. Never raises past the auth gate —
        returns ``[]`` for an out-of-scope/garbage request or on error.
        """
        try:
            resolved = await _FavoriteAmbassadorScope().resolve_target_tenant_id(
                info, tenant_id
            )
        except Exception:
            return []
        if not resolved:
            return []

        def _list():
            qs = (
                models.TenantFavoriteAmbassador.objects
                .select_related("ambassador__user")
                .filter(tenant_id=resolved)
                .order_by("-created_at")
            )
            return list(qs)
        return await sync_to_async(_list)()


@strawberry.type
class JobApplicationQueries:
    """Admin-facing view of BA applications for a specific Job."""

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def active_contractor_agreement(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID | None = None,
    ) -> types.ContractorAgreementType | None:
        """The contractor agreement a BA accepts when applying — the
        tenant's active override if set, else the global Ignite default.
        Null when none is configured (apply isn't gated). The BA passes
        the job's tenant id so brand-specific terms surface."""
        from jobs.models import ContractorAgreement

        tid = None
        if tenant_id is not None:
            try:
                tid = resolve_id_to_int(tenant_id)
            except Exception:  # noqa: BLE001
                tid = None

        def _load():
            ag = ContractorAgreement.active_for_tenant(tid)
            if not ag:
                return None
            return types.ContractorAgreementType(
                uuid=str(ag.uuid), version=ag.version, body=ag.body
            )

        return await sync_to_async(_load)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def my_available_jobs(
        self,
        info: strawberry.Info,
        filters: JobFiltersInput | None = None,
    ) -> list[types.Job]:
        """Posted jobs the calling BA can apply to.

        Filtering:
          - lifecycle_status == 'posted' (job is live on the board)
          - If `favorites_only=True`, BA must be on the tenant's
            TenantFavoriteAmbassador roster
          - BA can't see jobs they've already applied to (any status)
            so the board doesn't show their own past applications
          - Optional marketplace filters (state, date range, min pay)
            from `filters` let the BA narrow the board.

        Newest posted first so today's drops are at the top.
        """
        actor = getattr(info.context.request, "user", None)

        def _list():
            from ambassadors.models import Ambassador
            if not actor or not getattr(actor, "id", None):
                return []
            try:
                amb = Ambassador.objects.get(user_id=actor.id)
            except Ambassador.DoesNotExist:
                return []

            # Which tenants have this BA on their favorites list — used
            # to satisfy the favorites_only gate without joining for
            # every Job row.
            fav_tenant_ids = set(
                models.TenantFavoriteAmbassador.objects.filter(
                    ambassador=amb
                ).values_list("tenant_id", flat=True)
            )

            # Jobs they've already applied to (any status) — hide them.
            applied_job_ids = set(
                models.JobApplication.objects.filter(
                    ambassador=amb
                ).values_list("job_id", flat=True)
            )

            qs = (
                models.Job.objects
                .select_related(
                    "event", "event__tenant", "event__state", "job_title"
                )
                .filter(lifecycle_status=models.Job.STATUS_POSTED)
                .exclude(id__in=applied_job_ids)
                .order_by("-posted_at", "-id")
            )
            qs = _apply_ba_board_filters(qs, filters)

            visible = []
            for job in qs[:200]:
                if job.favorites_only and job.tenant_id not in fav_tenant_ids:
                    continue
                visible.append(job)
            return visible

        return await sync_to_async(_list)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def my_applications(
        self,
        info: strawberry.Info,
        status: str | None = None,
    ) -> list[types.JobApplication]:
        """Gigs the calling BA has applied to, newest first. The job board
        (my_available_jobs) deliberately hides applied jobs, so this is the
        only place a BA can see their application history + current status.

        Optional `status` filter (applied / accepted / declined / withdrawn).
        Tenant scoping is implicit: rows are filtered to the caller's own
        Ambassador, which only spans tenants they belong to. Returns empty for
        non-ambassador users. select_related pulls job + event (+ state +
        title) so the nested `job { ... event { ... } }` summary the mobile
        screen requests resolves without an N+1.
        """
        actor = getattr(info.context.request, "user", None)
        status_filter = (status or "").strip().lower() or None

        def _list():
            from ambassadors.models import Ambassador
            if not actor or not getattr(actor, "id", None):
                return []
            try:
                amb = Ambassador.objects.get(user_id=actor.id)
            except Ambassador.DoesNotExist:
                return []
            qs = (
                models.JobApplication.objects
                .select_related(
                    "job", "job__event", "job__event__state", "job__job_title"
                )
                .filter(ambassador=amb)
                .order_by("-applied_at")
            )
            valid = {
                models.JobApplication.STATUS_APPLIED,
                models.JobApplication.STATUS_ACCEPTED,
                models.JobApplication.STATUS_DECLINED,
                models.JobApplication.STATUS_WITHDRAWN,
            }
            if status_filter in valid:
                qs = qs.filter(status=status_filter)
            return list(qs)

        return await sync_to_async(_list)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def my_job_preferences(
        self,
        info: strawberry.Info,
    ) -> types.AmbassadorJobPreference:
        """The calling BA's job-board preferences. Returns defaults
        (notify on, no state filter, no min rate) when none are saved
        yet — the client never has to handle a null/unset case."""
        actor = getattr(info.context.request, "user", None)

        def _get():
            from ambassadors.models import Ambassador
            if not actor or not getattr(actor, "id", None):
                return types.AmbassadorJobPreference.defaults()
            try:
                amb = Ambassador.objects.get(user_id=actor.id)
            except Ambassador.DoesNotExist:
                return types.AmbassadorJobPreference.defaults()
            pref = (
                models.AmbassadorJobPreference.objects
                .filter(ambassador=amb)
                .first()
            )
            if pref is None:
                return types.AmbassadorJobPreference.defaults()
            return types.AmbassadorJobPreference.from_model(pref)

        return await sync_to_async(_get)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def job_applications(
        self,
        info: strawberry.Info,
        job_id: strawberry.ID,
    ) -> list[types.JobApplication]:
        """Every application row attached to a Job, ordered newest
        first. Includes the BA's name/email/uuid via the type resolver,
        so the admin Jobs page can render the applicant list without
        an N+1 round-trip."""
        def _list():
            from utils.graphql.mixins import resolve_id_to_int
            try:
                job_pk = resolve_id_to_int(job_id)
            except Exception:
                return []
            qs = (
                models.JobApplication.objects
                .select_related("ambassador__user")
                .filter(job_id=job_pk)
                .order_by("-applied_at")
            )
            return list(qs)
        return await sync_to_async(_list)()


# -------------------------------------------------------------------
# BA Briefing queries
# -------------------------------------------------------------------

@strawberry.type
class BriefingTemplateQueries:
    """List + lookup for per-tenant BriefingTemplates."""

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def briefing_templates(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID | None = None,
        include_archived: bool = False,
    ) -> list[types.BriefingTemplate]:
        """Return templates available to the caller's tenant. Admins
        can pass an explicit tenant_id to look up another's templates.

        Tenant-scoped: clients see only their OWN tenant's templates (any
        supplied ``tenantId`` is overridden to their tenant); admins see the
        requested tenant's. Never raises past the auth gate — returns ``[]``
        for an out-of-scope/garbage request or on error.
        """
        from jobs.job_scope import JobScope

        try:
            resolved = await JobScope().resolve_target_tenant_id(info, tenant_id)
        except Exception:
            return []
        # Clients always resolve to their own tenant; an admin with no usable
        # tenant in scope sees nothing rather than every tenant's templates.
        if not resolved:
            return []

        def _list():
            qs = models.BriefingTemplate.objects.filter(tenant_id=resolved)
            if not include_archived:
                qs = qs.filter(is_archived=False)
            # Pre-fetch attachments so the type resolver doesn't N+1.
            qs = qs.prefetch_related("attachments")
            return list(qs)
        return await sync_to_async(_list)()


@strawberry.type
class GigTemplateQueries:
    """List + lookup for per-tenant GigTemplates (Post-Job-modal defaults)."""

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def gig_templates(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID | None = None,
        include_archived: bool = False,
    ) -> list[types.GigTemplate]:
        """Return gig templates available to the caller's tenant. Admins
        can pass an explicit tenant_id to look up another's templates.

        Tenant-scoped: clients see only their OWN tenant's templates (any
        supplied ``tenantId`` is overridden to their tenant); admins see the
        requested tenant's. Never raises past the auth gate — returns ``[]``
        for an out-of-scope/garbage request or on error.
        """
        from jobs.job_scope import JobScope

        try:
            resolved = await JobScope().resolve_target_tenant_id(info, tenant_id)
        except Exception:
            return []
        # Clients always resolve to their own tenant; an admin with no usable
        # tenant in scope sees nothing rather than every tenant's templates.
        if not resolved:
            return []

        def _list():
            qs = models.GigTemplate.objects.filter(tenant_id=resolved)
            if not include_archived:
                qs = qs.filter(is_archived=False)
            return list(qs)
        return await sync_to_async(_list)()


@strawberry.type
class JobBriefingQueries:
    """One-shot lookup for the briefing attached to a specific job."""

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def job_briefing_for_event(
        self,
        info: strawberry.Info,
        event_uuid: strawberry.ID,
    ) -> types.JobBriefingPayload | None:
        """Look up a job briefing by the parent event's UUID. The BA
        mobile app receives shift offers keyed by event/ambassador-event
        UUID — they don't see Job IDs directly — so this is the entry
        point for "show me the briefing for the shift I was offered."

        Caller-aware authorization (``JobScope.can_read_event_briefing``):
        admins -> any event; a tenant member -> only their own tenant's
        events; a BA -> only an event their ambassador is linked to (offered
        via ``AmbassadorEvent`` with ``is_approved=False``, assigned, or
        on-roster) so the mobile shift-offer flow keeps working. Anyone else,
        or an out-of-scope event, gets ``null``. Never raises past the auth
        gate.

        Returns None when no job is attached to the event (BA accepting
        a shift before the job's been posted)."""
        from jobs.job_scope import JobScope

        def _load_job():
            try:
                event_uuid_str = str(event_uuid)
            except Exception:
                return None
            try:
                return (
                    models.Job.objects
                    .prefetch_related("briefing_attachments")
                    .select_related("briefing_template", "event")
                    .filter(event__uuid=event_uuid_str)
                    .order_by("-id")
                    .first()
                )
            except Exception:
                return None

        job = await sync_to_async(_load_job)()
        if not job:
            return None

        # Gate on the parent event's tenant + id BEFORE returning anything —
        # the briefing is keyed by a bare event UUID with no implicit
        # ownership, so any authenticated caller could otherwise read any
        # tenant's brand/products/instructions cross-tenant.
        try:
            allowed = await JobScope().can_read_event_briefing(
                info,
                event_tenant_id=job.event.tenant_id,
                event_id=job.event_id,
            )
        except Exception:
            return None
        if not allowed:
            return None

        def _payload():
            return types.JobBriefingPayload(
                title=job.briefing_title or "",
                body=job.briefing_body or "",
                template_uuid=(
                    str(job.briefing_template.uuid)
                    if job.briefing_template_id else None
                ),
                attachments=list(job.briefing_attachments.all()),
            )
        return await sync_to_async(_payload)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def job_briefing(
        self,
        info: strawberry.Info,
        job_id: strawberry.ID,
    ) -> types.JobBriefingPayload | None:
        """Fetch the briefing attached to a specific job by job id.

        Tenant-scoped: returns ``null`` when the job doesn't exist or its
        tenant is outside the caller's scope (a client can't read another
        brand's briefing by holding/guessing its job id). Admins -> any
        tenant. Never raises.
        """
        from jobs.job_scope import JobScope

        try:
            allowed = await JobScope().accessible_tenant_ids(info)
        except Exception:
            return None

        def _get():
            from utils.graphql.mixins import resolve_id_to_int
            try:
                job_pk = resolve_id_to_int(job_id)
            except Exception:
                return None
            try:
                job = (
                    models.Job.objects
                    .prefetch_related("briefing_attachments")
                    .select_related("briefing_template")
                    .get(id=job_pk)
                )
            except models.Job.DoesNotExist:
                return None
            if allowed is not None and job.tenant_id not in allowed:
                return None
            return types.JobBriefingPayload(
                title=job.briefing_title or "",
                body=job.briefing_body or "",
                template_uuid=(
                    str(job.briefing_template.uuid)
                    if job.briefing_template_id else None
                ),
                attachments=list(job.briefing_attachments.all()),
            )
        return await sync_to_async(_get)()
