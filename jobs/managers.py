from typing import TYPE_CHECKING

from asgiref.sync import async_to_sync
from django.db import models
import logging
from utils.models import BaseManager
from utils.onesignal import OneSignalError, one_signal_client

if TYPE_CHECKING:
    from jobs.models import Job, Status
    from ambassadors.models import Ambassador
    from tenants.models import User


logger = logging.getLogger(__name__)


class StatusManager(BaseManager, models.Manager):
    """Manager for Status model with async support."""

    def get_invited(self, tenant_id: int, user):
        """
        Get or create the 'invited' status for a tenant.

        Args:
            tenant_id: Tenant ID
            user: User instance (not user_id) for created_by/updated_by

        Returns:
            Status: The invited status instance
        """
        try:
            status = self.get(slug="invited", tenant_id=tenant_id)
        except self.model.DoesNotExist:
            status = self.create(name="Invited",
                                 slug="invited",
                                 tenant_id=tenant_id,
                                 created_by=user,
                                 updated_by=user
                                 )
        return status

    def get_accepted(self, tenant_id: int, user):
        """
        Get or create the 'accepted' status for a tenant.

        Args:
            tenant_id: Tenant ID
            user: User instance (not user_id) for created_by/updated_by

        Returns:
            Status: The accepted status instance
        """
        try:
            status = self.get(slug="accepted", tenant_id=tenant_id)
        except self.model.DoesNotExist:
            status = self.create(name="Accepted",
                                 slug="accepted",
                                 tenant_id=tenant_id,
                                 created_by=user,
                                 updated_by=user
                                 )
        return status


class JobManager(BaseManager, models.Manager):
    """Manager for Job model with async support."""

    pass


class AmbassadorJobManager(BaseManager, models.Manager):
    """Manager for AmbassadorJob model with async support."""

    def create_and_invite(self, job: "Job", ambassador: "Ambassador", action_by: "User"):
        from ambassadors.models import AmbassadorInvitation
        from jobs.models import Status

        invited_status = None
        if job:
            # prepare the invited status
            invited_status = Status.objects.get_invited(
                tenant_id=job.tenant_id, user=action_by
            )

        ambassador_job = self.create(
            ambassador=ambassador,
            job=job,
            tenant=job.tenant,
            status=invited_status,
            rate=job.rate,
            appear_as_rfp=True,
            created_by=action_by,
            updated_by=action_by,
        )

        AmbassadorInvitation.objects.create_and_send_invite(
            email=ambassador.user.email,
            ambassador=ambassador,
            tenant=job.tenant,
            invited_by=action_by,
            job=job,
        )
        self._send_assignment_push(ambassador_job)

        return ambassador_job

    def _send_assignment_push(self, ambassador_job):
        ambassador = getattr(ambassador_job, "ambassador", None)
        user = getattr(ambassador, "user", None)
        if not user:
            return

        job = ambassador_job.job
        deep_link = f"spark://app/tabs/my-gigs/{job.id}"
        try:
            async_to_sync(one_signal_client.send_push)(
                external_ids=[str(user.uuid)],
                title="New job invitation",
                message=f"You were invited to {job.name}.",
                url=deep_link,
                data={
                    "type": "job_invited",
                    "job_id": str(job.id),
                    "ambassador_job_id": str(ambassador_job.id),
                    "deep_link": deep_link,
                },
            )
        except OneSignalError as exc:
            logger.warning(
                "Failed to send OneSignal assignment push for ambassador_job=%s: %s",
                ambassador_job.id,
                exc,
            )

    def accept_from_invitation(self, invitation):
        from jobs.models import Status

        ambassador_job = self.get(
            ambassador=invitation.ambassador, job=invitation.job)
        if not ambassador_job:
            raise ValueError("Ambassador job not found for this invitation.")

        accepted_status = Status.objects.get_accepted(
            tenant_id=invitation.tenant_id, user=invitation.invited_by
        )
        ambassador_job.status = accepted_status
        ambassador_job.save()
        return ambassador_job
