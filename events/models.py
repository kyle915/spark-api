from uuid6 import uuid7
from django.db import models, transaction
from django.contrib.postgres.fields import ArrayField
from django.conf import settings
from tenants.models import Tenant, Role

from .managers import (
    ClientManager,
    RequestStatusManager,
    EventStatusManager,
    EventTypeManager,
    EventManager,
)
from utils.models import WithDefaultAttribute, Asyncable


class TimeZone(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=50)
    code = models.CharField(max_length=10)
    offset = models.IntegerField()

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="timezone_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="timezone_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Prevent duplicate, semantically-identical timezones. Existing dupes are
        # collapsed first by the data migration in 0048_dedupe_timezones so this
        # constraint can be added safely on deploy.
        constraints = [
            models.UniqueConstraint(
                fields=["name", "code", "offset"],
                name="uq_timezone_name_code_offset",
            ),
        ]

    def __str__(self):
        return f"{self.offset}"


class State(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=50)
    code = models.CharField(max_length=50)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="state_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="state_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class Location(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=50)
    code = models.CharField(max_length=50)
    zip = models.CharField(max_length=10)

    state = models.ForeignKey(
        State, on_delete=models.RESTRICT, related_name="location", null=True
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="locations_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="locations_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class Client(Asyncable, models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=50)
    email = models.CharField(max_length=254)

    tenant = models.ForeignKey(
        Tenant, on_delete=models.RESTRICT, related_name="clients"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="client_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="client_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ClientManager()


class Distributor(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=255)
    email = models.CharField(max_length=254, null=True)

    location = models.ForeignKey(Location, on_delete=models.RESTRICT, null=True)

    state = models.ForeignKey(
        State,
        on_delete=models.RESTRICT,
        null=True,
        related_name="distributor",
    )

    tenant = models.ForeignKey(
        Tenant, on_delete=models.RESTRICT, related_name="distributors"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="distributor_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="distributor_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class Retailer(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=100, null=True)
    store_contact = models.CharField(max_length=50, null=True)
    is_national = models.BooleanField(default=False)

    location = models.ForeignKey(Location, on_delete=models.RESTRICT, null=True)

    tenant = models.ForeignKey(
        Tenant, on_delete=models.RESTRICT, related_name="retailes"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="retailer_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="retailer_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class ProductType(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=50)

    tenant = models.ForeignKey(
        Tenant, on_delete=models.RESTRICT, related_name="productTypes"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="product_type_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="product_type_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class Product(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=50)
    description = models.TextField(null=True, blank=True)
    image = models.ImageField(upload_to="products/", null=True)

    product_type = models.ForeignKey(ProductType, on_delete=models.RESTRICT)
    tenant = models.ForeignKey(
        Tenant, on_delete=models.RESTRICT, related_name="products"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="product_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="product_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class RequestType(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=50)

    tenant = models.ForeignKey(
        Tenant, on_delete=models.RESTRICT, related_name="requestTypes"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="request_type_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="request_type_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class RequestStatus(WithDefaultAttribute, models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=50)
    slug = models.SlugField(max_length=50, null=True)
    # This create_event flag is used to know if the event should be created
    # if the status is selected
    create_event = models.BooleanField(default=False)
    is_default = models.BooleanField(default=False, db_index=True)

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="request_statuses",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="request_status_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="request_status_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = RequestStatusManager()

    def save(self, *args, **kwargs):
        if not self.slug:
            from django.utils.text import slugify

            self.slug = slugify(self.name)

        with transaction.atomic():
            super().save(*args, **kwargs)

            # Set the create event flag to false if the current status is set to true
            if self.create_event:
                (
                    RequestStatus.objects.filter(tenant=self.tenant, create_event=True)
                    .exclude(pk=self.pk)
                    .update(create_event=False)
                )


class BillingEntity(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=255)

    state = models.ForeignKey(
        State,
        on_delete=models.RESTRICT,
        null=True,
        related_name="billing_entity",
    )

    tenant = models.ForeignKey(
        Tenant, on_delete=models.RESTRICT, related_name="billing_entity"
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="billing_entity_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="billing_entity_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class SchedulingStatus(models.TextChoices):
    """Whether the demo is already booked with the store, or Ignite still
    needs to schedule it. Captured per request (incl. bulk imports) so the
    routed RMM knows which activations still need a booking call."""

    ALREADY_SCHEDULED = "already_scheduled", "Already scheduled with the account"
    NEEDS_SCHEDULING = "needs_scheduling", "Needs scheduling by Ignite"


class Request(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=255)
    date = models.DateTimeField(null=True)
    # Soft-delete timestamp. Null = live; non-null = deleted at this time.
    # All list/detail queries filter to deleted_at IS NULL so the request
    # disappears from the UI; the row stays in the DB so the activity log
    # and any FK-linked events / recaps survive intact. An admin could
    # restore by setting this back to NULL.
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    start_time = models.DateTimeField(null=True, db_index=True)
    end_time = models.DateTimeField(null=True, blank=True)
    address = models.TextField(null=False)
    decline_reason = models.TextField(null=True)
    requestor_email = models.CharField(max_length=254, null=True)
    notes = models.TextField(null=True)
    reviewed = models.BooleanField(default=False)
    # Already booked with the store vs. Ignite still needs to schedule it.
    # Required on new submissions (enforced in the form + bulk importer);
    # nullable at the DB level so legacy rows aren't broken.
    scheduling_status = models.CharField(
        max_length=32,
        choices=SchedulingStatus.choices,
        null=True,
        blank=True,
    )
    # Torch public request form ("Is Non-Active Product Required?").
    # Nullable so Liquid Death / Feel Free / legacy rows stay untouched.
    is_non_active_product_required = models.BooleanField(null=True, blank=True)
    # Torch On-Premise: "Account Spend Amount". Hidden for other types.
    account_spend_amount = models.CharField(max_length=64, null=True, blank=True)
    # Torch Event Activation extras. Hidden unless that request type is selected.
    event_assets_needed = models.TextField(null=True, blank=True)
    load_in_time = models.CharField(max_length=64, null=True, blank=True)
    onsite_poc = models.CharField(max_length=254, null=True, blank=True)
    additional_team_details = models.TextField(null=True, blank=True)
    # Torch spark-form: "How many cases to be shipped?" (5MG+10MG gate).
    cases_to_be_shipped = models.CharField(max_length=32, null=True, blank=True)
    store_number = models.CharField(max_length=254, null=True)
    coordinates = ArrayField(
        models.FloatField(),
        size=2,
        default=list,
    )

    client_name = models.CharField(max_length=50, null=True)
    client_email = models.CharField(max_length=254, null=True)

    distributor_name = models.CharField(max_length=50, null=True)
    distributor_email = models.CharField(max_length=254, null=True)

    retailer_name = models.CharField(max_length=50, null=True)
    retailer_address = models.TextField(null=True)
    retailer_store_contact = models.CharField(max_length=50, null=True)

    store_manager_name = models.CharField(max_length=50, null=True)
    store_manager_phone = models.CharField(max_length=20, null=True)

    timezone = models.ForeignKey(
        TimeZone, on_delete=models.RESTRICT, null=True, related_name="requests"
    )

    client = models.ForeignKey(
        Client,
        on_delete=models.RESTRICT,
        null=True,
        related_name="requests",
    )
    distributor = models.ForeignKey(
        Distributor,
        on_delete=models.RESTRICT,
        null=True,
        related_name="requests",
    )
    retailer = models.ForeignKey(
        Retailer,
        on_delete=models.RESTRICT,
        null=True,
        related_name="requests",
    )
    request_type = models.ForeignKey(
        RequestType,
        on_delete=models.RESTRICT,
        null=False,
        related_name="requests",
    )
    status = models.ForeignKey(
        RequestStatus, on_delete=models.SET_NULL, null=True, related_name="requests"
    )
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="requests",
    )

    billing_entity = models.ForeignKey(
        BillingEntity,
        on_delete=models.RESTRICT,
        null=True,
        related_name="requests",
    )

    rmm_asigned = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="requests",
    )

    location = models.ForeignKey(
        Location,
        on_delete=models.RESTRICT,
        null=True,
        related_name="request",
    )

    state = models.ForeignKey(
        State,
        on_delete=models.RESTRICT,
        null=True,
        related_name="request",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="request_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="request_updated_by",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="request_approved_by",
    )
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            # Master Tracker `requests` resolver (events/queries.py
            # `RequestQueriesService.get_queryset` + the `requests` field):
            #   Request.objects.filter(deleted_at__isnull=True)        # base qs
            #                  .filter(tenant_id=…)                    # tenant scope
            #                  .order_by("date" | "-date")             # Date column sort
            # The hot path is "all live requests for one tenant, sorted by
            # event date". A composite on (tenant, deleted_at, date) serves
            # the equality on tenant + the IS NULL on deleted_at and feeds the
            # date sort in order, so the tracker's single big page is an index
            # range scan rather than a tenant-wide scan + filesort. deleted_at
            # already has a standalone db_index, but that lone index can't
            # cover the tenant predicate or the date ordering.
            models.Index(
                fields=["tenant", "deleted_at", "date"],
                name="ev_request_t_del_date_idx",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self.status:
            self.status = RequestStatus.objects.get_default(self.tenant)
        super().save(*args, **kwargs)


class RequestDetail(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    is_table_needed = models.BooleanField(default=False)
    table_size = models.IntegerField(null=True, blank=True)

    request = models.ForeignKey(
        Request,
        on_delete=models.RESTRICT,
        null=False,
        related_name="request_details",
    )
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=True,
        related_name="request_details",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="request_detail_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="request_detail_updated_by",
    )
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class RequestProduct(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    request = models.ForeignKey(
        Request,
        on_delete=models.RESTRICT,
        null=False,
        related_name="request_product",
    )

    product = models.ForeignKey(
        Product,
        on_delete=models.RESTRICT,
        null=True,
        related_name="request_product",
    )

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=True,
        related_name="request_product",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="request_product_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="request_product_updated_by",
    )
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class RequestStoreManager(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=50)
    phone = models.CharField(max_length=20)

    request = models.ForeignKey(
        Request,
        on_delete=models.RESTRICT,
        null=True,
        related_name="requests_stores_manager",
    )
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=True,
        related_name="requests_stores_managers",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="request_store_manager_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="request_store_manager_updated_by",
    )
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class EventStatus(WithDefaultAttribute, models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=50)
    slug = models.SlugField(max_length=50, null=True)
    is_default = models.BooleanField(default=False, db_index=True)

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="event_statuses",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="event_status_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="event_status_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = EventStatusManager()

    def save(self, *args, **kwargs):
        if not self.slug:
            from django.utils.text import slugify

            self.slug = slugify(self.name)
        super().save(*args, **kwargs)


class EventType(WithDefaultAttribute, models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=50)
    is_default = models.BooleanField(default=False, db_index=True)
    slug = models.SlugField(max_length=50, null=True)

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="event_types",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="event_types_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="event_types_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = EventTypeManager()


class Event(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=255)
    date = models.DateTimeField(null=True)
    coordinates = ArrayField(
        models.FloatField(),
        size=2,
        null=True,
    )

    timezone = models.ForeignKey(
        TimeZone, on_delete=models.RESTRICT, null=True, related_name="events"
    )

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="events",
    )
    request = models.ForeignKey(
        Request,
        on_delete=models.CASCADE,
        # just in case we have records already. We'll validate in the request anyway.
        null=True,
        db_index=True,
    )
    # --- GPS mileage tracker (admin opt-in per gig) ---------------------
    # When True, a BA assigned to this event sees a Start/Stop "Track
    # mileage" control on the gig and can log driving trips
    # (ambassadors.MileageSession). Off by default — only the gigs an admin
    # flags reimburse mileage.
    track_mileage = models.BooleanField(default=False)
    # $/mile reimbursement rate for this gig. Snapshotted onto each session
    # at stop time so later edits don't rewrite past reimbursements. Null =
    # track miles only, no dollar amount.
    # 3 decimal places so per-mile rates like $0.725 store exactly.
    mileage_rate = models.DecimalField(
        max_digits=6, decimal_places=3, null=True, blank=True,
    )
    # When True, this event is EXCLUDED from the tenant dashboard KPIs, the
    # metro/week breakdown, the geographic map, and the reporting rollups
    # (recaps.tenant_overview + field_sampling_report) — for a one-off event
    # in a different campaign phase/market that would otherwise skew a
    # program's numbers (e.g. Feel Free's spring CO/CA activations vs the
    # summer FL/TX sampling program). default False = counts normally, so
    # this is a no-op for every existing event. The recap rows themselves are
    # left fully intact — this only scopes them out of aggregate reporting.
    exclude_from_dashboard = models.BooleanField(default=False, db_index=True)
    # --- Walk-up self-serve clock-in --------------------------------------
    # A short code an admin generates for this event so a BA can clock in +
    # file a recap WITHOUT being pre-assigned: they enter the code (or scan
    # its QR) in the app, which resolves to this event + brand and creates a
    # walk-up AmbassadorEvent (source="walkup", pending admin review). NULL =
    # walk-ups disabled for this event; generating a code enables them,
    # revoking clears it. Codes are minted uppercase from an unambiguous
    # alphabet and matched case-insensitively in the resolver.
    walkup_code = models.CharField(
        max_length=12, null=True, blank=True, unique=True, db_index=True,
    )
    # When the code stops working. Set to the event day + a buffer at
    # generation time; a resolve after this instant returns "expired".
    walkup_code_expires_at = models.DateTimeField(null=True, blank=True)
    # Leaving these fields nullable, we'll validate them in the schema
    # to avoid conflicts with the migrations
    event_type = models.ForeignKey(
        EventType,
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="events",
    )
    status = models.ForeignKey(
        EventStatus,
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="events",
    )

    retailer = models.ForeignKey(
        Retailer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events",
    )
    distributor = models.ForeignKey(
        Distributor,
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="events",
    )

    start_time = models.DateTimeField(null=True, db_index=True)
    end_time = models.DateTimeField(null=True)
    new_end_time = models.DateTimeField(null=True)
    address = models.TextField()
    notes = models.TextField(null=True, blank=True)
    is_national = models.BooleanField(default=False)

    rmm_asigned = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="events",
    )

    location = models.ForeignKey(
        Location,
        on_delete=models.RESTRICT,
        null=True,
        related_name="events",
    )

    state = models.ForeignKey(
        State,
        on_delete=models.RESTRICT,
        null=True,
        related_name="events",
    )

    custom_recap_template = models.ForeignKey(
        "recaps.CustomRecapTemplate",
        on_delete=models.RESTRICT,
        null=True,
        related_name="events",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="events_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="events_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = EventManager()

    class Meta:
        indexes = [
            # Recap lists (recaps/queries.py — both the legacy
            # `RecapQueriesService` and the `CustomRecapQueriesService`)
            # scope by tenant *through* the event join
            # (`event__tenant_id=…`) and filter the event date range
            # (`event__date__date__gte/__lte`, plus the clickable Date sort
            # on the Master Tracker `requests` view which orders Event-linked
            # rows by `date`). A composite on (tenant, date) lets Postgres
            # satisfy the tenant predicate and the date range/sort from one
            # index instead of scanning the whole tenant's events. `date` is
            # nullable but that's fine — NULLs sort together and the leading
            # tenant column still narrows the scan.
            models.Index(fields=["tenant", "date"], name="ev_event_tenant_date_idx"),
        ]


class GoogleCalendarEvent(models.Model):
    """Model to store Google Calendar event ID mapping for events per user."""

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    event = models.ForeignKey(
        Event,
        on_delete=models.CASCADE,
        null=False,
        related_name="google_calendar_events",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=False,
        related_name="google_calendar_event_mappings",
    )

    google_event_id = models.CharField(max_length=255, null=False)

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [["event", "user"]]
        indexes = [
            models.Index(fields=["event", "user"]),
        ]

    def __str__(self):
        return f"Event {self.event.id} -> Google Calendar {self.google_event_id} for user {self.user.id}"


class UserDistributor(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=100)

    distributor = models.ForeignKey(
        Distributor,
        on_delete=models.RESTRICT,
        null=False,
        related_name="user_distributor",
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="user_distributor",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="user_distributor_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="user_distributor_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class UserLocation(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=100)

    location = models.ForeignKey(
        Location,
        on_delete=models.RESTRICT,
        null=False,
        related_name="user_location",
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="user_location",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="user_location_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="user_location_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class NotificationGroup(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=255)
    state = models.BooleanField(default=False)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="notification_group_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="notification_group_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class NotificationGroupUser(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="notification_group_user",
    )

    notification_group = models.ForeignKey(
        NotificationGroup,
        on_delete=models.RESTRICT,
        null=False,
        related_name="notification_group_user",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="notification_group_user_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="notification_group_user_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class NotificationGroupLocation(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    location = models.ForeignKey(
        Location,
        on_delete=models.RESTRICT,
        null=False,
        related_name="notification_group_location",
    )

    notification_group = models.ForeignKey(
        NotificationGroup,
        on_delete=models.RESTRICT,
        null=False,
        related_name="notification_group_location",
    )

    state = models.ForeignKey(
        State,
        on_delete=models.RESTRICT,
        null=False,
        related_name="notification_group_location",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="notification_group_location_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="notification_group_location_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class NotificationGroupRole(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    role = models.ForeignKey(
        Role,
        on_delete=models.RESTRICT,
        null=False,
        related_name="notification_group_role",
    )

    notification_group = models.ForeignKey(
        NotificationGroup,
        on_delete=models.RESTRICT,
        null=False,
        related_name="notification_group_role",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="notification_group_role_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="notification_group_role_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


# ---------------------------------------------------------------------------
# RequestActivityLog
# ---------------------------------------------------------------------------
#
# Append-only audit trail of every meaningful change to a Request. Powers
# the activity timeline panel on the front-end request detail page so
# kyle / RMMs can answer "who did what when" without going to the DB.
#
# Design choices:
#   - Append-only by convention (no update/delete UI). If a row needs
#     correcting, log a compensating entry instead — that keeps the
#     audit story intact.
#   - actor_user is nullable: system-driven events (e.g. recap nudge
#     fires from a cron) have no human user.
#   - kind is a CharField + choices instead of an enum FK so we don't
#     need a separate seed migration each time we add a new event type.
#   - metadata is a flexible JSON blob for kind-specific context (e.g.
#     "from_status" / "to_status" on a status-change, "ba_name" on an
#     invite). Keeps the table schema stable as new event types ship.
#   - Indexed on (tenant, request, -created_at) for the timeline read
#     pattern (latest first, scoped to one request).
class RequestActivityLog(models.Model):
    KIND_CREATED = "created"
    KIND_UPDATED = "updated"
    KIND_STATUS_CHANGED = "status_changed"
    KIND_BA_INVITED = "ba_invited"
    KIND_BA_ACCEPTED = "ba_accepted"
    KIND_BA_DECLINED = "ba_declined"
    KIND_BA_REMOVED = "ba_removed"
    KIND_RECAP_FILED = "recap_filed"
    KIND_CLONED_FROM = "cloned_from"
    KIND_NOTE_ADDED = "note_added"
    KIND_NUDGE_SENT = "nudge_sent"

    KIND_CHOICES = [
        (KIND_CREATED, "Created"),
        (KIND_UPDATED, "Updated"),
        (KIND_STATUS_CHANGED, "Status changed"),
        (KIND_BA_INVITED, "BA invited"),
        (KIND_BA_ACCEPTED, "BA accepted"),
        (KIND_BA_DECLINED, "BA declined"),
        (KIND_BA_REMOVED, "BA removed"),
        (KIND_RECAP_FILED, "Recap filed"),
        (KIND_CLONED_FROM, "Cloned from another request"),
        (KIND_NOTE_ADDED, "Note added"),
        (KIND_NUDGE_SENT, "Nudge sent"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="request_activity_logs",
    )
    request = models.ForeignKey(
        "Request",
        on_delete=models.CASCADE,
        related_name="activity_logs",
    )
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)

    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="request_activity_logs",
    )
    # Free-form summary, e.g. "Status: pending → approved". Optional —
    # the front-end can also render from `kind` + `metadata` directly
    # for finer styling.
    summary = models.CharField(max_length=512, blank=True, default="")
    # Kind-specific context. Examples:
    #   status_changed: {"from": "pending", "to": "approved"}
    #   ba_invited:     {"ambassador_uuid": "...", "ba_name": "..."}
    #   cloned_from:    {"source_request_uuid": "..."}
    metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(
                fields=["tenant", "request", "-created_at"],
                name="ev_actlog_t_r_ctd_idx",
            ),
        ]

    def __str__(self) -> str:
        actor = self.actor_user.email if self.actor_user_id else "system"
        return f"[{self.kind}] {actor} · request={self.request_id}"


class PayrollApproval(models.Model):
    """Admin sign-off on a BA's worked hours for a pay period — the "close-out"
    step on top of the read-only timesheet (events/payroll.py).

    One row per (tenant, ambassador, period_start, period_end). Approving
    snapshots the hours + estimated pay at approval time (so a later clock edit
    doesn't silently change what was signed off), records who/when, and optionally
    stamps ``paid_at`` when the disbursement is made in Wingspan. Spark never moves
    money — this is record-keeping + the gate for the "approved only" export."""

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    period_start = models.DateField()
    period_end = models.DateField()
    hours = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    estimated_pay = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )

    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="payroll_approvals",
    )
    ambassador = models.ForeignKey(
        "ambassadors.Ambassador",
        on_delete=models.CASCADE,
        related_name="payroll_approvals",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="payroll_approvals_made",
    )
    approved_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "ambassador", "period_start", "period_end"],
                name="uq_payroll_approval_period",
            ),
        ]
        indexes = [
            models.Index(
                fields=["tenant", "period_start", "period_end"],
                name="ev_payappr_t_period_idx",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"PayrollApproval amb={self.ambassador_id} "
            f"{self.period_start}->{self.period_end}"
        )


class EventConfirmation(models.Model):
    """One BA's event confirmation email — the durable record that drives
    the booked / 24h-before / 3h-before sends.

    WHY THIS EXISTS AS ITS OWN ROW rather than stamps on AmbassadorEvent
    (which is how the activation reminder and recap nudge dedupe): the shifts
    this covers are typed into the admin tab and generally DON'T exist in
    Spark yet — there is no roster row to hang a stamp on. Deliberately we do
    NOT mint an Event/AmbassadorEvent to get one either: creating a booking
    fires the "New shift offered" push (events/signals.py), syncs Google
    Calendar and lands in dashboard KPIs, so a tab whose only job is to send
    an email would have three loud side effects. `event` /
    `ambassador_event` are set only when an admin picked a shift that already
    existed, purely as a back-reference.

    TIME IS STORED AS AN INSTANT, NOT A WALL-CLOCK DATE. `settings.TIME_ZONE`
    is UTC, so anything derived from `timezone.localdate()` flips a day early
    at 5pm Pacific. `starts_at` is an aware datetime, so "24 hours before"
    is plain instant arithmetic that is correct in every venue timezone; the
    `timezone` FK exists only to RENDER that instant back as the BA's local
    wall-clock ("08/01/2026", "1p - 4p"). The date and time the email shows
    are always derived from `starts_at`/`ends_at`, never stored separately —
    a stored label could drift from the instant the reminders fire on.
    """

    STAGE_BOOKED = "booked"
    STAGE_T24 = "t24"
    STAGE_T3 = "t3"
    STAGE_CHOICES = [
        (STAGE_BOOKED, "Booked"),
        (STAGE_T24, "24 hours before"),
        (STAGE_T3, "3 hours before"),
    ]
    # The two stages the wall-clock sweep is allowed to fire. "booked" is only
    # ever sent by an admin pressing Send, so the sweep must never pick it up.
    REMINDER_STAGES = (STAGE_T24, STAGE_T3)
    # Hours before `starts_at` each reminder stage is due.
    STAGE_LEAD_HOURS = {STAGE_T24: 24, STAGE_T3: 3}

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        related_name="event_confirmations",
    )
    # Back-references, set only when the shift already existed in Spark.
    # SET_NULL so deleting a shift can never take the audit trail of an email
    # that was genuinely sent with it.
    event = models.ForeignKey(
        "events.Event",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="confirmations",
    )
    ambassador_event = models.ForeignKey(
        "ambassadors.AmbassadorEvent",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="confirmations",
    )

    ba_name = models.CharField(max_length=255)
    # 320 = max addr length per RFC 3696 erratum (64 local + @ + 255 domain).
    ba_email = models.EmailField(max_length=320)

    store_name = models.CharField(max_length=255, blank=True, default="")
    address = models.TextField(blank=True, default="")
    # Eyebrow + body label ("Retail Sampling" → "scheduled for a Liquid Death
    # retail sampling"). Plain text rather than an EventType FK so a typed
    # one-off doesn't require the tenant to have that event type configured.
    event_type_label = models.CharField(max_length=120, blank=True, default="")

    starts_at = models.DateTimeField(db_index=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    # THE IANA NAME IS THE SOURCE OF TRUTH for rendering, not the FK below.
    # `TimeZone` is a lookup table that doesn't necessarily contain the zone an
    # admin picked, and when the lookup missed, rendering silently fell back to
    # the ops timezone — `starts_at` stayed correct, so the reminders fired on
    # time, but the email printed the wrong hour to the BA (a 1pm Chicago shift
    # read "11a - 2p"). Storing the name removes the dependency entirely.
    timezone_name = models.CharField(max_length=64, blank=True, default="")
    # Kept as an optional convenience for joins/reporting alongside
    # Event.timezone. Never relied on for rendering.
    timezone = models.ForeignKey(
        TimeZone,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="event_confirmations",
    )

    # Selected SKUs as a list of the option strings offered by the picker
    # (events.event_confirmations.confirmation_product_options). Stored as
    # chosen so the email reproduces the admin's selection even if the SKU
    # list changes.
    products = models.JSONField(default=list, blank=True)

    # Whether the sweep may fire t24/t3 for this row. Opt-in per send (the tab
    # defaults it ON): a confirmation an admin never sent stays silent, so a
    # misbehaving sweep can't reach BAs nobody chose to email.
    send_reminders = models.BooleanField(default=True)
    # Set when an admin calls the shift off. A cancelled row is skipped by the
    # sweep but kept, so the record of what was already sent survives.
    cancelled_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="event_confirmations_created",
    )
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            # The sweep's access path: due reminders for live rows.
            models.Index(
                fields=["send_reminders", "cancelled_at", "starts_at"],
                name="ev_confirm_sweep_idx",
            ),
            models.Index(
                fields=["tenant", "-starts_at"],
                name="ev_confirm_tenant_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"EventConfirmation {self.ba_email} @ {self.starts_at:%Y-%m-%d %H:%M}"

    @property
    def tzinfo(self):
        """The venue's tzinfo for rendering, falling back to the ops timezone.

        Prefers the stored IANA name over the FK — see `timezone_name`. The FK
        is only consulted for rows written before that column existed.

        `TimeZone.offset` is deliberately NEVER used: it's a single fixed
        number, so a shift on the far side of a DST boundary would render an
        hour off. An IANA name resolves the offset per-date.
        """
        from zoneinfo import ZoneInfo

        for name in (
            (self.timezone_name or "").strip(),
            (getattr(self.timezone, "name", "") or "").strip(),
        ):
            if not name:
                continue
            try:
                return ZoneInfo(name)
            except Exception:  # noqa: BLE001 — unknown/typo'd IANA name
                continue
        return ZoneInfo("America/Los_Angeles")

    def local_start(self):
        return self.starts_at.astimezone(self.tzinfo) if self.starts_at else None

    def local_end(self):
        return self.ends_at.astimezone(self.tzinfo) if self.ends_at else None


class EventConfirmationSend(models.Model):
    """One (confirmation, stage) send attempt — the sweep's idempotency ledger.

    The unique constraint on (confirmation, stage) is the real guarantee, not
    the queryset filter: the sweep runs every 15 minutes, and two overlapping
    runs (or a retried GitHub Actions job) would otherwise both pass the
    "not yet sent" check and email the BA twice. The sweep claims a stage by
    INSERTing this row first and only sends if the insert was its own, so the
    database — not timing — decides who sends.

    `sent_at` stays NULL until the driver returns, so a row that failed
    mid-send is distinguishable from one that succeeded and can be retried up
    to MAX_ATTEMPTS instead of being silently dropped.
    """

    MAX_ATTEMPTS = 3

    id = models.BigAutoField(primary_key=True)
    confirmation = models.ForeignKey(
        EventConfirmation,
        on_delete=models.CASCADE,
        related_name="sends",
    )
    stage = models.CharField(
        max_length=16, choices=EventConfirmation.STAGE_CHOICES, db_index=True
    )
    # Snapshot of where it actually went — the confirmation's ba_email can be
    # corrected later, and then it no longer says who was emailed.
    to_email = models.EmailField(max_length=320, blank=True, default="")

    sent_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["confirmation", "stage"],
                name="uq_event_confirmation_stage",
            ),
        ]

    def __str__(self) -> str:
        state = "sent" if self.sent_at else f"unsent({self.attempts})"
        return f"EventConfirmationSend {self.stage} {state}"
