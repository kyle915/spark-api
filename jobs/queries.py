import strawberry
from enum import Enum
from graphql import GraphQLError
from django.db.models import Prefetch

from utils.graphql.inputs import BaseTenantInput, SparkGraphQLInput
from utils.graphql.permissions import StrictIsAuthenticated
from utils.graphql.relay import CountableConnection
from utils.graphql.queries import BaseQueriesService
from utils.graphql.mixins import resolve_id_to_int
from jobs import models
from django.db.models import QuerySet
from django.db.models import Model
from jobs import types
from jobs.inputs import JobFiltersInput, JobStatusFilter
from ambassadors import models as ambassador_models


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

        if role_slug in {"spark-admin", "ambassador"}:
            if filters_tenant_id is None:
                return None
            tenant = await self._get_tenant_without_membership(
                tenant_id=filters_tenant_id
            )
            return tenant.id

        if role_slug == "client":
            tenant = await self.get_user_tenant(
                info,
                tenant_id=filters_tenant_id,
                user=user,
            )
            return tenant.id

        try:
            tenant = await self.get_user_tenant(
                info,
                tenant_id=filters_tenant_id,
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
    ) -> CountableConnection[types.RateType]:
        """Get all rate types."""
        service = RateTypeQueriesService()
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

    def get_queryset(self) -> QuerySet:
        """Get jobs with related attendance and ambassadors prefetched."""
        return (
            self.get_model()
            .objects.select_related(
                "job_title",
                "other_title",
                "company",
                "event",
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
            )
            .all()
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
            if filters.status:
                status_filter_map = {
                    JobStatusFilter.OPEN: False,
                    JobStatusFilter.CLOSED: True,
                }
                queryset = queryset.filter(closed=status_filter_map[filters.status])

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


@strawberry.enum
class AmbassadorJobStatusFilter(str, Enum):
    APPROVED = "approved"
    DECLINED = "declined"
    PENDING = "pending"


@strawberry.input
class AmbassadorJobFiltersInput(BaseTenantInput):
    status: AmbassadorJobStatusFilter | None = None
    status_id: strawberry.ID | None = None
    status_slug: str | None = None


@strawberry.type
class AmbassadorJobQueries:
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
            queryset = queryset.exclude(ambassador_jobs__ambassador__user=user)
        if filters and filters.event_id:
            try:
                event_id = resolve_id_to_int(filters.event_id)
                queryset = queryset.filter(event_id=event_id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid event ID.")
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
            if filters.status_id:
                queryset = queryset.filter(status_id=filters.status_id)
            elif filters.status_slug:
                status_filter_kwargs = {"status__slug": filters.status_slug}
                if tenant_id:
                    status_filter_kwargs["status__tenant_id"] = tenant_id
                queryset = queryset.filter(**status_filter_kwargs)
            elif filters.status:
                status_filter_kwargs = {"status__slug": filters.status.value}
                if tenant_id:
                    status_filter_kwargs["status__tenant_id"] = tenant_id
                queryset = queryset.filter(**status_filter_kwargs)

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
        queryset = queryset.filter(job_id=job_id)

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
