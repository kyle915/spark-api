from uuid6 import uuid7
from django.db import models
from django.contrib.postgres.fields import ArrayField
from django.conf import settings
from ambassadors.models import FileType, Ambassador
from events.models import Location, Event
from tenants.models import Tenant


UNIT: list[tuple[str, str]] = [
    ("H", "Hour"),
    ("D", "Day"),
    ("W", "Week"),
    ("M", "Months"),
]


class Status(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=100)

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="statuses",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="statuses_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="statuses_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class CompanyFile(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=100, null=False)
    url = models.CharField(max_length=2048, null=True)
    file_type = models.ForeignKey(
        FileType,
        on_delete=models.RESTRICT,
        null=False,
        related_name="company_files",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="company_files_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="company_files_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class Company(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    email = models.CharField(max_length=100, null=False)
    website_url = models.CharField(max_length=254, null=True)
    founding_date = models.DateField(null=True, blank=True)
    phone = models.CharField(max_length=100, null=False)
    address = models.CharField(max_length=254, null=True)
    about_us = models.TextField(null=True)
    company_size_min = models.IntegerField(null=True)
    company_size_max = models.IntegerField(null=True)
    approved = models.BooleanField(default=False)

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="companies",
    )
    location = models.ForeignKey(
        Location,
        on_delete=models.RESTRICT,
        null=True,
        related_name="companies",
    )
    cover = models.ForeignKey(
        CompanyFile,
        on_delete=models.RESTRICT,
        null=True,
        related_name="companies_cover",
    )
    profile_image = models.ForeignKey(
        CompanyFile,
        on_delete=models.RESTRICT,
        null=True,
        related_name="companies_profile_image",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="companies_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="companies_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class CompanyReview(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    global_score = models.IntegerField(null=False)
    review = models.TextField(null=False)
    min_pay_timing = models.IntegerField(null=False)
    max_pay_timing = models.IntegerField(null=False)
    pay_timing_range = models.IntegerField(null=False)

    company = models.ForeignKey(
        Company,
        on_delete=models.RESTRICT,
        null=False,
        related_name="company_review",
    )
    ambassador = models.ForeignKey(
        Ambassador,
        on_delete=models.RESTRICT,
        null=False,
        related_name="company_review",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="company_reviews_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="company_reviews_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class PayTiming(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    min_pay_timing = models.IntegerField(null=False)
    max_pay_timing = models.IntegerField(null=False)
    unit = models.CharField(max_length=1, choices=UNIT)

    company_review = models.ForeignKey(
        CompanyReview,
        on_delete=models.RESTRICT,
        null=False,
        related_name="pay_timings",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="pay_timings_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="pay_timings_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class ReviewScore(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=100, null=True)
    score = models.IntegerField(null=True)

    company_review = models.ForeignKey(
        CompanyReview,
        on_delete=models.RESTRICT,
        null=False,
        related_name="review_scores",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="review_scores_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="review_scores_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class JobTitle(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=100, null=True)

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="job_titles",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="job_titles_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="job_titles_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class RateType(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=100, null=False)

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="rate_types",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="rate_types_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="rate_types_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class Rate(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    amount = models.DecimalField(max_digits=14, decimal_places=4)

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="rates",
    )
    rate_type = models.ForeignKey(
        RateType,
        on_delete=models.RESTRICT,
        null=False,
        related_name="rates",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="rates_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="rates_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class Job(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=100, null=False)
    description = models.TextField(null=True)
    code = models.CharField(max_length=100)
    address = models.CharField(max_length=255)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    public = models.BooleanField(default=False)
    closed = models.BooleanField(default=False)
    national = models.BooleanField(default=False)
    ongoing = models.BooleanField(default=False)

    job_title = models.ForeignKey(
        JobTitle,
        on_delete=models.RESTRICT,
        null=False,
        related_name="jobs",
    )

    other_title = models.ForeignKey(
        JobTitle,
        on_delete=models.RESTRICT,
        null=True,
        related_name="jobs_other_titles",
    )

    company = models.ForeignKey(
        Company, on_delete=models.RESTRICT, null=False, related_name="jobs"
    )

    event = models.ForeignKey(
        Event, on_delete=models.RESTRICT, null=False, related_name="jobs"
    )

    location = models.ForeignKey(
        Location,
        on_delete=models.RESTRICT,
        null=False,
        related_name="jobs",
    )

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="jobs",
    )

    rate = models.ForeignKey(
        Rate, on_delete=models.RESTRICT, null=True, related_name="jobs"
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="jobs_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="jobs_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class JobFile(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=100, null=False)
    url = models.CharField(max_length=2048)

    job = models.ForeignKey(
        Job,
        on_delete=models.RESTRICT,
        null=False,
        related_name="job_files",
    )

    file_type = models.ForeignKey(
        FileType,
        on_delete=models.RESTRICT,
        null=False,
        related_name="job_files",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="job_files_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="job_files_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class JobRequirementType(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=100, null=False)

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="job_requirement_types",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="job_requirement_types_creted_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="job_requirement_types_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class JobRequirement(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=100, null=False)

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="job_requirements",
    )

    job_requirement_type = models.ForeignKey(
        JobRequirementType,
        on_delete=models.CASCADE,
        null=False,
        related_name="job_requirements",
    )

    job = models.ForeignKey(
        Job,
        on_delete=models.RESTRICT,
        null=False,
        related_name="job_requirements",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="job_requirement_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="job_requirement_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class JobRequirementFile(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=100, null=False)
    url = models.CharField(max_length=255)

    job_requirement = models.ForeignKey(
        JobRequirement,
        on_delete=models.RESTRICT,
        null=False,
        related_name="job_requirement_files",
    )

    file_type = models.ForeignKey(
        FileType,
        on_delete=models.RESTRICT,
        null=False,
        related_name="job_requirement_files",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="job_requirement_files_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="job_requirement_files_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class AmbassadorJob(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    appear_as_rfp = models.BooleanField(
        default=True
    )  # This bool is for record purposes that it was an invitation.

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassador_jobs",
    )

    ambassador = models.ForeignKey(
        Ambassador,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassador_jobs",
    )

    job = models.ForeignKey(
        Job, on_delete=models.RESTRICT, null=False, related_name="ambassador_jobs"
    )

    status = models.ForeignKey(
        Status, on_delete=models.RESTRICT, null=False, related_name="ambassador_jobs"
    )

    rate = models.ForeignKey(
        Rate, on_delete=models.RESTRICT, null=False, related_name="ambassador_jobs"
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassador_jobs_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="ambassador_jobs_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class CompanyToAmbassadorReview(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    description = models.TextField()
    rate = models.DecimalField(max_digits=14, decimal_places=4)

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="company_to_ambassador_reviews",
    )

    ambassador = models.ForeignKey(
        Ambassador,
        on_delete=models.RESTRICT,
        null=False,
        related_name="company_to_ambassador_reviews",
    )

    job = models.ForeignKey(
        Job,
        on_delete=models.RESTRICT,
        null=False,
        related_name="company_to_ambassador_reviews",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="company_to_ambassador_reviews_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="company_to_ambassador_reviews_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class AmbassadorToAmbassadorReview(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    description = models.TextField()
    rate = models.DecimalField(max_digits=14, decimal_places=4)

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassador_to_ambassador_reviews",
    )

    ambassador = models.ForeignKey(
        Ambassador,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassador_to_ambassador_reviews",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassador_to_ambassador_reviews_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="ambassador_to_ambassador_reviews_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


# Questions
class QuestionType(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=100, null=False)

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="question_types",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="question_types_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="question_types_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class JobRequirementQuestion(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    question = models.TextField()

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="job_requirement_questions",
    )

    job_requirement = models.ForeignKey(
        JobRequirement,
        on_delete=models.RESTRICT,
        null=False,
        related_name="job_requirement_questions",
    )

    question_type = models.ForeignKey(
        QuestionType,
        on_delete=models.RESTRICT,
        null=False,
        related_name="job_requirement_questions",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="job_requirement_questions_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="job_requirement_questions_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class QuestionOption(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    option = models.CharField(max_length=255, null=False)

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="question_options",
    )

    job_requirement_question = models.ForeignKey(
        JobRequirementQuestion,
        on_delete=models.RESTRICT,
        null=False,
        related_name="question_options",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="question_options_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="question_options_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class JobRequirementAnswer(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    selected_answer = ArrayField(
        models.IntegerField(),
        default=list,
    )

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="job_requirement_answers",
    )

    job_requirement_question = models.ForeignKey(
        JobRequirementQuestion,
        on_delete=models.RESTRICT,
        null=False,
        related_name="job_requirement_answers",
    )

    ambassador = models.ForeignKey(
        Ambassador,
        on_delete=models.RESTRICT,
        null=False,
        related_name="job_requirement_answers",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="job_requirement_answers_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="job_requirement_answers_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)
