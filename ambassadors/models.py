from uuid6 import uuid7

from django.db import models
from django.conf import settings
from django.contrib.postgres.fields import ArrayField

from tenants.models import Tenant
from events.models import Client, Event, Location, TimeZone
from ambassadors.managers import (
    AmbassadorManager,
    AmbassadorInvitationManager,
    AmbassadorReviewManager,
    SkillManager,
    AmbassadorSkillManager,
    AmbassadorGroupManager,
    UserGroupManager,
)
from utils.models import Asyncable


class FileType(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=100, null=False)
    extension = models.CharField(max_length=10, null=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="files_types_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="files_types_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class Ambassador(Asyncable, models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    rating = models.IntegerField(default=0)
    address = models.TextField(null=True)
    phone = models.CharField(max_length=100, null=True)
    about_me = models.TextField(null=True)
    # BA TALENT profile fields (#talent). `bio` is the BA-authored
    # free-text blurb shown on the admin profile pop-up + mobile "You"
    # screen. It coexists with the legacy `about_me`: the BA self-edit
    # mutation keeps both in sync (writes to bio, mirrors to about_me)
    # so older surfaces that still read about_me don't go blank.
    bio = models.TextField(blank=True, default="")
    # School the BA attends / attended. Indexed because the talent
    # search filters on it (college=__icontains) for campus staffing.
    college = models.CharField(max_length=255, blank=True, default="", db_index=True)
    # "Currently attending" — the searchable flag admins use to pull
    # the in-college student pool for campus activations.
    in_college = models.BooleanField(default=False)
    # GCS blob PATH (not a signed URL) for the BA's headshot — single,
    # uploaded via the getUploadUrl→PUT flow then stored here. Served
    # via utils.gcs.public_url. Mirrors documents.AmbassadorDocument.file.
    headshot = models.CharField(max_length=1024, blank=True, default="")
    # GCS blob PATH for the BA's résumé (PDF), single. Same convention.
    resume = models.CharField(max_length=1024, blank=True, default="")
    coordinates = ArrayField(
        models.FloatField(),
        size=2,
        default=list,
    )
    is_active = models.BooleanField(default=False)
    location = models.ForeignKey(
        Location, on_delete=models.RESTRICT, null=True, related_name="ambassador"
    )
    t_shirt_size = models.CharField(max_length=100, null=True)

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassador",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassadors_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="ambassadors_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = AmbassadorManager()

    class Meta:
        indexes = [
            models.Index(fields=["is_active"]),
            models.Index(fields=["user", "is_active"]),
        ]


class AmbassadorInvitation(Asyncable, models.Model):
    """Model to track ambassador invitations sent by clients or spark-admins."""

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    email = models.EmailField(null=False)
    token = models.CharField(max_length=255, unique=True, null=False)
    expires_at = models.DateTimeField(null=False)
    is_used = models.BooleanField(default=False)
    used_at = models.DateTimeField(null=True)

    # Who created the invitation (client or spark-admin)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassador_invitations_sent",
    )

    # Tenant for which the ambassador is being invited
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassador_invitations",
    )

    # The ambassador created from this invitation (if used)
    ambassador = models.ForeignKey(
        "Ambassador",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invitation",
    )
    # If the user is invited to a specific job, we need to ensure that.
    job = models.ForeignKey(
        "jobs.Job",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invitations",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassador_invitations_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="ambassador_invitations_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = AmbassadorInvitationManager()

    class Meta:
        indexes = [
            models.Index(fields=["email", "is_used"]),
            models.Index(fields=["email", "is_used", "expires_at"]),
            models.Index(fields=["token"]),
            models.Index(fields=["expires_at"]),
        ]

    @property
    def accept_url(self):
        return f"{settings.AMBASSADOR_FRONTEND_URL}/invitations/?token={self.token}"

    def is_usable(self, raise_exception: bool = False):
        from django.utils import timezone

        now = timezone.now()
        message: str = ""
        if self.expires_at <= now:
            message = "This invitation has expired."
        if self.is_used:
            message = "This invitation has already been used."

        if raise_exception and message:
            raise ValueError(message)
        return message == ""


class AmbassadorReview(Asyncable, models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    review = models.TextField(null=True)
    score = models.IntegerField(null=True)

    ambassador = models.ForeignKey(
        Ambassador,
        on_delete=models.RESTRICT,
        null=True,
        related_name="ambassadors_reviews",
    )

    client = models.ForeignKey(
        Client,
        on_delete=models.RESTRICT,
        null=True,
        related_name="ambassadors_reviews",
    )

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=True,
        related_name="ambassadors_reviews",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassadors_reviews_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="ambassadors_reviews_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = AmbassadorReviewManager()

    class Meta:
        verbose_name = "Ambassador Review"
        verbose_name_plural = "Ambassador Reviews"
        unique_together = ("ambassador", "client")


class AmbassadorEvent(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    is_approved = models.BooleanField(default=False)

    ambassador = models.ForeignKey(
        Ambassador,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassadors_events",
    )
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassadors_events",
    )
    event = models.ForeignKey(
        Event,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassadors_events",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassadors_events_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="ambassadors_events_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class AmbassadorFile(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=100, null=False)
    url = models.CharField(max_length=2048, null=True)
    main_resume = models.BooleanField(default=False)
    profile_pic = models.BooleanField(default=False)
    is_public = models.BooleanField(default=False)

    file_type = models.ForeignKey(
        FileType,
        on_delete=models.RESTRICT,
        null=True,
        related_name="ambassadors_files",
    )
    ambassador = models.ForeignKey(
        Ambassador,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassadors_files",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassadors_files_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="ambassadors_files_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class AmbassadorPhoto(models.Model):
    """An event/work photo a BA adds to their TALENT profile gallery.

    Multiple per ambassador (unlike the single `Ambassador.headshot`).
    `image` stores the GCS blob PATH — same getUploadUrl→PUT convention
    as the headshot/résumé and documents.AmbassadorDocument.file —
    served to admins/clients via utils.gcs.public_url. Uploaded by the
    BA from the mobile "You" screen; shown in the web profile pop-up
    gallery.
    """

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    ambassador = models.ForeignKey(
        Ambassador,
        on_delete=models.CASCADE,
        null=False,
        related_name="photos",
    )
    # GCS blob path (not a signed URL).
    image = models.CharField(max_length=1024, null=False)
    caption = models.CharField(max_length=255, blank=True, default="")

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="ambassador_photos_created_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["ambassador", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"photo {self.ambassador_id} {self.image[:32]}"


class AmbassadorTrait(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    ambassador = models.ForeignKey(
        Ambassador,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassador_traits",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassador_traits",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassadors_traits_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="ambassadors_traits_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class Skill(Asyncable, models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=50)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="skill_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="skill_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SkillManager()


class AmbassadorSkill(Asyncable, models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    ambassador = models.ForeignKey(
        Ambassador,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassadors_skills",
    )
    skill = models.ForeignKey(
        Skill,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassadors_skills",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassadors_skills_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="ambassadors_skills_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = AmbassadorSkillManager()

    class Meta:
        unique_together = ("ambassador", "skill")


class AmbassadorNote(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    note = models.TextField(null=False)

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassadors_notes",
    )
    ambassador = models.ForeignKey(
        Ambassador,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassadors_notes",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassadors_notes_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="ambassadors_notes_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class AmbassadorWorkHistory(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    ambassador = models.ForeignKey(
        Ambassador,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassador_work_histories",
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassador_work_histories_user",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassador_work_histories_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="ambandassador_work_histories_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class AmbassadorEducation(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    ambassador = models.ForeignKey(
        Ambassador,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassador_educations",
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambasador_educations_user",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassador_educations_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="ambasador_educations_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class People(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=100, null=False)

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="people",
    )
    ambassador = models.ForeignKey(
        Ambassador,
        on_delete=models.RESTRICT,
        null=False,
        related_name="people",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="people_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="people_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class EventJob(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=50)
    description = models.TextField(null=True)
    rate = models.DecimalField(max_digits=14, decimal_places=4)
    code = models.CharField(max_length=100)
    address = models.CharField(max_length=255)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)

    location = models.ForeignKey(
        Location, on_delete=models.RESTRICT, null=False, related_name="event_jobs"
    )

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="event_jobs",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="event_jobs_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="event_jobs_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class PeopleJob(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    confirmed = models.BooleanField(default=False)

    people = models.ForeignKey(
        People,
        on_delete=models.RESTRICT,
        null=False,
        related_name="people_jobs",
    )
    event_job = models.ForeignKey(
        EventJob, on_delete=models.RESTRICT, null=False, related_name="people_jobs"
    )

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="people_jobs",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="people_jobs_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="people_jobs_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class Review(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    description = models.TextField(null=True)
    rate = models.DecimalField(max_digits=14, decimal_places=4)

    people = models.ForeignKey(
        People,
        on_delete=models.RESTRICT,
        null=False,
        related_name="reviews",
    )

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="reviews",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="reviews_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="reviews_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class AttendanceType(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=50, null=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="attendance_types_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="attendance_types_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class AttendanceStatus(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=50, null=True)

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=True,
        related_name="attendance_status",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="attendance_status_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="attendance_status_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class Source(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=255)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="source_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="source_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class Attendance(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    clock_time = models.DateTimeField()
    coordinates = ArrayField(models.FloatField(), size=2, null=True)

    ambassador = models.ForeignKey(
        Ambassador, on_delete=models.RESTRICT, null=True, related_name="attendance"
    )

    job = models.ForeignKey(
        "jobs.Job", on_delete=models.RESTRICT, null=True, related_name="attendance"
    )

    event = models.ForeignKey(
        Event, on_delete=models.RESTRICT, null=True, related_name="attendance"
    )

    attendace_type = models.ForeignKey(
        AttendanceType, on_delete=models.RESTRICT, null=True, related_name="attendance"
    )

    attendance_status = models.ForeignKey(
        AttendanceStatus,
        on_delete=models.RESTRICT,
        null=True,
        related_name="attendance",
    )

    source = models.ForeignKey(
        Source,
        on_delete=models.RESTRICT,
        null=True,
        related_name="attendance",
    )

    timezone = models.ForeignKey(
        TimeZone,
        on_delete=models.RESTRICT,
        null=True,
        related_name="attendance",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="attendance_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="attendance_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


# Models Related to the Ambassador Groups
class GroupType(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=255)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="group_types_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="group_types_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class AmbassadorGroup(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=255)
    description = models.TextField(null=True)
    private = models.BooleanField(default=False)
    group_type = models.ForeignKey(
        GroupType,
        on_delete=models.RESTRICT,
        null=False,
        related_name="groups",
    )

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="groups",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="groups_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="groups_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = AmbassadorGroupManager()

    def __str__(self):
        return self.name


class AmbassadorGroupJob(models.Model):
    """Pivot table to store explicit group-job assignment."""

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    group = models.ForeignKey(
        AmbassadorGroup,
        on_delete=models.RESTRICT,
        null=False,
        related_name="job_links",
    )
    job = models.ForeignKey(
        "jobs.Job",
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassador_group_links",
    )
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassador_group_links",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassador_group_jobs_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="ambassador_group_jobs_updated_by",
    )
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["group", "job"],
                name="unique_ambassador_group_job",
            )
        ]


class UserGroup(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="user_groups",
    )
    group = models.ForeignKey(
        AmbassadorGroup,
        on_delete=models.RESTRICT,
        null=False,
        related_name="members",
    )
    # ambassador is optional because it will be set when the user accepts the invite.
    ambassador = models.ForeignKey(
        Ambassador,
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="ambassador_groups",
    )

    objects = UserGroupManager()

    def __str__(self):
        return f"{self.user.username} - {self.group.name}"


class PushDevice(models.Model):
    """A registered Expo push token for a user device.

    A user may have many devices (phone + tablet, multiple installs). The
    token is unique per install — re-registering with the same token is
    idempotent (we just bump `updated_at` + the device metadata).

    `is_active` is flipped off when the Expo push relay tells us the
    token is invalid (DeviceNotRegistered receipt). The mobile client
    overwrites the row by re-registering on next launch.
    """

    PLATFORM_CHOICES = (
        ("ios", "iOS"),
        ("android", "Android"),
        ("web", "Web"),
    )

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=False,
        related_name="push_devices",
    )
    token = models.CharField(max_length=255, unique=True, db_index=True)
    platform = models.CharField(max_length=20, choices=PLATFORM_CHOICES)
    device_name = models.CharField(max_length=255, null=True, blank=True)
    app_version = models.CharField(max_length=40, null=True, blank=True)

    is_active = models.BooleanField(default=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "is_active"]),
        ]

    def __str__(self):
        return f"{self.user_id}:{self.platform}:{self.token[:12]}…"


class LocationPing(models.Model):
    """A single GPS reading from a BA's mobile app during an active shift.

    The spark-mobile activation tracker pings here every 2 minutes (or
    on 50m movement, whichever comes first) while the BA is inside the
    activation window — from clock-in through clock-out + 15 min grace.

    Used by the web admin "Today, on the ground" map to render live BA
    pins, and by ops to retro-audit whether a BA was actually on-site
    during the shift.
    """

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    ambassador = models.ForeignKey(
        Ambassador,
        on_delete=models.CASCADE,
        null=False,
        related_name="location_pings",
    )
    event = models.ForeignKey(
        Event,
        on_delete=models.CASCADE,
        null=False,
        related_name="location_pings",
    )

    lat = models.FloatField()
    lng = models.FloatField()
    accuracy_meters = models.FloatField(null=True, blank=True)
    # ISO-ish timestamp from the device when the GPS reading was taken.
    # Separate from created_at (server clock) so we can compute
    # "freshness" without trusting the server time.
    recorded_at = models.DateTimeField(db_index=True)

    SOURCE_CHOICES = (
        ("foreground", "Foreground"),
        ("background", "Background"),
        ("clock_in", "Clock-in"),
        ("clock_out", "Clock-out"),
    )
    source = models.CharField(
        max_length=20, choices=SOURCE_CHOICES, default="background"
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)

    class Meta:
        indexes = [
            # "latest ping per BA in the last N minutes" is the hot query.
            models.Index(fields=["ambassador", "-recorded_at"]),
            models.Index(fields=["event", "-recorded_at"]),
        ]

    def __str__(self):
        return f"ping {self.ambassador_id}@{self.event_id} {self.recorded_at}"


class AmbassadorRating(models.Model):
    """A 1-5 star rating (with optional comment) for a BA's work on a gig.

    Both Ignite admins and client users can rate. The `by_client` flag
    is captured at create time from the rater's role and drives
    visibility: client-authored ratings are surfaced only to Ignite
    (admins see everything; a client sees only the ratings they wrote).
    The `rateAmbassador` mutation recomputes the rounded mean of *all*
    ratings into the denormalized `Ambassador.rating` after each write.

    `event` is the gig the rating is about. Nullable so a general,
    non-gig rating from the BA detail page is still possible; the
    unique constraint keeps one rating per (ambassador, event, rater)
    so re-rating updates rather than stacks.
    """

    SCORE_MIN = 1
    SCORE_MAX = 5

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    ambassador = models.ForeignKey(
        Ambassador,
        on_delete=models.CASCADE,
        null=False,
        related_name="ratings",
    )
    event = models.ForeignKey(
        Event,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ambassador_ratings",
    )
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassador_ratings",
    )

    score = models.IntegerField()  # 1-5
    comment = models.TextField(null=True, blank=True)
    # True when the rater is a client (vs Ignite admin). Set at create
    # time; never trust the client to flip it.
    by_client = models.BooleanField(default=False)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=False,
        related_name="ambassador_ratings_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True,
        related_name="ambassador_ratings_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # One rating per rater per ambassador per gig. (Two NULL
            # events from the same rater are allowed by Postgres, which
            # is fine — a rater can leave at most one gig-less rating
            # in practice and the UI upserts.)
            models.UniqueConstraint(
                fields=["ambassador", "event", "created_by"],
                name="uniq_ambassador_rating_per_gig_per_rater",
            ),
        ]
        indexes = [
            models.Index(fields=["ambassador", "-created_at"]),
            models.Index(fields=["event"]),
            models.Index(fields=["tenant", "by_client"]),
        ]

    def __str__(self):
        return f"rating {self.score}★ ba={self.ambassador_id} ev={self.event_id}"


class ShiftExtensionRequest(models.Model):
    """A BA's in-shift request for more activation time.

    Created from the mobile app mid-shift (`requestExtension`). On
    create we push + email the assigned RMM and Spark admins so they can
    approve/deny; the mobile app reads `status` / `approved_minutes`
    back. Tenant is reached via `event.tenant` (no denormalized FK).
    """

    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_DENIED = "denied"

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    event = models.ForeignKey(
        Event,
        on_delete=models.CASCADE,
        null=True,
        related_name="extension_requests",
    )
    ambassador = models.ForeignKey(
        Ambassador,
        on_delete=models.CASCADE,
        null=True,
        related_name="extension_requests",
    )

    minutes_requested = models.PositiveIntegerField(default=0)
    reason = models.TextField(blank=True, default="")
    status = models.CharField(max_length=16, default=STATUS_PENDING)
    approved_minutes = models.PositiveIntegerField(null=True, blank=True)
    # When the BA hit "send" on their device (may differ from created_at
    # if the request was queued offline).
    requested_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="extension_requests_created_by",
    )
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["event", "status"]),
            models.Index(fields=["status", "-created_at"]),
        ]

    def __str__(self):
        return (
            f"extension {self.minutes_requested}min "
            f"ba={self.ambassador_id} ev={self.event_id} [{self.status}]"
        )
