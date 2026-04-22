from uuid6 import uuid7
from django.db import models
from django.conf import settings
from ambassadors.models import FileType, Ambassador
from events.models import Event, TimeZone, Retailer, Product, Location, State, EventType
from tenants.models import Tenant
from jobs.models import Job


class FileRecapCategory(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=100, null=False)

    tenant = models.ForeignKey(
        Tenant, on_delete=models.RESTRICT, related_name="file_recap_categories"
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="file_recap_categories_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="file_recap_categories_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class Recap(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.TextField(null=False)
    submited_at = models.DateTimeField(null=True)
    total_engagements = models.IntegerField(null=True)
    products_sold = models.IntegerField(null=True)
    total_earnings = models.DecimalField(max_digits=10, decimal_places=4, null=True)
    total_cans_sold = models.IntegerField(null=True)
    total_packs_sold = models.IntegerField(null=True)
    filling_for_ambassador = models.BooleanField(default=False)
    late = models.BooleanField(default=False)
    incomplete = models.BooleanField(default=False)
    account_spend_amount = models.DecimalField(
        max_digits=10, decimal_places=4, null=True
    )
    approved = models.BooleanField(default=False)
    traffic_description = models.CharField(max_length=255, null=True)
    competitive_presence = models.CharField(max_length=255, null=True)

    timezone = models.ForeignKey(
        TimeZone,
        on_delete=models.RESTRICT,
        null=True,
        related_name="recaps",
    )

    event = models.ForeignKey(
        Event, on_delete=models.RESTRICT, null=False, related_name="recaps"
    )

    ambassador = models.ForeignKey(
        Ambassador,
        on_delete=models.RESTRICT,
        null=True,
        related_name="recaps",
    )

    job = models.ForeignKey(
        Job,
        on_delete=models.RESTRICT,
        null=True,
        related_name="recaps",
    )

    retailer = models.ForeignKey(
        Retailer,
        on_delete=models.RESTRICT,
        null=True,
        related_name="recaps",
    )

    location = models.ForeignKey(
        Location,
        on_delete=models.RESTRICT,
        null=True,
        related_name="recaps",
    )

    state = models.ForeignKey(
        State,
        on_delete=models.RESTRICT,
        null=True,
        related_name="recaps",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="recaps_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="recaps_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class RecapFile(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.TextField(null=True)
    file = models.FileField(upload_to="recap_files/", max_length=1024, null=True)
    approved = models.BooleanField(default=False)

    recap = models.ForeignKey(
        Recap,
        on_delete=models.RESTRICT,
        null=True,
        related_name="recap_files",
    )

    file_type = models.ForeignKey(
        FileType,
        on_delete=models.RESTRICT,
        null=False,
        related_name="recap_files",
    )

    file_recap_category = models.ForeignKey(
        FileRecapCategory,
        on_delete=models.RESTRICT,
        null=True,
        related_name="recap_files",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="recap_files_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="recap_files_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class ConsumerEngagements(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    total_consumer = models.IntegerField(null=True)
    first_time_consumers = models.IntegerField(null=True)
    brand_aware_consumers = models.IntegerField(null=True)
    willing_to_purchase_consumers = models.IntegerField(null=True)
    not_willing_consumers = models.IntegerField(null=True)

    recap = models.ForeignKey(
        Recap,
        on_delete=models.RESTRICT,
        null=False,
        related_name="consumer_engagements",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="consumer_engagements_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="consumer_engagements_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class ProductSamples(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    product = models.ForeignKey(
        Product, on_delete=models.RESTRICT, null=False, related_name="product_samples"
    )
    recap = models.ForeignKey(
        Recap, on_delete=models.RESTRICT, null=False, related_name="product_samples"
    )
    quantity = models.IntegerField(null=False)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="product_samples_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="product_samples_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class TypeOfGood(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=100, null=False)

    tenant = models.ForeignKey(
        Tenant, on_delete=models.RESTRICT, related_name="type_of_goods"
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="type_of_goods_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="type_of_goods_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class SalesPerformance(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    price = models.DecimalField(max_digits=10, decimal_places=4)

    product = models.ForeignKey(
        Product, on_delete=models.RESTRICT, null=False, related_name="sales_performance"
    )
    recap = models.ForeignKey(
        Recap, on_delete=models.RESTRICT, null=False, related_name="sales_performance"
    )
    type_of_good = models.ForeignKey(
        TypeOfGood,
        on_delete=models.RESTRICT,
        null=False,
        related_name="sales_performance",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="sales_performance_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="sales_performance_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class ConsumerFeedback(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    demographics = models.TextField(null=True)
    feedback = models.TextField(null=True)
    quotes = models.TextField(null=True)
    positive_stories = models.TextField(null=True)
    reasons_to_decline = models.TextField(null=True)

    recap = models.ForeignKey(
        Recap, on_delete=models.RESTRICT, null=False, related_name="consumer_feedback"
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="consumer_feedback_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="consumer_feedback_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class AccountFeedback(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    do_differently_feedback = models.TextField(null=True)
    feedback = models.TextField(null=True)
    corpo_card = models.TextField(null=True)
    was_corpo_card_used = models.BooleanField(default=False)

    recap = models.ForeignKey(
        Recap, on_delete=models.RESTRICT, null=False, related_name="account_feedback"
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="account_feedback_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="account_feedback_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


# Custom Recaps
class CustomRecapTemplate(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.TextField(null=False)
    product_samples = models.BooleanField(default=False)
    sales_performance = models.BooleanField(default=False)
    layout = models.JSONField(default=dict, blank=True)

    event_type = models.ForeignKey(
        EventType,
        on_delete=models.RESTRICT,
        null=False,
        related_name="custom_recap_template",
    )

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="custom_recap_template",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="custom_recap_template_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="custom_recap_template_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class CustomRecap(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.TextField(null=False)
    submitted_at = models.DateTimeField(null=True)
    total_engagements = models.IntegerField(null=True)
    filling_for_ambassador = models.BooleanField(default=False)
    late = models.BooleanField(default=False)
    incomplete = models.BooleanField(default=False)
    approved = models.BooleanField(default=False)
    used_corpo_card = models.BooleanField(default=False)

    timezone = models.ForeignKey(
        TimeZone,
        on_delete=models.RESTRICT,
        null=True,
        related_name="custom_recap",
    )

    event = models.ForeignKey(
        Event, on_delete=models.RESTRICT, null=False, related_name="custom_recap"
    )

    ambassador = models.ForeignKey(
        Ambassador,
        on_delete=models.RESTRICT,
        null=True,
        related_name="custom_recap",
    )

    job = models.ForeignKey(
        Job,
        on_delete=models.RESTRICT,
        null=True,
        related_name="custom_recap",
    )

    retailer = models.ForeignKey(
        Retailer,
        on_delete=models.RESTRICT,
        null=True,
        related_name="custom_recap",
    )

    location = models.ForeignKey(
        Location,
        on_delete=models.RESTRICT,
        null=True,
        related_name="custom_recap",
    )

    state = models.ForeignKey(
        State,
        on_delete=models.RESTRICT,
        null=True,
        related_name="custom_recap",
    )

    tenant = models.ForeignKey(
        Tenant, on_delete=models.RESTRICT, null=False, related_name="custom_recap"
    )

    custom_recap_template = models.ForeignKey(
        CustomRecapTemplate,
        on_delete=models.RESTRICT,
        null=False,
        related_name="custom_recap",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="custom_recap_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="custom_recap_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class CustomRecapFieldType(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.TextField(null=False)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="custom_recap_field_type_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="custom_recap_field_type_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class RecapSection(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.TextField(null=False)

    tenant = models.ForeignKey(
        Tenant, on_delete=models.RESTRICT, null=False, related_name="recap_section"
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="recap_section_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="recap_section_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class CustomField(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.TextField(null=False)
    required = models.BooleanField(default=False)

    custom_recap_template = models.ForeignKey(
        CustomRecapTemplate,
        on_delete=models.RESTRICT,
        null=False,
        related_name="custom_field",
    )

    custom_field_type = models.ForeignKey(
        CustomRecapFieldType,
        on_delete=models.RESTRICT,
        null=False,
        related_name="custom_field",
    )

    recap_section = models.ForeignKey(
        RecapSection,
        on_delete=models.RESTRICT,
        null=False,
        related_name="custom_field",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="custom_field_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="custom_field_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class CustomRecapSalePerformance(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    price = models.DecimalField(max_digits=10, decimal_places=4)

    product = models.ForeignKey(
        Product,
        on_delete=models.RESTRICT,
        null=False,
        related_name="custom_recap_sale_performance",
    )
    custom_recap = models.ForeignKey(
        CustomRecap,
        on_delete=models.RESTRICT,
        null=False,
        related_name="custom_recap_sale_performance",
    )
    type_of_good = models.ForeignKey(
        TypeOfGood,
        on_delete=models.RESTRICT,
        null=False,
        related_name="custom_recap_sale_performance",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="custom_recap_sale_performance_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="custom_recap_sales_performance_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class CustomRecapProductSample(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    product = models.ForeignKey(
        Product,
        on_delete=models.RESTRICT,
        null=False,
        related_name="custom_recap_product_sample",
    )

    custom_recap = models.ForeignKey(
        CustomRecap,
        on_delete=models.RESTRICT,
        null=False,
        related_name="custom_recap_product_sample",
    )

    quantity = models.IntegerField(null=False)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="custom_recap_product_sample_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="custom_recap_product_sample_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class CustomFieldValue(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    value = models.TextField(null=False)

    custom_recap = models.ForeignKey(
        CustomRecap,
        on_delete=models.RESTRICT,
        null=False,
        related_name="custom_field_value",
    )

    custom_field = models.ForeignKey(
        CustomField,
        on_delete=models.RESTRICT,
        null=False,
        related_name="custom_field_value",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="custom_field_value_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="custom_field_value_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class CustomRecapFile(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.TextField(null=True)
    url = models.FileField(upload_to="recap_files/", max_length=1024, null=True)
    approved = models.BooleanField(default=False)

    custom_recap = models.ForeignKey(
        CustomRecap,
        on_delete=models.RESTRICT,
        null=True,
        related_name="custom_recap_files",
    )

    file_type = models.ForeignKey(
        FileType,
        on_delete=models.RESTRICT,
        null=False,
        related_name="custom_recap_files",
    )

    file_recap_category = models.ForeignKey(
        FileRecapCategory,
        on_delete=models.RESTRICT,
        null=True,
        related_name="custom_recap_files",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="custom_recap_files_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="custom_recap_files_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)
