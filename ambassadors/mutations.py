import strawberry
from asgiref.sync import sync_to_async
from strawberry.types import Info
from jobs.models import (
    AmbassadorJob as AmbassadorJobModel,
    Job,
    Status as JobStatus,
)
from jobs.types import AmbassadorJob as AmbassadorJobType
from .models import Ambassador


from utils.graphql.permissions import StrictIsAuthenticated


@strawberry.type
class ApplyAmbassadorJobResponse:
    success: bool
    message: str
    application: AmbassadorJobType | None = None


async def _get_default_application_status(tenant_id: int) -> JobStatus | None:
    """Pick a sensible default status for a new application."""
    for pattern in ("apply", "pending"):
        status = await sync_to_async(
            JobStatus.objects.filter(
                tenant_id=tenant_id, name__icontains=pattern
            ).first
        )()
        if status:
            return status

    return await sync_to_async(
        JobStatus.objects.filter(tenant_id=tenant_id).first
    )()


@strawberry.type
class AmbassadorMutations:
    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def apply_ambassador_job(
        self, info: Info, job_id: strawberry.ID
    ) -> ApplyAmbassadorJobResponse:
        user = info.context.request.user
        # Manual check removed as StrictIsAuthenticated handles it

        try:
            ambassador = await Ambassador.objects.aget(user=user)
        except Ambassador.DoesNotExist:
            return ApplyAmbassadorJobResponse(
                success=False, message="Ambassador profile not found"
            )

        try:
            job = await Job.objects.select_related("tenant", "rate").aget(id=job_id)
        except Job.DoesNotExist:
            return ApplyAmbassadorJobResponse(success=False, message="Job not found")

        if await AmbassadorJobModel.objects.filter(
            ambassador=ambassador, job=job
        ).aexists():
            return ApplyAmbassadorJobResponse(
                success=False, message="Already applied to this job"
            )

        if job.rate is None:
            return ApplyAmbassadorJobResponse(
                success=False,
                message="Job has no rate configured; please contact the client.",
            )

        status = await _get_default_application_status(job.tenant_id)
        if status is None:
            return ApplyAmbassadorJobResponse(
                success=False,
                message="No status configured for this tenant to store applications.",
            )

        application = await AmbassadorJobModel.objects.acreate(
            ambassador=ambassador,
            job=job,
            tenant=job.tenant,
            status=status,
            rate=job.rate,
            appear_as_rfp=False,
            created_by=user,
            updated_by=user,
        )

        return ApplyAmbassadorJobResponse(
            success=True, message="Application successful", application=application
        )
