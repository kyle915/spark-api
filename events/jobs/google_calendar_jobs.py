import logging

from ambassadors.models import AmbassadorEvent
from events.models import Event, NotificationGroupUser
from tenants.models import Tenant, User, TenantedUser, Role
from utils.queues import Queues


class EventGoogleCalendarJob:

    def __init__(self, event_id: int):
        self.event: Event = Event.objects.get(id=event_id)
        self.tenant: Tenant = self.event.tenant
        self.roles: list[Role] = Role.objects.all()
        self.queues: Queues = Queues()
        self.logger: logging.Logger = logging.getLogger(__name__)

    def handle(self):
        self.send_to_admins()
        self.send_to_clients()
        self.send_to_ambassadors()

    def send_to_admins(self):
        """Send the event to all admins of the tenant
        """
        admin_role: Role = self.roles.get(slug=Role.SPARK_ADMIN_SLUG)
        tenanted_users = self.get_tenanted_users(admin_role)
        for tenanted_user in tenanted_users:
            self.send_to_user(tenanted_user.user)

    def send_to_clients(self):
        """Send the event to all clients of the tenant

        Important: For clients, we need to use the notification group to send the event to the users.
        """
        client_role: Role = self.roles.get(slug=Role.CLIENT_SLUG)
        tenanted_users = self.get_tenanted_users(client_role)
        event_location = self.get_event_location()
        if not event_location:
            self.logger.warning(
                f"Event {self.event.id} does not have a location, skipping client sync")
            return
        n_groups = event_location.notification_group_location.all(
        ).values_list("notification_group_id", flat=True)
        group_users = NotificationGroupUser.objects.filter(
            notification_group_id__in=n_groups,
            user_id__in=tenanted_users.values_list("user_id", flat=True)
        ).select_related("user").all()
        if group_users.count() == 0:
            self.logger.warning(
                f"Event {self.event.id} does not have any group users, skipping client sync")
            return
        for group_user in group_users:
            self.send_to_user(group_user.user)

    def send_to_ambassadors(self):
        """Send the event to all ambassadors of the tenant
        """
        ambassadors = AmbassadorEvent.objects.filter(
            event_id=self.event.id,
            tenant_id=self.tenant.id,
        ).select_related("ambassador", "ambassador__user").all()
        if ambassadors.count() == 0:
            self.logger.warning(
                f"Event {self.event.id} does not have any ambassadors, skipping ambassador sync")
            return
        for ambassador in ambassadors:
            self.send_to_user(ambassador.ambassador.user)

    def send_to_user(self, user: User):
        from events.tasks import sync_event_to_google_calendar
        # Check if the user has an active Google Calendar connection
        # otherwise, skip
        if not hasattr(user, "google_calendar_connection") \
                or not user.google_calendar_connection.is_active:
            self.logger.warning(
                f"User {user.id} does not have an active Google Calendar connection")
            return
        self.queues.default.add(
            sync_event_to_google_calendar, user.id, self.event.id)

    def get_tenanted_users(self, role: Role):
        return TenantedUser.objects.filter(
            tenant_id=self.tenant.id,
            is_active=True,
            user__role_id=role.id
        ).select_related("user").all()

    def get_event_location(self):
        if self.event.request:
            # Location comes from request's retailer or distributor
            if self.event.request.retailer and self.event.request.retailer.location:
                return self.event.request.retailer.location
            if self.event.request.distributor and self.event.request.distributor.location:
                return self.event.request.distributor.location
        if self.event.retailer:
            return self.event.retailer.location
        if self.event.distributor:
            return self.event.distributor.location
        return None
