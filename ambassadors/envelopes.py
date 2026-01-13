from utils.mailer import Envelope, Mailer
from ambassadors.models import AmbassadorEvent, AmbassadorInvitation


class AmbassadorEventApplicationMailer(Mailer):
    """
    The Ambassador Event Application Mailer.
    """

    def __init__(self, application: AmbassadorEvent):
        self.application = application

    def envelope(self) -> Envelope:
        return Envelope(
            subject="Your application has been received",
            template="ambassadors.templates.emails.event_application",
            to_emails=[self.application.ambassador.user.email],
            context={
                "application": self.application
            }
        )


class NotifyApplicationToClientMailer(Mailer):
    def __init__(self, application: AmbassadorEvent):
        self.application = application

    def envelope(self) -> Envelope:
        from tenants.models import TenantedUser, Role
        users = TenantedUser.objects.filter(
            tenant=self.application.tenant,
            user__role__slug=Role.CLIENT_SLUG
        ).select_related("user", "user__role")
        to_emails = [user.user.email for user in users]

        return Envelope(
            subject="New application has been received",
            template="ambassadors.templates.emails.notify_application_to_client",
            to_emails=to_emails,
            context={
                "application": self.application
            }
        )


class SendInvitationMailToAmbassadorMailer(Mailer):
    def __init__(self, invitation: AmbassadorInvitation):
        self.invitation = invitation

    def envelope(self) -> Envelope:
        return Envelope(
            subject="You have been invited to a job",
            template="ambassadors.templates.emails.send_invitation_to_ambassador",
            to_emails=[self.invitation.email],
            context={
                "invitation": self.invitation
            }
        )
