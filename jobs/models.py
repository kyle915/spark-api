from uuid6 import uuid7
from django.db import models
from django.conf import settings
from ambassadors.models import FileType, Ambassador
from events.models import Location
from tenants.models import Tenant


UNIT: list[tuple[str, str]] = [
    ("H", "Hour"),
    ("D", "Day"),
    ("W", "Week"),
    ("M", "Months"),
]


class CompanyFile(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=100, blank=False, null=False)
    url = models.CharField(max_length=2048, blank=True, null=True)
    file_type = models.ForeignKey(
        FileType,
        on_delete=models.RESTRICT,
        null=False,
        blank=False,
        related_name="company_files",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        blank=False,
        related_name="company_files_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="company_files_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class Company(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    email = models.CharField(max_length=100, blank=False, null=False)
    website_url = models.CharField(max_length=254, blank=True, null=True)
    founding_date = models.DateField(null=True, blank=True)
    phone = models.CharField(max_length=100, blank=False, null=False)
    address = models.CharField(max_length=254, blank=True, null=True)
    about_us = models.TextField(blank=True, null=True)
    company_size_min = models.IntegerField(blank=True, null=True)
    company_size_max = models.IntegerField(blank=True, null=True)
    approved = models.BooleanField(default=False)

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        blank=False,
        related_name="companies",
    )
    location = models.ForeignKey(
        Location,
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="companies",
    )
    cover = models.ForeignKey(
        CompanyFile,
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="companies_cover",
    )
    profile_image = models.ForeignKey(
        CompanyFile,
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="companies_profile_image",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        blank=False,
        related_name="companies_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="companies_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class CompanyReview(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    global_score = models.IntegerField(blank=False, null=False)
    review = models.TextField(blank=False, null=False)
    min_pay_timing = models.IntegerField(blank=False, null=False)
    max_pay_timing = models.IntegerField(blank=False, null=False)
    pay_timing_range = models.IntegerField(blank=False, null=False)

    company = models.ForeignKey(
        Company,
        on_delete=models.RESTRICT,
        null=False,
        blank=False,
        related_name="company_review",
    )
    ambassador = models.ForeignKey(
        Ambassador,
        on_delete=models.RESTRICT,
        null=False,
        blank=False,
        related_name="company_review",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        blank=False,
        related_name="company_reviews_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="company_reviews_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class PayTiming(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    min_pay_timing = models.IntegerField(blank=False, null=False)
    max_pay_timing = models.IntegerField(blank=False, null=False)
    unit = models.CharField(max_length=1, choices=UNIT)

    company_review = models.ForeignKey(
        CompanyReview,
        on_delete=models.RESTRICT,
        null=False,
        blank=False,
        related_name="pay_timings",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        blank=False,
        related_name="pay_timings_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="pay_timings_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class ReviewScore(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=100, blank=True, null=True)
    score = models.IntegerField(blank=True, null=True)

    company_review = models.ForeignKey(
        CompanyReview,
        on_delete=models.RESTRICT,
        null=False,
        blank=False,
        related_name="review_scores",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        blank=False,
        related_name="review_scores_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="review_scores_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class JobTitle(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        blank=False,
        related_name="job_titles",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        blank=False,
        related_name="job_titles_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="job_titles_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class RateType(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=100, blank=False, null=False)

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        blank=False,
        related_name="rate_types",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        blank=False,
        related_name="rate_types_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="rate_types_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class Rate(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    amout = models.DecimalField(max_digits=14, decimal_places=4)

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        blank=False,
        related_name="rates",
    )
    rate_type = models.ForeignKey(
        RateType,
        on_delete=models.RESTRICT,
        null=False,
        blank=False,
        related_name="rates",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        blank=False,
        related_name="rates_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="rates_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


# class Job(models.Model):
#     id = models.BigAutoField(primary_key=True)
#     uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
#     name = models.CharField(max_length=100, blank=False, null=False)
#     description = models.TextField(blank=True, null=True)

#     tenant = models.ForeignKey(
#         Tenant,
#         on_delete=models.RESTRICT,
#         null=False,
#         blank=False,
#         related_name="rate_types",
#     )
#     rate = models.ForeignKey(
#         Rate, on_delete=models.RESTRICT, null=True, blank=True, related_name="jobs"
#     )

#     created_by = models.ForeignKey(
#         settings.AUTH_USER_MODEL,
#         on_delete=models.RESTRICT,
#         null=False,
#         blank=False,
#         related_name="rate_types_created_by",
#     )
#     updated_by = models.ForeignKey(
#         settings.AUTH_USER_MODEL,
#         on_delete=models.RESTRICT,
#         null=True,
#         blank=True,
#         related_name="rate_types_updated_by",
#     )

#     created_at = models.DateTimeField(auto_now_add=True, editable=False)
#     updated_at = models.DateTimeField(auto_now=True)
#     date = models
