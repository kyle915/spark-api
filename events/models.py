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
