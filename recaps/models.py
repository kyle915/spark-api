from uuid6 import uuid7
from django.db import models
from django.conf import settings
from ambassadors.models import FileType, Ambassador
from events.models import Event, TimeZone, Retailer, Product
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
    name = models.CharField(max_length=100, null=False)
    submited_at = models.DateTimeField(null=True)
    total_engagements = models.IntegerField(null=True)
    products_sold = models.IntegerField(null=True)
    total_earnings = models.DecimalField(max_digits=10, decimal_places=4, null=True)
    approved = models.BooleanField(default=False)

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
    name = models.CharField(max_length=100, null=False)
    file = models.FileField(upload_to="recap_files/", null=True)
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
    total_consumer = models.IntegerField(null=False)
    first_time_consumers = models.IntegerField(null=False)
    brand_aware_consumers = models.IntegerField(null=False)
    willing_to_purchase_consumers = models.IntegerField(null=False)
    not_willing_consumers = models.IntegerField(null=False)

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
    demographics = models.CharField(max_length=100, null=True)
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
