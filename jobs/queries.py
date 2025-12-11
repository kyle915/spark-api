import strawberry
from graphql import GraphQLError

from utils.graphql.permissions import StrictIsAuthenticated
from utils.graphql.relay import CountableConnection
from utils.graphql.queries import BaseQueriesService
from jobs import models
from django.db.models import QuerySet
from django.db.models import Model
from jobs import types
from jobs.inputs import JobFiltersInput


# Status Queries
class StatusQueriesService(BaseQueriesService):
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
        tenant = await service.get_user_tenant(info)
        return await service.get_connection(
            tenant_id=tenant.id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def ambassador_job_status(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> types.Status | None:
        """Get a single status."""
        try:
            service = StatusQueriesService()
            tenant = await service.get_user_tenant(info)
            return await service.get_record(id, tenant.id)
        except GraphQLError:
            return None


# CompanyFile Queries
class CompanyFileQueriesService(BaseQueriesService):
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
        tenant = await service.get_user_tenant(info)
        return await service.get_connection(
            tenant_id=tenant.id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def company_file(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> types.CompanyFile | None:
        """Get a single company file."""
        try:
            service = CompanyFileQueriesService()
            tenant = await service.get_user_tenant(info)
            return await service.get_record(id, tenant.id)
        except GraphQLError:
            return None


# Company Queries
class CompanyQueriesService(BaseQueriesService):
    """Service for company queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Company


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
        tenant = await service.get_user_tenant(info)
        return await service.get_connection(
            tenant_id=tenant.id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def company(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> types.Company | None:
        """Get a single company."""
        try:
            service = CompanyQueriesService()
            tenant = await service.get_user_tenant(info)
            return await service.get_record(id, tenant.id)
        except GraphQLError:
            return None


# CompanyReview Queries
class CompanyReviewQueriesService(BaseQueriesService):
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
        tenant = await service.get_user_tenant(info)
        return await service.get_connection(
            tenant_id=tenant.id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def company_review(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> types.CompanyReview | None:
        """Get a single company review."""
        try:
            service = CompanyReviewQueriesService()
            tenant = await service.get_user_tenant(info)
            return await service.get_record(id, tenant.id)
        except GraphQLError:
            return None


# PayTiming Queries
class PayTimingQueriesService(BaseQueriesService):
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
        tenant = await service.get_user_tenant(info)
        return await service.get_connection(
            tenant_id=tenant.id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def pay_timing(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> types.PayTiming | None:
        """Get a single pay timing."""
        try:
            service = PayTimingQueriesService()
            tenant = await service.get_user_tenant(info)
            return await service.get_record(id, tenant.id)
        except GraphQLError:
            return None


# ReviewScore Queries
class ReviewScoreQueriesService(BaseQueriesService):
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
        tenant = await service.get_user_tenant(info)
        return await service.get_connection(
            tenant_id=tenant.id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def review_score(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> types.ReviewScore | None:
        """Get a single review score."""
        try:
            service = ReviewScoreQueriesService()
            tenant = await service.get_user_tenant(info)
            return await service.get_record(id, tenant.id)
        except GraphQLError:
            return None


# JobTitle Queries
class JobTitleQueriesService(BaseQueriesService):
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
        tenant = await service.get_user_tenant(info)
        return await service.get_connection(
            tenant_id=tenant.id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def job_title(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> types.JobTitle | None:
        """Get a single job title."""
        try:
            service = JobTitleQueriesService()
            tenant = await service.get_user_tenant(info)
            return await service.get_record(id, tenant.id)
        except GraphQLError:
            return None


# RateType Queries
class RateTypeQueriesService(BaseQueriesService):
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
        tenant = await service.get_user_tenant(info)
        return await service.get_connection(
            tenant_id=tenant.id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def rate_type(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> types.RateType | None:
        """Get a single rate type."""
        try:
            service = RateTypeQueriesService()
            tenant = await service.get_user_tenant(info)
            return await service.get_record(id, tenant.id)
        except GraphQLError:
            return None


# Rate Queries
class RateQueriesService(BaseQueriesService):
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
        tenant = await service.get_user_tenant(info)
        return await service.get_connection(
            tenant_id=tenant.id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def rate(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> types.Rate | None:
        """Get a single rate."""
        try:
            service = RateQueriesService()
            tenant = await service.get_user_tenant(info)
            return await service.get_record(id, tenant.id)
        except GraphQLError:
            return None


# Job Queries
class JobQueriesService(BaseQueriesService):
    """Service for job queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Job


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
        tenant = await service.get_user_tenant(
            info, tenant_id=filters.tenant_id if filters else None
        )
        queryset = service.get_ordered_queryset(
            tenant_id=tenant.id,
            q=q,
        )

        if filters and filters.event_id:
            queryset = queryset.filter(event_id=filters.event_id)

        return await service.get_connection(
            tenant_id=tenant.id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
            queryset=queryset,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def job(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> types.Job | None:
        """Get a single job."""
        try:
            service = JobQueriesService()
            tenant = await service.get_user_tenant(info)
            return await service.get_record(id, tenant.id)
        except GraphQLError:
            return None


# JobFile Queries


class JobFileQueriesService(BaseQueriesService):
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
        tenant = await service.get_user_tenant(info)
        return await service.get_connection(
            tenant_id=tenant.id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def job_file(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> types.JobFile | None:
        """Get a single job file."""
        try:
            service = JobFileQueriesService()
            tenant = await service.get_user_tenant(info)
            return await service.get_record(id, tenant.id)
        except GraphQLError:
            return None


# JobRequirementType Queries
class JobRequirementTypeQueriesService(BaseQueriesService):
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
        tenant = await service.get_user_tenant(info)
        return await service.get_connection(
            tenant_id=tenant.id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def job_requirement_type(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> types.JobRequirementType | None:
        """Get a single job requirement type."""
        try:
            service = JobRequirementTypeQueriesService()
            tenant = await service.get_user_tenant(info)
            return await service.get_record(id, tenant.id)
        except GraphQLError:
            return None


# JobRequirement Queries
class JobRequirementQueriesService(BaseQueriesService):
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
        tenant = await service.get_user_tenant(info)
        return await service.get_connection(
            tenant_id=tenant.id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def job_requirement(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> types.JobRequirement | None:
        """Get a single job requirement."""
        try:
            service = JobRequirementQueriesService()
            tenant = await service.get_user_tenant(info)
            return await service.get_record(id, tenant.id)
        except GraphQLError:
            return None


# JobRequirementFile Queries
class JobRequirementFileQueriesService(BaseQueriesService):
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
        tenant = await service.get_user_tenant(info)
        return await service.get_connection(
            tenant_id=tenant.id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def job_requirement_file(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> types.JobRequirementFile | None:
        """Get a single job requirement file."""
        try:
            service = JobRequirementFileQueriesService()
            tenant = await service.get_user_tenant(info)
            return await service.get_record(id, tenant.id)
        except GraphQLError:
            return None


# AmbassadorJob Queries
class AmbassadorJobQueriesService(BaseQueriesService):
    """Service for ambassador job queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.AmbassadorJob


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
        tenant = await service.get_user_tenant(
            info, tenant_id=filters.tenant_id if filters else None
        )
        queryset = service.get_ordered_queryset(
            tenant_id=tenant.id, q=q, ordering=("start_date",))
        queryset = queryset.filter(ongoing=True, closed=False, public=True)\
            .prefetch_related("job_requirements")
        if filters and filters.event_id:
            queryset = queryset.filter(event_id=filters.event_id)
        return await service.get_connection(
            tenant_id=tenant.id,
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
    ) -> CountableConnection[types.AmbassadorJob]:
        """Get all ambassador jobs."""
        service = AmbassadorJobQueriesService()
        tenant = await service.get_user_tenant(info)
        return await service.get_connection(
            tenant_id=tenant.id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def ambassador_job(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> types.AmbassadorJob | None:
        """Get a single ambassador job."""
        try:
            service = AmbassadorJobQueriesService()
            tenant = await service.get_user_tenant(info)
            return await service.get_record(id, tenant.id)
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
        tenant = await service.get_user_tenant(info)

        # Get base queryset filtered by tenant
        queryset = service.get_queryset().filter(tenant_id=tenant.id)

        # Filter by job_id
        queryset = queryset.filter(job_id=job_id)

        # Note: q parameter is not used here as AmbassadorJob doesn't have a 'name' field
        # If search is needed, it could be implemented by filtering on related ambassador or job fields

        # Apply ordering
        queryset = queryset.order_by(*service.ordering)

        return await service.get_connection(
            tenant_id=tenant.id,
            first=first,
            after=after,
            last=last,
            before=before,
            queryset=queryset,
        )


# CompanyToAmbassadorReview Queries
class CompanyToAmbassadorReviewQueriesService(BaseQueriesService):
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
        tenant = await service.get_user_tenant(info)
        return await service.get_connection(
            tenant_id=tenant.id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def company_to_ambassador_review(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> types.CompanyToAmbassadorReview | None:
        """Get a single company to ambassador review."""
        try:
            service = CompanyToAmbassadorReviewQueriesService()
            tenant = await service.get_user_tenant(info)
            return await service.get_record(id, tenant.id)
        except GraphQLError:
            return None


# AmbassadorToAmbassadorReview Queries
class AmbassadorToAmbassadorReviewQueriesService(BaseQueriesService):
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
        tenant = await service.get_user_tenant(info)
        return await service.get_connection(
            tenant_id=tenant.id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def ambassador_to_ambassador_review(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> types.AmbassadorToAmbassadorReview | None:
        """Get a single ambassador to ambassador review."""
        try:
            service = AmbassadorToAmbassadorReviewQueriesService()
            tenant = await service.get_user_tenant(info)
            return await service.get_record(id, tenant.id)
        except GraphQLError:
            return None


# QuestionType Queries
class QuestionTypeQueriesService(BaseQueriesService):
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
        tenant = await service.get_user_tenant(info)
        return await service.get_connection(
            tenant_id=tenant.id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def question_type(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> types.QuestionType | None:
        """Get a single question type."""
        try:
            service = QuestionTypeQueriesService()
            tenant = await service.get_user_tenant(info)
            return await service.get_record(id, tenant.id)
        except GraphQLError:
            return None


# JobRequirementQuestion Queries
class JobRequirementQuestionQueriesService(BaseQueriesService):
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
        tenant = await service.get_user_tenant(info)
        return await service.get_connection(
            tenant_id=tenant.id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def job_requirement_question(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> types.JobRequirementQuestion | None:
        """Get a single job requirement question."""
        try:
            service = JobRequirementQuestionQueriesService()
            tenant = await service.get_user_tenant(info)
            return await service.get_record(id, tenant.id)
        except GraphQLError:
            return None


# QuestionOption Queries
class QuestionOptionQueriesService(BaseQueriesService):
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
        tenant = await service.get_user_tenant(info)
        return await service.get_connection(
            tenant_id=tenant.id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def question_option(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> types.QuestionOption | None:
        """Get a single question option."""
        try:
            service = QuestionOptionQueriesService()
            tenant = await service.get_user_tenant(info)
            return await service.get_record(id, tenant.id)
        except GraphQLError:
            return None


# JobRequirementAnswer Queries
class JobRequirementAnswerQueriesService(BaseQueriesService):
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
        tenant = await service.get_user_tenant(info)
        return await service.get_connection(
            tenant_id=tenant.id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def job_requirement_answer(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> types.JobRequirementAnswer | None:
        """Get a single job requirement answer."""
        try:
            service = JobRequirementAnswerQueriesService()
            tenant = await service.get_user_tenant(info)
            return await service.get_record(id, tenant.id)
        except GraphQLError:
            return None
