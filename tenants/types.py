import django.contrib.sites.requests
import strawberry
import strawberry_django
from asgiref.sync import sync_to_async
from utils.gcs import extract_blob_name_from_url, public_url
from .models import Tenant, Role, User, TenantTheme, SupportTicket
from strawberry.relay import Node


@strawberry_django.type(Role)
class RoleType(Node):
    uuid: strawberry.auto
    name: strawberry.auto


@strawberry_django.type(Tenant)
class TenantType(Node):
    uuid: strawberry.auto
    name: strawberry.auto
    slug: strawberry.auto
    request_url_name: strawberry.auto
    linked_sheet_url: strawberry.auto
    # Read side of the scheduled monthly-report opt-in (toggled via
    # setScheduledReportEnabled). Lets the admin UI show the current
    # ON/OFF state. Default False (opt-in only).
    scheduled_report_enabled: strawberry.auto
    # Read side of the WEEKLY client-digest opt-in (toggled via
    # setClientWeeklyDigestEnabled) — its own flag so the weekly digest and
    # the monthly report roll out independently. Default False (opt-in only).
    client_weekly_digest_enabled: strawberry.auto

    @strawberry.field
    def recap_recipient_emails(self) -> str:
        """Raw stored list of extra recap-approval recipient emails for
        this brand (comma/newline/semicolon-separated as the admin entered
        it). "" when none are configured. Parsed at send time alongside the
        RMM, client-role users, and requestor."""
        return self.recap_recipient_emails or ""

    @strawberry.field
    async def default_external_rmm(self) -> "SparkUserType | None":
        """The user all external (public-form) requests route to, if set
        on the Team page. None = fall back to territory/Ignite routing."""
        return await sync_to_async(lambda obj: obj.default_external_rmm)(self)

    @strawberry.field(name="image")
    def image_url(self) -> str | None:
        """Return the public URL for the tenant image if one exists.

        Aliased to GraphQL field `image` via name= so we can keep the
        Python method off the shadow path. Avoids an extra ORM round
        trip per row.
        """
        # __dict__-first; safe getattr fallback for queries where the
        # optimizer defers the column.
        field_file = self.__dict__.get("image")
        if field_file is None:
            try:
                field_file = getattr(self, "image", None)
            except Exception:
                return None
        if not field_file:
            return None
        try:
            blob = field_file.name
        except Exception:
            blob = str(field_file)
        blob_name = extract_blob_name_from_url(blob)
        return public_url(blob_name)


@strawberry_django.type(User)
class SparkUserType(Node):
    uuid: strawberry.auto
    username: strawberry.auto
    email: strawberry.auto
    first_name: strawberry.auto
    last_name: strawberry.auto
    image: strawberry.auto


@strawberry_django.type(TenantTheme)
class TenantThemeType(Node):
    name: strawberry.auto
    color_scheme: strawberry.auto
    css_variables: strawberry.auto
    tenant: strawberry.auto


@strawberry_django.type(SupportTicket)
class SupportTicketType(Node):
    """A captured Help-page support request. Returned by createSupportTicket
    (so the FE can confirm the row was saved) and the admin
    tenantSupportTickets query."""

    uuid: strawberry.auto
    subject: strawberry.auto
    body: strawberry.auto
    category: strawberry.auto
    status: strawberry.auto
    created_at: strawberry.auto
    created_by: "SparkUserType | None"
    tenant: "TenantType | None"
