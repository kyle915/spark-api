from uuid6 import uuid7
from django.db import models
from django.contrib.postgres.fields import ArrayField
from django.conf import settings
from ambassadors.models import FileType, Ambassador
from events.models import Location, Event
from tenants.models import Tenant
from .managers import StatusManager, JobManager, AmbassadorJobManager


UNIT: list[tuple[str, str]] = [
    ("H", "Hour"),
    ("D", "Day"),
    ("W", "Week"),
    ("M", "Months"),
]


def get_job_extension_rate_default():
    return settings.JOB_EXTENSION_RATE_DEFAULT


class Status(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=50, null=True)

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

    objects = StatusManager()

    def save(self, *args, **kwargs):
        if not self.slug:
            from django.utils.text import slugify

            self.slug = slugify(self.name)

        super().save(*args, **kwargs)


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
    # null bc I already have companies with no name xd
    name = models.CharField(max_length=100, null=True, blank=False)
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
    # Lifecycle stages — see Jobs page wireframe.
    #
    #   pending   — autogenerated alongside the Event when an admin
    #               approves a Request. Sits in the "Pending Jobs"
    #               queue waiting for an admin to fill in hours/pay
    #               and click Post.
    #   posted    — admin clicked Post. Job appears on the BA job
    #               board in the mobile app. Gated to favorites by
    #               default, opens to all when admin clicks "Open
    #               to all".
    #   filled    — an ambassador's application was accepted (or
    #               admin manually assigned one). Stops accepting new
    #               applications.
    #   completed — shift wrapped + a recap was filed. Terminal state.
    #   canceled  — admin pulled the job before fill. Terminal.
    STATUS_PENDING = "pending"
    STATUS_POSTED = "posted"
    STATUS_FILLED = "filled"
    STATUS_COMPLETED = "completed"
    STATUS_CANCELED = "canceled"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_POSTED, "Posted"),
        (STATUS_FILLED, "Filled"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_CANCELED, "Canceled"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.TextField(null=True)
    description = models.TextField(null=True)
    code = models.CharField(max_length=100, null=True)
    address = models.CharField(max_length=255)
    start_date = models.DateTimeField(null=True)
    end_date = models.DateTimeField(null=True)
    public = models.BooleanField(default=False)
    closed = models.BooleanField(default=False)
    national = models.BooleanField(default=False)
    ongoing = models.BooleanField(default=False)
    coordinates = ArrayField(models.FloatField(), size=2, null=True)
    extension_rate = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        null=True,
        default=get_job_extension_rate_default,
    )

    # ---- Posting fields (filled by admin when posting) ----
    lifecycle_status = models.CharField(
        max_length=20, choices=STATUS_CHOICES,
        default=STATUS_PENDING, db_index=True,
    )
    total_hours = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True,
    )
    hourly_rate = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True,
    )
    uniform_notes = models.TextField(null=True, blank=True)
    # Gating — when true, only ambassadors in the tenant's
    # FavoriteAmbassador list see this job. Admin can "Open to all".
    favorites_only = models.BooleanField(default=True)
    posted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    max_applications = models.PositiveIntegerField(null=True, blank=True)

    # ---- BA Briefing ----
    # Free-form briefing surfaced to the BA on the mobile job detail
    # screen. Title is short ("Liquid Death · Whole Foods PDX demo"),
    # body is markdown-ish plain text. Attachments (PDFs, decks,
    # images) live on the JobBriefingAttachment model below.
    #
    # `briefing_template` is the optional saved template this briefing
    # was copied from — kept so admins can tell at a glance whether a
    # job is using a stock briefing or got bespoke edits.
    briefing_title = models.CharField(max_length=200, blank=True, default="")
    briefing_body = models.TextField(blank=True, default="")
    briefing_template = models.ForeignKey(
        "jobs.BriefingTemplate",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="jobs_using_template",
    )

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

    # company = models.ForeignKey(
    #     Company, on_delete=models.RESTRICT, null=False, related_name="jobs"
    # )

    event = models.ForeignKey(
        Event, on_delete=models.RESTRICT, null=False, related_name="jobs"
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

    objects = JobManager()


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
    recap_uploaded = models.BooleanField(default=False)
    accepted_terms = models.BooleanField(default=False)
    real_amount = models.DecimalField(max_digits=14, decimal_places=4, null=True)
    time_blocks_15m = models.PositiveIntegerField(default=0)
    appear_as_rfp = models.BooleanField(
        default=True
    )  # This bool is for record purposes that it was an invitation.
    reminder_sent_at = models.DateTimeField(null=True)
    reminder_3h_sent_at = models.DateTimeField(null=True)
    reminder_15m_sent_at = models.DateTimeField(null=True)
    reminder_end_15m_sent_at = models.DateTimeField(null=True)

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

    objects = AmbassadorJobManager()


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


# ---------------------------------------------------------------------
# JobApplication
# ---------------------------------------------------------------------
#
# A BA expresses interest in a posted job. Lifecycle:
#   applied   — BA tapped Apply
#   accepted  — admin assigned the BA (or accepted the application).
#               Once one app is accepted, the parent Job flips to
#               filled and other applications go to declined.
#   declined  — admin rejected the app, or the job filled with someone
#               else.
#   withdrawn — BA pulled their application.
#
# Indexed on (job, status) for the admin "show me applicants" query
# and on (ambassador, status) for the BA's "my applications" screen.
class JobApplication(models.Model):
    STATUS_APPLIED = "applied"
    STATUS_ACCEPTED = "accepted"
    STATUS_DECLINED = "declined"
    STATUS_WITHDRAWN = "withdrawn"
    STATUS_CHOICES = [
        (STATUS_APPLIED, "Applied"),
        (STATUS_ACCEPTED, "Accepted"),
        (STATUS_DECLINED, "Declined"),
        (STATUS_WITHDRAWN, "Withdrawn"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="job_applications",
    )
    job = models.ForeignKey(
        "Job",
        on_delete=models.CASCADE,
        related_name="applications",
    )
    ambassador = models.ForeignKey(
        "ambassadors.Ambassador",
        on_delete=models.CASCADE,
        related_name="job_applications",
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_APPLIED,
        db_index=True,
    )
    # Free-text "Why I'd be great for this" from the BA.
    note = models.TextField(blank=True, default="")
    # When the admin made the accept / decline call.
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="job_applications_decided",
    )
    applied_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # A BA can only apply once per job. Re-applying after withdraw
        # bumps the same row, doesn't insert a duplicate.
        constraints = [
            models.UniqueConstraint(
                fields=["job", "ambassador"], name="uniq_job_ambassador_application",
            ),
        ]
        indexes = [
            models.Index(fields=["job", "status"]),
            models.Index(fields=["ambassador", "status"]),
        ]
        ordering = ("-applied_at",)


# ---------------------------------------------------------------------
# TenantFavoriteAmbassador
# ---------------------------------------------------------------------
#
# Tenant-curated list of BAs that get first-look at favorites_only
# jobs. One row per (tenant, ambassador). Admins manage the list from
# the Favorites tab.
class TenantFavoriteAmbassador(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="favorite_ambassadors",
    )
    ambassador = models.ForeignKey(
        "ambassadors.Ambassador",
        on_delete=models.CASCADE,
        related_name="favorited_by_tenants",
    )
    # Free-text rationale ("Great at sampling demos", "LD-trained")
    note = models.CharField(max_length=255, blank=True, default="")
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="favorite_ambassadors_added",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "ambassador"],
                name="uniq_tenant_favorite_ambassador",
            ),
        ]
        ordering = ("-created_at",)


# ---------------------------------------------------------------------
# BA Briefing Template + Attachments
# ---------------------------------------------------------------------
#
# A reusable per-tenant briefing template (title, body, attachments)
# that admins can "apply to job" — copies the title/body onto Job and
# clones attachments into JobBriefingAttachment rows. Each Job also
# carries its own briefing_title / briefing_body / attachments so the
# briefing can drift from the template (edits don't write back).
#
# Attachments are stored as GCS blob paths (we already have this for
# recap files); the resolved URL is computed at read time so the
# bucket can move without rewriting rows.
class BriefingTemplate(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="briefing_templates",
    )
    name = models.CharField(max_length=120)
    title = models.CharField(max_length=200, blank=True, default="")
    body = models.TextField(blank=True, default="")
    # Soft-delete so a Job that's still pointing at a deleted template
    # doesn't 500. Use the queryset filter `is_archived=False` for the
    # admin picker.
    is_archived = models.BooleanField(default=False, db_index=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="briefing_templates_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="briefing_templates_updated",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name",)


class BriefingTemplateAttachment(models.Model):
    """Attachment row belonging to a reusable BriefingTemplate."""
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    template = models.ForeignKey(
        BriefingTemplate,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    name = models.CharField(max_length=200)
    # GCS object path or external URL. Treated as a URL by the
    # mobile app — we pass through whatever's stored here.
    url = models.CharField(max_length=1024)
    content_type = models.CharField(max_length=120, blank=True, default="")
    size = models.PositiveBigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("id",)


class JobBriefingAttachment(models.Model):
    """Per-job attachment. Cloned from the BriefingTemplate at apply
    time but free to drift after."""
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    job = models.ForeignKey(
        Job,
        on_delete=models.CASCADE,
        related_name="briefing_attachments",
    )
    name = models.CharField(max_length=200)
    url = models.CharField(max_length=1024)
    content_type = models.CharField(max_length=120, blank=True, default="")
    size = models.PositiveBigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("id",)


class AmbassadorJobPreference(models.Model):
    """Per-BA job-board preferences.

    Drives two things:
      1. The marketplace filters' defaults on the mobile job board.
      2. The daily new-gig digest push — who gets nudged about new
         postings, and which gigs count as a "match" for them.

    A BA has at most one row (OneToOne). Rows are created lazily:
    `my_job_preferences` returns sensible defaults when none exists,
    and `update_job_preferences` upserts.
    """

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    ambassador = models.OneToOneField(
        Ambassador,
        on_delete=models.CASCADE,
        related_name="job_preference",
    )

    # Master switch for the daily new-gig digest push. Off = never nudge
    # this BA about new postings (they can still browse the board).
    notify_new_gigs = models.BooleanField(default=True)

    # Preferred US state codes (e.g. ["CA", "TX"]). Empty = all states.
    # Matched against Job.event.state.code in the digest + board filter.
    preferred_state_codes = ArrayField(
        models.CharField(max_length=10),
        default=list,
        blank=True,
    )

    # Minimum hourly rate the BA wants to hear about. Null = no minimum.
    min_hourly_rate = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True,
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("id",)

    def __str__(self) -> str:
        return f"AmbassadorJobPreference(ambassador={self.ambassador_id})"
