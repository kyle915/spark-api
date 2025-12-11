from uuid6 import uuid7
from django.db import models, transaction
from django.contrib.postgres.fields import ArrayField
from django.conf import settings
from tenants.models import Tenant

from .managers import (
    RequestStatusManager,
    EventStatusManager,
    EventTypeManager,
    EventManager,
)
from utils.models import WithDefaultAttribute


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


class Location(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=50)
    code = models.CharField(max_length=50, unique=True)
    zip = models.CharField(max_length=10)

    tenant = models.ForeignKey(
        Tenant, on_delete=models.RESTRICT, related_name="locations"
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


class Client(models.Model):
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


class Distributor(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=50)
    email = models.CharField(max_length=254)

    location = models.ForeignKey(Location, on_delete=models.RESTRICT)
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
    name = models.CharField(max_length=50)
    address = models.CharField(max_length=100)
    store_contact = models.CharField(max_length=50)

    location = models.ForeignKey(Location, on_delete=models.RESTRICT)
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


class Request(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=50)
    date = models.DateTimeField(null=True)
    start_time = models.DateTimeField(null=True, db_index=True)
    end_time = models.DateTimeField(null=True, blank=True)
    address = models.CharField(max_length=100)
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
    retailer_address = models.CharField(max_length=100, null=True)
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
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

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
        null=False,
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
    name = models.CharField(max_length=50)
    date = models.DateTimeField(null=True)
    coordinates = ArrayField(
        models.FloatField(),
        size=2,
        null=True,
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

    start_time = models.DateTimeField(null=True, db_index=True)
    end_time = models.DateTimeField(null=True)
    address = models.CharField(max_length=100, null=False, default="")
    notes = models.TextField(null=True, blank=True)
    is_national = models.BooleanField(default=False)

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
