import strawberry
from strawberry import relay
from graphql import GraphQLError
from asgiref.sync import sync_to_async
from typing import Any
from decimal import Decimal
import logging

from django.contrib.auth import get_user_model
from django.db.models import Model, Prefetch, Q
from django.db import transaction
from django.utils import timezone
from django.conf import settings
from django.utils.text import slugify

from recaps import types
from recaps import models
from recaps import inputs
from recaps.envelopes import RecapApprovedNotificationMailer
from recaps.queries import RecapQueriesService
from ambassadors.models import FileType, Ambassador, Attendance
from events.models import Event, Retailer, Location, State, TimeZone, EventType
from jobs.models import Job, AmbassadorJob
from tenants.models import Role, TenantedUser, Tenant
from utils.graphql.inputs import SparkGraphQLInput
from utils.graphql.permissions import StrictIsAuthenticated
from utils.graphql.relay import ensure_relay_mutation
from utils.graphql.mixins import SparkGraphQLMixin, resolve_id_to_int
from utils.utils import build_mutation_response
from utils.gcs import (
    extract_blob_name_from_url,
    delete_blob,
    upload_bytes,
    download_blob_bytes,
    generate_download_url,
    get_gcs_client,
)
from utils.onesignal import OneSignalError, one_signal_client
from recaps.pdf import build_recap_pdf, should_embed_recap_file, is_image_bytes
from recaps.excel import build_recaps_xlsx

ensure_relay_mutation()

User = get_user_model()
logger = logging.getLogger(__name__)


async def _notify_recap_approved_to_rmm_or_clients(recap: models.Recap) -> None:
    event = recap.event
    rmm_user = getattr(event, "rmm_asigned", None)
    fallback_reply_to = "events@igniteproductions.co"
    reply_to_email = (
        (getattr(rmm_user, "email", None) or "").strip() or fallback_reply_to
    )

    recipients: list[tuple[str, str]] = []
    if rmm_user and rmm_user.email:
        recipients.append(
            (
                rmm_user.email.strip(),
                (rmm_user.first_name or "").strip(),
            )
        )
    else:
        rows = await sync_to_async(list)(
            TenantedUser.objects.filter(
                tenant_id=event.tenant_id,
                is_active=True,
                user__role__slug=Role.CLIENT_SLUG,
            ).values("user__email", "user__first_name")
        )
        for row in rows:
            email = (row.get("user__email") or "").strip()
            if not email:
                continue
            recipients.append((email, (row.get("user__first_name") or "").strip()))

    if not recipients:
        return

    for email, first_name in recipients:
        mailer = RecapApprovedNotificationMailer(
            recap=recap,
            to_emails=[email],
            recipient_first_name=first_name or None,
            reply_to_email=reply_to_email,
        )
        await sync_to_async(mailer.send)()


async def _notify_recap_approved_to_ambassador_by_push(
    recap: models.Recap,
) -> None:
    ambassador = getattr(recap, "ambassador", None)
    user = getattr(ambassador, "user", None)
    if not user:
        return

    deep_link = f"spark://app/tabs/recaps/{recap.id}"

    try:
        await one_signal_client.send_push(
            external_ids=[str(user.uuid)],
            title="Recap approved",
            message=f"Your recap for {recap.name} was approved.",
            url=deep_link,
            data={
                "type": "recap_approved",
                "recap_id": str(recap.id),
                "deep_link": deep_link,
            },
        )
    except OneSignalError as exc:
        logger.warning(
            "Failed to send OneSignal recap approval push for recap=%s: %s",
            recap.id,
            exc,
        )


class RecapMutationService(SparkGraphQLMixin):
    """Service for recap mutations."""

    input: SparkGraphQLInput | None = None
    info: strawberry.Info | None = None
    user: User | None = None

    @classmethod
    def with_input(cls, input: SparkGraphQLInput) -> "RecapMutationService":
        """Create a new instance of the service with the input."""
        service = cls()
        service.set_input(input)
        return service

    def set_input(self, input: SparkGraphQLInput) -> "RecapMutationService":
        """Set the input for the service."""
        self.input = input
        return self

    async def set_user(self, info: strawberry.Info) -> "RecapMutationService":
        """Set the user for the service."""
        self.info = info
        self.user = await self.get_user(info)
        return self

    @staticmethod
    def _has_complete_consumer_engagements(
        consumer_engagements: inputs.ConsumerEngagementsInput | None,
    ) -> bool:
        if consumer_engagements is None:
            return False
        return all(
            value is not None
            for value in (
                consumer_engagements.total_consumer,
                consumer_engagements.first_time_consumers,
                consumer_engagements.brand_aware_consumers,
                consumer_engagements.willing_to_purchase_consumers,
                consumer_engagements.not_willing_consumers,
            )
        )

    @staticmethod
    def _has_any_consumer_engagements(
        consumer_engagements: inputs.ConsumerEngagementsInput | None,
    ) -> bool:
        if consumer_engagements is None:
            return False
        return any(
            value is not None
            for value in (
                consumer_engagements.total_consumer,
                consumer_engagements.first_time_consumers,
                consumer_engagements.brand_aware_consumers,
                consumer_engagements.willing_to_purchase_consumers,
                consumer_engagements.not_willing_consumers,
            )
        )

    @staticmethod
    def _has_complete_product_sample(
        product_sample: inputs.ProductSampleInput | None,
    ) -> bool:
        if product_sample is None:
            return False
        return product_sample.product_id not in (None, "") and product_sample.quantity is not None

    @classmethod
    def _has_complete_product_samples(
        cls,
        product_samples: list[inputs.ProductSampleInput] | None,
    ) -> bool:
        return bool(
            product_samples
            and any(cls._has_complete_product_sample(sample) for sample in product_samples)
        )

    @staticmethod
    def _has_complete_sales_performance(
        sale: inputs.SalesPerformanceInput | None,
    ) -> bool:
        if sale is None:
            return False
        return (
            sale.product_id not in (None, "")
            and sale.type_of_good_id not in (None, "")
            and sale.price is not None
        )

    @classmethod
    def _has_complete_sales_performance_items(
        cls,
        sales_performance: list[inputs.SalesPerformanceInput] | None,
    ) -> bool:
        return bool(
            sales_performance
            and any(cls._has_complete_sales_performance(sale) for sale in sales_performance)
        )

    def _is_recap_fully_completed(self) -> bool:
        """Validate that recap input includes all optional sections and files."""
        if not isinstance(self.input, inputs.CreateRecapInput):
            return False
        if self.input.incomplete is True:
            return False

        has_files = bool(self.input.files and len(self.input.files) > 0)
        has_metrics = all(
            value is not None
            for value in (
                self.input.products_sold,
                self.input.total_cans_sold,
                self.input.total_packs_sold,
                self.input.total_earnings,
                self.input.account_spend_amount,
            )
        )
        has_consumer_engagements = self._has_complete_consumer_engagements(
            self.input.consumer_engagements
        )
        has_product_samples = self._has_complete_product_samples(
            self.input.product_samples
        )
        has_sales_performance = self._has_complete_sales_performance_items(
            self.input.sales_performance
        )
        has_consumer_feedback = self.input.consumer_feedback is not None and all(
            bool((value or "").strip())
            for value in (
                self.input.consumer_feedback.demographics,
                self.input.consumer_feedback.feedback,
                self.input.consumer_feedback.quotes,
                self.input.consumer_feedback.positive_stories,
                self.input.consumer_feedback.reasons_to_decline,
            )
        )
        has_account_feedback = self.input.account_feedback is not None and all(
            bool((value or "").strip())
            for value in (
                self.input.account_feedback.do_differently_feedback,
                self.input.account_feedback.feedback,
                self.input.account_feedback.corpo_card,
            )
        )

        return all(
            (
                has_files,
                has_metrics,
                has_consumer_engagements,
                has_product_samples,
                has_sales_performance,
                has_consumer_feedback,
                has_account_feedback,
            )
        )

    @staticmethod
    def _normalize_attendance_slug(slug: str | None) -> str:
        return (slug or "").strip().lower().replace("-", "_")

    async def _apply_time_based_recap_payment_rule(
        self,
        *,
        event: Event,
        job: Job | None,
        ambassador: Ambassador | None,
    ) -> None:
        """
        Rule:
        - If recap is 100% complete:
          - worked time <= 49% of event duration: set real_amount to 40% of rate.
          - worked time >= 50% of event duration: set real_amount to 65% of rate.
        - If recap is incomplete and worked time is 100% (or more):
          - set real_amount to 85% of rate.
        """
        if not ambassador or not job:
            return

        is_full_recap = self._is_recap_fully_completed()

        if not event.start_time or not event.end_time or event.end_time <= event.start_time:
            return

        attendance_filters = Q(ambassador=ambassador, event=event)
        if job:
            attendance_filters &= Q(job=job)

        attendances = await sync_to_async(list)(
            Attendance.objects.select_related("attendace_type")
            .filter(attendance_filters)
            .order_by("clock_time")
        )
        if not attendances:
            return

        clock_in_times = [
            record.clock_time
            for record in attendances
            if self._normalize_attendance_slug(getattr(record.attendace_type, "slug", None))
            == "clock_in"
        ]
        clock_out_times = [
            record.clock_time
            for record in attendances
            if self._normalize_attendance_slug(getattr(record.attendace_type, "slug", None))
            == "clock_out"
        ]

        event_minutes = int((event.end_time - event.start_time).total_seconds() // 60)
        if event_minutes <= 0:
            return

        # Business rule: no clock-in means 0% worked time.
        if not clock_in_times:
            worked_percentage = 0
        elif not clock_out_times:
            worked_percentage = 0
        else:
            worked_minutes = int(
                (max(clock_out_times) - min(clock_in_times)).total_seconds() // 60
            )
            if worked_minutes <= 0:
                worked_percentage = 0
            else:
                worked_ratio = worked_minutes / event_minutes
                worked_percentage = worked_ratio * 100

        ambassador_job = await sync_to_async(
            lambda: AmbassadorJob.objects.select_related("rate")
            .filter(ambassador=ambassador, job=job)
            .order_by("-created_at")
            .first()
        )()
        if not ambassador_job or not ambassador_job.rate or ambassador_job.rate.amount is None:
            return

        if is_full_recap:
            if worked_percentage >= 100:
                ambassador_job.real_amount = ambassador_job.rate.amount
            elif worked_percentage <= 49:
                ambassador_job.real_amount = ambassador_job.rate.amount * Decimal("0.40")
            elif worked_percentage >= 50:
                ambassador_job.real_amount = ambassador_job.rate.amount * Decimal("0.65")
            else:
                return
        elif worked_percentage >= 100:
            ambassador_job.real_amount = ambassador_job.rate.amount * Decimal("0.85")
        else:
            return

        await sync_to_async(ambassador_job.save)(update_fields=["real_amount", "updated_at"])

    async def create_recap(self) -> models.Recap:
        """Create a recap with multiple files."""
        if not isinstance(self.input, inputs.CreateRecapInput):
            raise GraphQLError("Invalid input type.")

        # Validate event exists
        try:
            event_id = resolve_id_to_int(self.input.event_id)
            event = await sync_to_async(Event.objects.get)(id=event_id)
        except (Event.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Event not found.")

        job = None
        if self.input.job_id:
            try:
                job_id = resolve_id_to_int(self.input.job_id)
                job = await sync_to_async(Job.objects.get)(id=job_id)
            except (Job.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Job not found.")

        retailer = None
        if self.input.retailer_id:
            try:
                retailer_id = resolve_id_to_int(self.input.retailer_id)
                retailer = await sync_to_async(Retailer.objects.get)(id=retailer_id)
            except (Retailer.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Retailer not found.")

        ambassador = None
        if self.input.ambassador_id:
            try:
                ambassador_id = resolve_id_to_int(self.input.ambassador_id)
                ambassador = await sync_to_async(Ambassador.objects.get)(
                    id=ambassador_id
                )
            except (Ambassador.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Ambassador not found.")

        location = None
        if self.input.location_id:
            try:
                location_id = resolve_id_to_int(self.input.location_id)
                location = await sync_to_async(Location.objects.get)(id=location_id)
            except (Location.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Location not found.")

        state = None
        if self.input.state_id:
            try:
                state_id = resolve_id_to_int(self.input.state_id)
                state = await sync_to_async(State.objects.get)(id=state_id)
            except (State.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("State not found.")

        if not self.input.files or len(self.input.files) == 0:
            raise GraphQLError("At least one file is required.")

        # Use transaction to ensure atomicity
        @sync_to_async
        def create_recap_with_files():
            with transaction.atomic():
                # Create RecapFile instances for each file
                recap_files = []
                for file_input in self.input.files:
                    file_url = file_input.file
                    # Extract blob name from GCS URL
                    blob_name = extract_blob_name_from_url(file_url)
                    if not blob_name:
                        raise GraphQLError("Invalid recap file path.")

                    file_type = None
                    if file_input.file_type_id not in (None, ""):
                        try:
                            file_type_id = resolve_id_to_int(file_input.file_type_id)
                            file_type = FileType.objects.get(id=file_type_id)
                        except (FileType.DoesNotExist, TypeError, ValueError, GraphQLError):
                            raise GraphQLError("File type not found.")

                    # Get default file type (you may want to make this configurable)
                    if not file_type:
                        file_type = FileType.objects.first()
                    if not file_type:
                        raise GraphQLError(
                            "No file type available. Please create a file type first."
                        )

                    file_recap_category = None
                    if file_input.file_recap_category_id not in (None, ""):
                        try:
                            category_id = resolve_id_to_int(
                                file_input.file_recap_category_id
                            )
                            file_recap_category = models.FileRecapCategory.objects.get(
                                id=category_id
                            )
                        except (
                            models.FileRecapCategory.DoesNotExist,
                            TypeError,
                            ValueError,
                            GraphQLError,
                        ):
                            raise GraphQLError("File recap category not found.")

                    recap_file = models.RecapFile(
                        name=f"Recap file for {self.input.name}",
                        file=blob_name,
                        file_type=file_type,
                        file_recap_category=file_recap_category,
                        approved=False,
                        created_by=self.user,
                    )
                    recap_file.save()
                    recap_files.append(recap_file)

                # Create the Recap instance
                total_engagements = None
                if self.input.consumer_engagements is not None:
                    total_engagements = self.input.consumer_engagements.total_consumer

                recap = models.Recap(
                    name=self.input.name,
                    event=event,
                    created_by=self.user,
                    total_engagements=total_engagements,
                    products_sold=self.input.products_sold,
                    total_cans_sold=self.input.total_cans_sold,
                    total_packs_sold=self.input.total_packs_sold,
                    total_earnings=self.input.total_earnings,
                    account_spend_amount=self.input.account_spend_amount,
                    traffic_description=self.input.traffic_description,
                    competitive_presence=self.input.competitive_presence,
                    job=job,
                    retailer=retailer,
                    ambassador=ambassador,
                    location=location,
                    state=state,
                )
                if self.input.filling_for_ambassador is not None:
                    recap.filling_for_ambassador = self.input.filling_for_ambassador
                if self.input.late is not None:
                    recap.late = self.input.late
                if self.input.incomplete is not None:
                    recap.incomplete = self.input.incomplete
                recap.save()

                # Link recap to all recap files
                models.RecapFile.objects.filter(
                    id__in=[recap_file.id for recap_file in recap_files]
                ).update(recap=recap)

                # Create related objects
                if self._has_any_consumer_engagements(self.input.consumer_engagements):
                    models.ConsumerEngagements.objects.create(
                        recap=recap,
                        created_by=self.user,
                        total_consumer=self.input.consumer_engagements.total_consumer,
                        first_time_consumers=self.input.consumer_engagements.first_time_consumers,
                        brand_aware_consumers=self.input.consumer_engagements.brand_aware_consumers,
                        willing_to_purchase_consumers=self.input.consumer_engagements.willing_to_purchase_consumers,
                        not_willing_consumers=self.input.consumer_engagements.not_willing_consumers,
                    )

                if self.input.product_samples:
                    for sample in self.input.product_samples:
                        if not self._has_complete_product_sample(sample):
                            continue
                        try:
                            product_id = resolve_id_to_int(sample.product_id)
                            models.ProductSamples.objects.create(
                                recap=recap,
                                created_by=self.user,
                                product_id=product_id,
                                quantity=sample.quantity,
                            )
                        except (TypeError, ValueError, GraphQLError):
                            raise GraphQLError(
                                f"Invalid product ID: {sample.product_id}"
                            )

                if self.input.sales_performance:
                    for sale in self.input.sales_performance:
                        if not self._has_complete_sales_performance(sale):
                            continue
                        try:
                            product_id = resolve_id_to_int(sale.product_id)
                            type_of_good_id = resolve_id_to_int(sale.type_of_good_id)
                            models.SalesPerformance.objects.create(
                                recap=recap,
                                created_by=self.user,
                                product_id=product_id,
                                type_of_good_id=type_of_good_id,
                                price=sale.price,
                            )
                        except (TypeError, ValueError, GraphQLError):
                            raise GraphQLError(f"Invalid product or type of good ID")

                if self.input.consumer_feedback:
                    models.ConsumerFeedback.objects.create(
                        recap=recap,
                        created_by=self.user,
                        demographics=self.input.consumer_feedback.demographics,
                        feedback=self.input.consumer_feedback.feedback,
                        quotes=self.input.consumer_feedback.quotes,
                        positive_stories=self.input.consumer_feedback.positive_stories,
                        reasons_to_decline=self.input.consumer_feedback.reasons_to_decline,
                    )

                if self.input.account_feedback:
                    models.AccountFeedback.objects.create(
                        recap=recap,
                        created_by=self.user,
                        do_differently_feedback=self.input.account_feedback.do_differently_feedback,
                        feedback=self.input.account_feedback.feedback,
                        corpo_card=self.input.account_feedback.corpo_card,
                        was_corpo_card_used=bool(
                            self.input.account_feedback.was_corpo_card_used
                        ),
                    )

                return recap

        recap = await create_recap_with_files()
        await self._apply_time_based_recap_payment_rule(
            event=event,
            job=job,
            ambassador=ambassador,
        )
        return recap

    async def update_recap(self) -> models.Recap:
        """Update a recap."""
        if not isinstance(self.input, inputs.UpdateRecapInput):
            raise GraphQLError("Invalid input type.")

        try:
            recap_id = resolve_id_to_int(self.input.id)
            recap = await sync_to_async(models.Recap.objects.get)(id=recap_id)
        except (models.Recap.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Recap not found.")

        # Validate event exists
        try:
            event_id = resolve_id_to_int(self.input.event_id)
            event = await sync_to_async(Event.objects.get)(id=event_id)
        except (Event.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Event not found.")

        job = None
        if self.input.job_id:
            try:
                job_id = resolve_id_to_int(self.input.job_id)
                job = await sync_to_async(Job.objects.get)(id=job_id)
            except (Job.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Job not found.")

        retailer = None
        if self.input.retailer_id:
            try:
                retailer_id = resolve_id_to_int(self.input.retailer_id)
                retailer = await sync_to_async(Retailer.objects.get)(id=retailer_id)
            except (Retailer.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Retailer not found.")

        ambassador = None
        if self.input.ambassador_id:
            try:
                ambassador_id = resolve_id_to_int(self.input.ambassador_id)
                ambassador = await sync_to_async(Ambassador.objects.get)(
                    id=ambassador_id
                )
            except (Ambassador.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Ambassador not found.")

        location = None
        if self.input.location_id:
            try:
                location_id = resolve_id_to_int(self.input.location_id)
                location = await sync_to_async(Location.objects.get)(id=location_id)
            except (Location.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Location not found.")

        state = None
        if self.input.state_id:
            try:
                state_id = resolve_id_to_int(self.input.state_id)
                state = await sync_to_async(State.objects.get)(id=state_id)
            except (State.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("State not found.")

        if not self.input.files or len(self.input.files) == 0:
            raise GraphQLError("At least one file is required.")

        @sync_to_async
        def update_recap_with_files():
            with transaction.atomic():
                existing_files = list(
                    models.RecapFile.objects.filter(recap=recap).distinct()
                )
                blob_to_file = {
                    extract_blob_name_from_url(str(file.file)): file
                    for file in existing_files
                    if extract_blob_name_from_url(str(file.file))
                }

                final_files: list[models.RecapFile] = []
                for file_input in self.input.files:
                    file_url = file_input.file
                    blob_name = extract_blob_name_from_url(file_url)
                    if not blob_name:
                        raise GraphQLError("Invalid recap file path.")

                    if blob_name in blob_to_file:
                        # Reuse existing file; mark as kept by popping
                        existing_file = blob_to_file.pop(blob_name)
                        updated_fields = []
                        if file_input.file_type_id not in (None, ""):
                            try:
                                file_type_id = resolve_id_to_int(file_input.file_type_id)
                                file_type = FileType.objects.get(id=file_type_id)
                            except (
                                FileType.DoesNotExist,
                                TypeError,
                                ValueError,
                                GraphQLError,
                            ):
                                raise GraphQLError("File type not found.")
                            if existing_file.file_type_id != file_type.id:
                                existing_file.file_type = file_type
                                updated_fields.append("file_type")

                        if file_input.file_recap_category_id not in (None, ""):
                            try:
                                category_id = resolve_id_to_int(
                                    file_input.file_recap_category_id
                                )
                                file_recap_category = (
                                    models.FileRecapCategory.objects.get(id=category_id)
                                )
                            except (
                                models.FileRecapCategory.DoesNotExist,
                                TypeError,
                                ValueError,
                                GraphQLError,
                            ):
                                raise GraphQLError("File recap category not found.")
                            if existing_file.file_recap_category_id != file_recap_category.id:
                                existing_file.file_recap_category = file_recap_category
                                updated_fields.append("file_recap_category")

                        if updated_fields:
                            existing_file.save(update_fields=updated_fields)
                        final_files.append(existing_file)
                        continue

                    file_type = None
                    if file_input.file_type_id not in (None, ""):
                        try:
                            file_type_id = resolve_id_to_int(file_input.file_type_id)
                            file_type = FileType.objects.get(id=file_type_id)
                        except (FileType.DoesNotExist, TypeError, ValueError, GraphQLError):
                            raise GraphQLError("File type not found.")
                    if not file_type:
                        file_type = FileType.objects.first()
                    if not file_type:
                        raise GraphQLError("No file type available.")

                    file_recap_category = None
                    if file_input.file_recap_category_id not in (None, ""):
                        try:
                            category_id = resolve_id_to_int(
                                file_input.file_recap_category_id
                            )
                            file_recap_category = models.FileRecapCategory.objects.get(
                                id=category_id
                            )
                        except (
                            models.FileRecapCategory.DoesNotExist,
                            TypeError,
                            ValueError,
                            GraphQLError,
                        ):
                            raise GraphQLError("File recap category not found.")

                    recap_file = models.RecapFile(
                        name=f"Recap file for {self.input.name}",
                        file=blob_name,
                        file_type=file_type,
                        file_recap_category=file_recap_category,
                        recap=recap,
                        approved=False,
                        created_by=self.user,
                    )
                    recap_file.save()
                    final_files.append(recap_file)

                removed_files = list(blob_to_file.values())

                # Update the recap
                recap.name = self.input.name
                recap.event = event
                recap.products_sold = self.input.products_sold
                recap.total_cans_sold = self.input.total_cans_sold
                recap.total_packs_sold = self.input.total_packs_sold
                recap.total_earnings = self.input.total_earnings
                recap.account_spend_amount = self.input.account_spend_amount
                recap.traffic_description = self.input.traffic_description
                recap.competitive_presence = self.input.competitive_presence
                if self.input.consumer_engagements is not None:
                    recap.total_engagements = (
                        self.input.consumer_engagements.total_consumer
                    )
                if self.input.job_id is not None:
                    recap.job = job
                if self.input.retailer_id is not None:
                    recap.retailer = retailer
                if self.input.ambassador_id is not None:
                    recap.ambassador = ambassador
                if self.input.location_id is not None:
                    recap.location = location
                if self.input.state_id is not None:
                    recap.state = state
                if self.input.filling_for_ambassador is not None:
                    recap.filling_for_ambassador = self.input.filling_for_ambassador
                if self.input.late is not None:
                    recap.late = self.input.late
                if self.input.incomplete is not None:
                    recap.incomplete = self.input.incomplete
                recap.updated_by = self.user
                recap.save()

                if self.input.consumer_feedback is not None:
                    consumer_feedback = (
                        models.ConsumerFeedback.objects.filter(recap=recap)
                        .order_by("-created_at")
                        .first()
                    )
                    if consumer_feedback:
                        consumer_feedback.demographics = (
                            self.input.consumer_feedback.demographics
                        )
                        consumer_feedback.feedback = self.input.consumer_feedback.feedback
                        consumer_feedback.quotes = self.input.consumer_feedback.quotes
                        consumer_feedback.positive_stories = (
                            self.input.consumer_feedback.positive_stories
                        )
                        consumer_feedback.reasons_to_decline = (
                            self.input.consumer_feedback.reasons_to_decline
                        )
                        consumer_feedback.updated_by = self.user
                        consumer_feedback.save(
                            update_fields=[
                                "demographics",
                                "feedback",
                                "quotes",
                                "positive_stories",
                                "reasons_to_decline",
                                "updated_by",
                                "updated_at",
                            ]
                        )
                    else:
                        models.ConsumerFeedback.objects.create(
                            recap=recap,
                            created_by=self.user,
                            demographics=self.input.consumer_feedback.demographics,
                            feedback=self.input.consumer_feedback.feedback,
                            quotes=self.input.consumer_feedback.quotes,
                            positive_stories=self.input.consumer_feedback.positive_stories,
                            reasons_to_decline=self.input.consumer_feedback.reasons_to_decline,
                        )

                if self.input.account_feedback is not None:
                    account_feedback = (
                        models.AccountFeedback.objects.filter(recap=recap)
                        .order_by("-created_at")
                        .first()
                    )
                    if account_feedback:
                        account_feedback.do_differently_feedback = (
                            self.input.account_feedback.do_differently_feedback
                        )
                        account_feedback.feedback = self.input.account_feedback.feedback
                        account_feedback.corpo_card = self.input.account_feedback.corpo_card
                        account_feedback.was_corpo_card_used = bool(
                            self.input.account_feedback.was_corpo_card_used
                        )
                        account_feedback.updated_by = self.user
                        account_feedback.save(
                            update_fields=[
                                "do_differently_feedback",
                                "feedback",
                                "corpo_card",
                                "was_corpo_card_used",
                                "updated_by",
                                "updated_at",
                            ]
                        )
                    else:
                        models.AccountFeedback.objects.create(
                            recap=recap,
                            created_by=self.user,
                            do_differently_feedback=self.input.account_feedback.do_differently_feedback,
                            feedback=self.input.account_feedback.feedback,
                            corpo_card=self.input.account_feedback.corpo_card,
                            was_corpo_card_used=bool(
                                self.input.account_feedback.was_corpo_card_used
                            ),
                        )

                if self._has_any_consumer_engagements(self.input.consumer_engagements):
                    consumer_engagement = (
                        models.ConsumerEngagements.objects.filter(recap=recap)
                        .order_by("-created_at")
                        .first()
                    )
                    if consumer_engagement:
                        consumer_engagement.total_consumer = (
                            self.input.consumer_engagements.total_consumer
                        )
                        consumer_engagement.first_time_consumers = (
                            self.input.consumer_engagements.first_time_consumers
                        )
                        consumer_engagement.brand_aware_consumers = (
                            self.input.consumer_engagements.brand_aware_consumers
                        )
                        consumer_engagement.willing_to_purchase_consumers = (
                            self.input.consumer_engagements.willing_to_purchase_consumers
                        )
                        consumer_engagement.not_willing_consumers = (
                            self.input.consumer_engagements.not_willing_consumers
                        )
                        consumer_engagement.updated_by = self.user
                        consumer_engagement.save(
                            update_fields=[
                                "total_consumer",
                                "first_time_consumers",
                                "brand_aware_consumers",
                                "willing_to_purchase_consumers",
                                "not_willing_consumers",
                                "updated_by",
                                "updated_at",
                            ]
                        )
                    else:
                        models.ConsumerEngagements.objects.create(
                            recap=recap,
                            created_by=self.user,
                            total_consumer=self.input.consumer_engagements.total_consumer,
                            first_time_consumers=self.input.consumer_engagements.first_time_consumers,
                            brand_aware_consumers=self.input.consumer_engagements.brand_aware_consumers,
                            willing_to_purchase_consumers=self.input.consumer_engagements.willing_to_purchase_consumers,
                            not_willing_consumers=self.input.consumer_engagements.not_willing_consumers,
                        )

                if self.input.product_samples is not None:
                    models.ProductSamples.objects.filter(recap=recap).delete()
                    for sample in self.input.product_samples:
                        if not self._has_complete_product_sample(sample):
                            continue
                        try:
                            product_id = resolve_id_to_int(sample.product_id)
                            models.ProductSamples.objects.create(
                                recap=recap,
                                created_by=self.user,
                                product_id=product_id,
                                quantity=sample.quantity,
                            )
                        except (TypeError, ValueError, GraphQLError):
                            raise GraphQLError(f"Invalid product ID: {sample.product_id}")

                if self.input.sales_performance is not None:
                    models.SalesPerformance.objects.filter(recap=recap).delete()
                    for sale in self.input.sales_performance:
                        if not self._has_complete_sales_performance(sale):
                            continue
                        try:
                            product_id = resolve_id_to_int(sale.product_id)
                            type_of_good_id = resolve_id_to_int(sale.type_of_good_id)
                            models.SalesPerformance.objects.create(
                                recap=recap,
                                created_by=self.user,
                                product_id=product_id,
                                type_of_good_id=type_of_good_id,
                                price=sale.price,
                            )
                        except (TypeError, ValueError, GraphQLError):
                            raise GraphQLError("Invalid product or type of good ID")

                for recap_file in final_files:
                    if recap_file.recap_id != recap.id:
                        recap_file.recap = recap
                        recap_file.save(update_fields=["recap"])

                removed_blob_names = [
                    extract_blob_name_from_url(str(file.file)) for file in removed_files
                ]

                if removed_files:
                    models.RecapFile.objects.filter(
                        id__in=[file.id for file in removed_files]
                    ).delete()

                return recap, removed_blob_names

        recap, removed_blob_names = await update_recap_with_files()

        for blob_name in removed_blob_names:
            if blob_name:
                delete_blob(blob_name)

        if recap.filling_for_ambassador or recap.late or recap.incomplete:
            await self._apply_time_based_recap_payment_rule(
                event=recap.event,
                job=recap.job,
                ambassador=recap.ambassador,
            )

        return recap

    async def create_custom_recap(self) -> models.CustomRecap:
        """Create a custom recap."""
        if not isinstance(self.input, inputs.CreateCustomRecapInput):
            raise GraphQLError("Invalid input type.")

        try:
            event_id = resolve_id_to_int(self.input.event_id)
            event = await sync_to_async(Event.objects.get)(id=event_id)
        except (Event.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Event not found.")

        try:
            template_id = resolve_id_to_int(self.input.custom_recap_template_id)
            custom_recap_template = await sync_to_async(
                models.CustomRecapTemplate.objects.get
            )(id=template_id)
        except (
            models.CustomRecapTemplate.DoesNotExist,
            TypeError,
            ValueError,
            GraphQLError,
        ):
            raise GraphQLError("Custom recap template not found.")

        if custom_recap_template.tenant_id != event.tenant_id:
            raise GraphQLError(
                "Custom recap template does not belong to the event tenant."
            )

        timezone = None
        if self.input.timezone_id:
            try:
                timezone_id = resolve_id_to_int(self.input.timezone_id)
                timezone = await sync_to_async(TimeZone.objects.get)(id=timezone_id)
            except (TimeZone.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Time zone not found.")

        job = None
        if self.input.job_id:
            try:
                job_id = resolve_id_to_int(self.input.job_id)
                job = await sync_to_async(Job.objects.get)(id=job_id)
            except (Job.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Job not found.")

        retailer = None
        if self.input.retailer_id:
            try:
                retailer_id = resolve_id_to_int(self.input.retailer_id)
                retailer = await sync_to_async(Retailer.objects.get)(id=retailer_id)
            except (Retailer.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Retailer not found.")

        ambassador = None
        if self.input.ambassador_id:
            try:
                ambassador_id = resolve_id_to_int(self.input.ambassador_id)
                ambassador = await sync_to_async(Ambassador.objects.get)(
                    id=ambassador_id
                )
            except (Ambassador.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Ambassador not found.")

        location = None
        if self.input.location_id:
            try:
                location_id = resolve_id_to_int(self.input.location_id)
                location = await sync_to_async(Location.objects.get)(id=location_id)
            except (Location.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Location not found.")

        state = None
        if self.input.state_id:
            try:
                state_id = resolve_id_to_int(self.input.state_id)
                state = await sync_to_async(State.objects.get)(id=state_id)
            except (State.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("State not found.")

        @sync_to_async
        def create_custom_recap_transaction():
            with transaction.atomic():
                custom_recap = models.CustomRecap.objects.create(
                    name=self.input.name,
                    event=event,
                    timezone=timezone,
                    total_engagements=self.input.total_engagements,
                    job=job,
                    retailer=retailer,
                    ambassador=ambassador,
                    location=location,
                    state=state,
                    tenant_id=event.tenant_id,
                    custom_recap_template=custom_recap_template,
                    created_by=self.user,
                )
                if self.input.filling_for_ambassador is not None:
                    custom_recap.filling_for_ambassador = (
                        self.input.filling_for_ambassador
                    )
                if self.input.late is not None:
                    custom_recap.late = self.input.late
                if self.input.incomplete is not None:
                    custom_recap.incomplete = self.input.incomplete
                if self.input.approved is not None:
                    custom_recap.approved = self.input.approved
                if self.input.used_corpo_card is not None:
                    custom_recap.used_corpo_card = self.input.used_corpo_card
                custom_recap.save()

                if self.input.custom_field_values is not None:
                    for custom_field_value_input in self.input.custom_field_values:
                        try:
                            custom_field_id = resolve_id_to_int(
                                custom_field_value_input.custom_field_id
                            )
                        except (TypeError, ValueError, GraphQLError):
                            raise GraphQLError(
                                f"Invalid custom field ID: {custom_field_value_input.custom_field_id}"
                            )

                        custom_field = models.CustomField.objects.filter(
                            id=custom_field_id,
                            custom_recap_template_id=custom_recap_template.id,
                        ).first()
                        if not custom_field:
                            raise GraphQLError(
                                "Custom field not found for the selected template."
                            )

                        models.CustomFieldValue.objects.create(
                            custom_recap=custom_recap,
                            custom_field=custom_field,
                            value=custom_field_value_input.value,
                            created_by=self.user,
                        )

                if self.input.product_samples is not None:
                    for sample in self.input.product_samples:
                        if not self._has_complete_product_sample(sample):
                            continue
                        try:
                            product_id = resolve_id_to_int(sample.product_id)
                            models.CustomRecapProductSample.objects.create(
                                custom_recap=custom_recap,
                                created_by=self.user,
                                product_id=product_id,
                                quantity=sample.quantity,
                            )
                        except (TypeError, ValueError, GraphQLError):
                            raise GraphQLError(
                                f"Invalid product ID: {sample.product_id}"
                            )

                if self.input.sales_performance is not None:
                    for sale in self.input.sales_performance:
                        if not self._has_complete_sales_performance(sale):
                            continue
                        try:
                            product_id = resolve_id_to_int(sale.product_id)
                            type_of_good_id = resolve_id_to_int(sale.type_of_good_id)
                            models.CustomRecapSalePerformance.objects.create(
                                custom_recap=custom_recap,
                                created_by=self.user,
                                product_id=product_id,
                                type_of_good_id=type_of_good_id,
                                price=sale.price,
                            )
                        except (TypeError, ValueError, GraphQLError):
                            raise GraphQLError("Invalid product or type of good ID")

                if self.input.files is not None:
                    for file_input in self.input.files:
                        file_url = file_input.file
                        blob_name = extract_blob_name_from_url(file_url)
                        if not blob_name:
                            raise GraphQLError("Invalid custom recap file path.")

                        file_type = None
                        if file_input.file_type_id not in (None, ""):
                            try:
                                file_type_id = resolve_id_to_int(file_input.file_type_id)
                                file_type = FileType.objects.get(id=file_type_id)
                            except (FileType.DoesNotExist, TypeError, ValueError, GraphQLError):
                                raise GraphQLError("File type not found.")

                        if not file_type:
                            file_type = FileType.objects.first()
                        if not file_type:
                            raise GraphQLError("No file type available.")

                        file_recap_category = None
                        if file_input.file_recap_category_id not in (None, ""):
                            try:
                                category_id = resolve_id_to_int(
                                    file_input.file_recap_category_id
                                )
                                file_recap_category = (
                                    models.FileRecapCategory.objects.get(id=category_id)
                                )
                            except (
                                models.FileRecapCategory.DoesNotExist,
                                TypeError,
                                ValueError,
                                GraphQLError,
                            ):
                                raise GraphQLError("File recap category not found.")

                        models.CustomRecapFile.objects.create(
                            name=f"Custom recap file for {self.input.name}",
                            url=blob_name,
                            file_type=file_type,
                            file_recap_category=file_recap_category,
                            custom_recap=custom_recap,
                            approved=False,
                            created_by=self.user,
                        )
                return custom_recap

        return await create_custom_recap_transaction()

    async def update_custom_recap(self) -> models.CustomRecap:
        """Update a custom recap."""
        if not isinstance(self.input, inputs.UpdateCustomRecapInput):
            raise GraphQLError("Invalid input type.")

        try:
            custom_recap_id = resolve_id_to_int(self.input.id)
            custom_recap = await sync_to_async(models.CustomRecap.objects.get)(
                id=custom_recap_id
            )
        except (models.CustomRecap.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Custom recap not found.")

        try:
            event_id = resolve_id_to_int(self.input.event_id)
            event = await sync_to_async(Event.objects.get)(id=event_id)
        except (Event.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Event not found.")

        try:
            template_id = resolve_id_to_int(self.input.custom_recap_template_id)
            custom_recap_template = await sync_to_async(
                models.CustomRecapTemplate.objects.get
            )(id=template_id)
        except (
            models.CustomRecapTemplate.DoesNotExist,
            TypeError,
            ValueError,
            GraphQLError,
        ):
            raise GraphQLError("Custom recap template not found.")

        if custom_recap_template.tenant_id != event.tenant_id:
            raise GraphQLError(
                "Custom recap template does not belong to the event tenant."
            )

        timezone = None
        if self.input.timezone_id:
            try:
                timezone_id = resolve_id_to_int(self.input.timezone_id)
                timezone = await sync_to_async(TimeZone.objects.get)(id=timezone_id)
            except (TimeZone.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Time zone not found.")

        job = None
        if self.input.job_id:
            try:
                job_id = resolve_id_to_int(self.input.job_id)
                job = await sync_to_async(Job.objects.get)(id=job_id)
            except (Job.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Job not found.")

        retailer = None
        if self.input.retailer_id:
            try:
                retailer_id = resolve_id_to_int(self.input.retailer_id)
                retailer = await sync_to_async(Retailer.objects.get)(id=retailer_id)
            except (Retailer.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Retailer not found.")

        ambassador = None
        if self.input.ambassador_id:
            try:
                ambassador_id = resolve_id_to_int(self.input.ambassador_id)
                ambassador = await sync_to_async(Ambassador.objects.get)(
                    id=ambassador_id
                )
            except (Ambassador.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Ambassador not found.")

        location = None
        if self.input.location_id:
            try:
                location_id = resolve_id_to_int(self.input.location_id)
                location = await sync_to_async(Location.objects.get)(id=location_id)
            except (Location.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Location not found.")

        state = None
        if self.input.state_id:
            try:
                state_id = resolve_id_to_int(self.input.state_id)
                state = await sync_to_async(State.objects.get)(id=state_id)
            except (State.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("State not found.")

        @sync_to_async
        def update_custom_recap_transaction():
            with transaction.atomic():
                custom_recap.name = self.input.name
                custom_recap.event = event
                custom_recap.custom_recap_template = custom_recap_template
                custom_recap.total_engagements = self.input.total_engagements
                custom_recap.updated_by = self.user
                custom_recap.tenant_id = event.tenant_id

                if self.input.timezone_id is not None:
                    custom_recap.timezone = timezone
                if self.input.job_id is not None:
                    custom_recap.job = job
                if self.input.retailer_id is not None:
                    custom_recap.retailer = retailer
                if self.input.ambassador_id is not None:
                    custom_recap.ambassador = ambassador
                if self.input.location_id is not None:
                    custom_recap.location = location
                if self.input.state_id is not None:
                    custom_recap.state = state
                if self.input.filling_for_ambassador is not None:
                    custom_recap.filling_for_ambassador = (
                        self.input.filling_for_ambassador
                    )
                if self.input.late is not None:
                    custom_recap.late = self.input.late
                if self.input.incomplete is not None:
                    custom_recap.incomplete = self.input.incomplete
                if self.input.approved is not None:
                    custom_recap.approved = self.input.approved
                if self.input.used_corpo_card is not None:
                    custom_recap.used_corpo_card = self.input.used_corpo_card

                custom_recap.save()

                if self.input.custom_field_values is not None:
                    models.CustomFieldValue.objects.filter(
                        custom_recap=custom_recap
                    ).delete()

                    for custom_field_value_input in self.input.custom_field_values:
                        try:
                            custom_field_id = resolve_id_to_int(
                                custom_field_value_input.custom_field_id
                            )
                        except (TypeError, ValueError, GraphQLError):
                            raise GraphQLError(
                                f"Invalid custom field ID: {custom_field_value_input.custom_field_id}"
                            )

                        custom_field = models.CustomField.objects.filter(
                            id=custom_field_id,
                            custom_recap_template_id=custom_recap_template.id,
                        ).first()
                        if not custom_field:
                            raise GraphQLError(
                                "Custom field not found for the selected template."
                            )

                        models.CustomFieldValue.objects.create(
                            custom_recap=custom_recap,
                            custom_field=custom_field,
                            value=custom_field_value_input.value,
                            created_by=self.user,
                        )

                if self.input.product_samples is not None:
                    models.CustomRecapProductSample.objects.filter(
                        custom_recap=custom_recap
                    ).delete()
                    for sample in self.input.product_samples:
                        if not self._has_complete_product_sample(sample):
                            continue
                        try:
                            product_id = resolve_id_to_int(sample.product_id)
                            models.CustomRecapProductSample.objects.create(
                                custom_recap=custom_recap,
                                created_by=self.user,
                                product_id=product_id,
                                quantity=sample.quantity,
                            )
                        except (TypeError, ValueError, GraphQLError):
                            raise GraphQLError(
                                f"Invalid product ID: {sample.product_id}"
                            )

                if self.input.sales_performance is not None:
                    models.CustomRecapSalePerformance.objects.filter(
                        custom_recap=custom_recap
                    ).delete()
                    for sale in self.input.sales_performance:
                        if not self._has_complete_sales_performance(sale):
                            continue
                        try:
                            product_id = resolve_id_to_int(sale.product_id)
                            type_of_good_id = resolve_id_to_int(sale.type_of_good_id)
                            models.CustomRecapSalePerformance.objects.create(
                                custom_recap=custom_recap,
                                created_by=self.user,
                                product_id=product_id,
                                type_of_good_id=type_of_good_id,
                                price=sale.price,
                            )
                        except (TypeError, ValueError, GraphQLError):
                            raise GraphQLError("Invalid product or type of good ID")

                removed_blob_names: list[str] = []
                if self.input.files is not None:
                    existing_files = list(
                        models.CustomRecapFile.objects.filter(custom_recap=custom_recap)
                    )
                    blob_to_file = {
                        extract_blob_name_from_url(str(file.url)): file
                        for file in existing_files
                        if extract_blob_name_from_url(str(file.url))
                    }

                    final_files: list[models.CustomRecapFile] = []
                    for file_input in self.input.files:
                        file_url = file_input.file
                        blob_name = extract_blob_name_from_url(file_url)
                        if not blob_name:
                            raise GraphQLError("Invalid custom recap file path.")

                        if blob_name in blob_to_file:
                            existing_file = blob_to_file.pop(blob_name)
                            updated_fields = []

                            if file_input.file_type_id not in (None, ""):
                                try:
                                    file_type_id = resolve_id_to_int(file_input.file_type_id)
                                    file_type = FileType.objects.get(id=file_type_id)
                                except (
                                    FileType.DoesNotExist,
                                    TypeError,
                                    ValueError,
                                    GraphQLError,
                                ):
                                    raise GraphQLError("File type not found.")
                                if existing_file.file_type_id != file_type.id:
                                    existing_file.file_type = file_type
                                    updated_fields.append("file_type")

                            if file_input.file_recap_category_id not in (None, ""):
                                try:
                                    category_id = resolve_id_to_int(
                                        file_input.file_recap_category_id
                                    )
                                    file_recap_category = (
                                        models.FileRecapCategory.objects.get(id=category_id)
                                    )
                                except (
                                    models.FileRecapCategory.DoesNotExist,
                                    TypeError,
                                    ValueError,
                                    GraphQLError,
                                ):
                                    raise GraphQLError("File recap category not found.")
                                if existing_file.file_recap_category_id != file_recap_category.id:
                                    existing_file.file_recap_category = file_recap_category
                                    updated_fields.append("file_recap_category")

                            if updated_fields:
                                existing_file.save(update_fields=updated_fields)
                            final_files.append(existing_file)
                            continue

                        file_type = None
                        if file_input.file_type_id not in (None, ""):
                            try:
                                file_type_id = resolve_id_to_int(file_input.file_type_id)
                                file_type = FileType.objects.get(id=file_type_id)
                            except (FileType.DoesNotExist, TypeError, ValueError, GraphQLError):
                                raise GraphQLError("File type not found.")
                        if not file_type:
                            file_type = FileType.objects.first()
                        if not file_type:
                            raise GraphQLError("No file type available.")

                        file_recap_category = None
                        if file_input.file_recap_category_id not in (None, ""):
                            try:
                                category_id = resolve_id_to_int(
                                    file_input.file_recap_category_id
                                )
                                file_recap_category = models.FileRecapCategory.objects.get(
                                    id=category_id
                                )
                            except (
                                models.FileRecapCategory.DoesNotExist,
                                TypeError,
                                ValueError,
                                GraphQLError,
                            ):
                                raise GraphQLError("File recap category not found.")

                        custom_recap_file = models.CustomRecapFile.objects.create(
                            name=f"Custom recap file for {self.input.name}",
                            url=blob_name,
                            file_type=file_type,
                            file_recap_category=file_recap_category,
                            custom_recap=custom_recap,
                            approved=False,
                            created_by=self.user,
                        )
                        final_files.append(custom_recap_file)

                    removed_files = list(blob_to_file.values())
                    if removed_files:
                        removed_blob_names = [
                            extract_blob_name_from_url(str(file.url))
                            for file in removed_files
                        ]
                        models.CustomRecapFile.objects.filter(
                            id__in=[file.id for file in removed_files]
                        ).delete()

                return custom_recap, removed_blob_names

        custom_recap, removed_blob_names = await update_custom_recap_transaction()
        for blob_name in removed_blob_names:
            if blob_name:
                delete_blob(blob_name)
        return custom_recap

    async def create_custom_field(self) -> models.CustomField:
        """Create a custom field."""
        if not isinstance(self.input, inputs.CreateCustomFieldInput):
            raise GraphQLError("Invalid input type.")

        try:
            custom_recap_template_id = resolve_id_to_int(
                self.input.custom_recap_template_id
            )
            custom_recap_template = await sync_to_async(
                models.CustomRecapTemplate.objects.get
            )(id=custom_recap_template_id)
        except (
            models.CustomRecapTemplate.DoesNotExist,
            TypeError,
            ValueError,
            GraphQLError,
        ):
            raise GraphQLError("Custom recap template not found.")

        try:
            custom_field_type_id = resolve_id_to_int(self.input.custom_field_type_id)
            custom_field_type = await sync_to_async(
                models.CustomRecapFieldType.objects.get
            )(id=custom_field_type_id)
        except (
            models.CustomRecapFieldType.DoesNotExist,
            TypeError,
            ValueError,
            GraphQLError,
        ):
            raise GraphQLError("Custom field type not found.")

        try:
            recap_section_id = resolve_id_to_int(self.input.recap_section_id)
            recap_section = await sync_to_async(models.RecapSection.objects.get)(
                id=recap_section_id
            )
        except (models.RecapSection.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Recap section not found.")

        if recap_section.tenant_id != custom_recap_template.tenant_id:
            raise GraphQLError(
                "Recap section does not belong to the template tenant."
            )

        @sync_to_async
        def create_custom_field_transaction():
            with transaction.atomic():
                custom_field = models.CustomField.objects.create(
                    name=self.input.name,
                    custom_recap_template=custom_recap_template,
                    custom_field_type=custom_field_type,
                    recap_section=recap_section,
                    created_by=self.user,
                    required=bool(self.input.required),
                )
                return custom_field

        return await create_custom_field_transaction()

    async def update_custom_field(self) -> models.CustomField:
        """Update a custom field."""
        if not isinstance(self.input, inputs.UpdateCustomFieldInput):
            raise GraphQLError("Invalid input type.")

        try:
            custom_field_id = resolve_id_to_int(self.input.id)
            custom_field = await sync_to_async(models.CustomField.objects.get)(
                id=custom_field_id
            )
        except (models.CustomField.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Custom field not found.")

        try:
            custom_recap_template_id = resolve_id_to_int(
                self.input.custom_recap_template_id
            )
            custom_recap_template = await sync_to_async(
                models.CustomRecapTemplate.objects.get
            )(id=custom_recap_template_id)
        except (
            models.CustomRecapTemplate.DoesNotExist,
            TypeError,
            ValueError,
            GraphQLError,
        ):
            raise GraphQLError("Custom recap template not found.")

        try:
            custom_field_type_id = resolve_id_to_int(self.input.custom_field_type_id)
            custom_field_type = await sync_to_async(
                models.CustomRecapFieldType.objects.get
            )(id=custom_field_type_id)
        except (
            models.CustomRecapFieldType.DoesNotExist,
            TypeError,
            ValueError,
            GraphQLError,
        ):
            raise GraphQLError("Custom field type not found.")

        try:
            recap_section_id = resolve_id_to_int(self.input.recap_section_id)
            recap_section = await sync_to_async(models.RecapSection.objects.get)(
                id=recap_section_id
            )
        except (models.RecapSection.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Recap section not found.")

        if recap_section.tenant_id != custom_recap_template.tenant_id:
            raise GraphQLError(
                "Recap section does not belong to the template tenant."
            )

        @sync_to_async
        def update_custom_field_transaction():
            with transaction.atomic():
                custom_field.name = self.input.name
                custom_field.custom_recap_template = custom_recap_template
                custom_field.custom_field_type = custom_field_type
                custom_field.recap_section = recap_section
                custom_field.updated_by = self.user
                if self.input.required is not None:
                    custom_field.required = self.input.required
                custom_field.save()
                return custom_field

        return await update_custom_field_transaction()

    async def create_custom_recap_template(self) -> models.CustomRecapTemplate:
        """Create a custom recap template."""
        if not isinstance(self.input, inputs.CreateCustomRecapTemplateInput):
            raise GraphQLError("Invalid input type.")

        try:
            event_type_id = resolve_id_to_int(self.input.event_type_id)
            event_type = await sync_to_async(EventType.objects.get)(id=event_type_id)
        except (EventType.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Event type not found.")

        @sync_to_async
        def create_custom_recap_template_transaction():
            with transaction.atomic():
                custom_recap_template = models.CustomRecapTemplate.objects.create(
                    name=self.input.name,
                    event_type=event_type,
                    tenant_id=event_type.tenant_id,
                    product_samples=bool(self.input.product_samples),
                    sales_performance=bool(self.input.sales_performance),
                    layout=self.input.layout or {},
                    created_by=self.user,
                )
                return custom_recap_template

        return await create_custom_recap_template_transaction()

    async def update_custom_recap_template(self) -> models.CustomRecapTemplate:
        """Update a custom recap template."""
        if not isinstance(self.input, inputs.UpdateCustomRecapTemplateInput):
            raise GraphQLError("Invalid input type.")

        try:
            custom_recap_template_id = resolve_id_to_int(self.input.id)
            custom_recap_template = await sync_to_async(
                models.CustomRecapTemplate.objects.get
            )(id=custom_recap_template_id)
        except (
            models.CustomRecapTemplate.DoesNotExist,
            TypeError,
            ValueError,
            GraphQLError,
        ):
            raise GraphQLError("Custom recap template not found.")

        try:
            event_type_id = resolve_id_to_int(self.input.event_type_id)
            event_type = await sync_to_async(EventType.objects.get)(id=event_type_id)
        except (EventType.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Event type not found.")

        @sync_to_async
        def update_custom_recap_template_transaction():
            with transaction.atomic():
                custom_recap_template.name = self.input.name
                custom_recap_template.event_type = event_type
                custom_recap_template.tenant_id = event_type.tenant_id
                custom_recap_template.updated_by = self.user
                if self.input.product_samples is not None:
                    custom_recap_template.product_samples = self.input.product_samples
                if self.input.sales_performance is not None:
                    custom_recap_template.sales_performance = (
                        self.input.sales_performance
                    )
                if self.input.layout is not None:
                    custom_recap_template.layout = self.input.layout
                custom_recap_template.save()
                return custom_recap_template

        return await update_custom_recap_template_transaction()

    async def create_custom_recap_field_type(self) -> models.CustomRecapFieldType:
        """Create a custom recap field type."""
        if not isinstance(self.input, inputs.CreateCustomRecapFieldTypeInput):
            raise GraphQLError("Invalid input type.")

        @sync_to_async
        def create_custom_recap_field_type_transaction():
            with transaction.atomic():
                custom_recap_field_type = models.CustomRecapFieldType.objects.create(
                    name=self.input.name,
                    created_by=self.user,
                )
                return custom_recap_field_type

        return await create_custom_recap_field_type_transaction()

    async def update_custom_recap_field_type(self) -> models.CustomRecapFieldType:
        """Update a custom recap field type."""
        if not isinstance(self.input, inputs.UpdateCustomRecapFieldTypeInput):
            raise GraphQLError("Invalid input type.")

        try:
            custom_recap_field_type_id = resolve_id_to_int(self.input.id)
            custom_recap_field_type = await sync_to_async(
                models.CustomRecapFieldType.objects.get
            )(id=custom_recap_field_type_id)
        except (
            models.CustomRecapFieldType.DoesNotExist,
            TypeError,
            ValueError,
            GraphQLError,
        ):
            raise GraphQLError("Custom recap field type not found.")

        @sync_to_async
        def update_custom_recap_field_type_transaction():
            with transaction.atomic():
                custom_recap_field_type.name = self.input.name
                custom_recap_field_type.updated_by = self.user
                custom_recap_field_type.save()
                return custom_recap_field_type

        return await update_custom_recap_field_type_transaction()

    async def create_recap_section(self) -> models.RecapSection:
        """Create a recap section."""
        if not isinstance(self.input, inputs.CreateRecapSectionInput):
            raise GraphQLError("Invalid input type.")

        try:
            tenant_id = resolve_id_to_int(self.input.tenant_id)
            tenant = await sync_to_async(Tenant.objects.get)(id=tenant_id)
        except (Tenant.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Tenant not found.")

        @sync_to_async
        def create_recap_section_transaction():
            with transaction.atomic():
                recap_section = models.RecapSection.objects.create(
                    name=self.input.name,
                    tenant=tenant,
                    created_by=self.user,
                )
                return recap_section

        return await create_recap_section_transaction()

    async def update_recap_section(self) -> models.RecapSection:
        """Update a recap section."""
        if not isinstance(self.input, inputs.UpdateRecapSectionInput):
            raise GraphQLError("Invalid input type.")

        try:
            recap_section_id = resolve_id_to_int(self.input.id)
            recap_section = await sync_to_async(models.RecapSection.objects.get)(
                id=recap_section_id
            )
        except (models.RecapSection.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Recap section not found.")

        try:
            tenant_id = resolve_id_to_int(self.input.tenant_id)
            tenant = await sync_to_async(Tenant.objects.get)(id=tenant_id)
        except (Tenant.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Tenant not found.")

        @sync_to_async
        def update_recap_section_transaction():
            with transaction.atomic():
                recap_section.name = self.input.name
                recap_section.tenant = tenant
                recap_section.updated_by = self.user
                recap_section.save()
                return recap_section

        return await update_recap_section_transaction()

    async def delete_recap(self) -> bool:
        """Delete a recap."""
        if not isinstance(self.input, inputs.DeleteRecapInput):
            raise GraphQLError("Invalid input type.")

        try:
            recap_id = resolve_id_to_int(self.input.id)
            recap = await sync_to_async(models.Recap.objects.get)(id=recap_id)
        except (models.Recap.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Recap not found.")

        @sync_to_async
        def delete_recap_with_files():
            with transaction.atomic():
                # Detach recap files before deleting recap
                models.RecapFile.objects.filter(recap=recap).update(recap=None)
                # Delete the recap
                recap.delete()
            return True

        return await delete_recap_with_files()

    async def delete_recap_file(self) -> bool:
        """Delete a recap file and its blob from GCS."""
        if not isinstance(self.input, inputs.DeleteRecapFileInput):
            raise GraphQLError("Invalid input type.")

        try:
            recap_file_id = resolve_id_to_int(self.input.id)
            recap_file = await sync_to_async(models.RecapFile.objects.get)(
                id=recap_file_id
            )
        except (models.RecapFile.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Recap file not found.")

        @sync_to_async
        def delete_file_with_references():
            with transaction.atomic():
                if recap_file.recap_id:
                    raise GraphQLError(
                        "Recap file is linked to a recap. Update the recap before deleting this file."
                    )

                blob_name = extract_blob_name_from_url(str(recap_file.file))
                recap_file.delete()
                return blob_name

        blob_name = await delete_file_with_references()
        if blob_name:
            delete_blob(blob_name)
        return True

    async def approve_recap(self) -> models.Recap:
        """Approve or decline a recap."""
        if not isinstance(self.input, inputs.ApproveRecapInput):
            raise GraphQLError("Invalid input type.")

        try:
            recap_id = resolve_id_to_int(self.input.id)
            recap = await sync_to_async(models.Recap.objects.get)(id=recap_id)
        except (models.Recap.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Recap not found.")

        @sync_to_async
        def approve_recap_transaction():
            with transaction.atomic():
                recap.approved = self.input.approved
                recap.updated_by = self.user
                recap.save()
                return recap

        recap = await approve_recap_transaction()
        if self.input.approved:
            recap = await sync_to_async(
                models.Recap.objects.select_related(
                    "event",
                    "event__tenant",
                    "event__rmm_asigned",
                    "event__timezone",
                    "job",
                    "retailer",
                    "timezone",
                    "ambassador",
                    "ambassador__user",
                ).get
            )(id=recap.id)
            await _notify_recap_approved_to_rmm_or_clients(recap)
            await _notify_recap_approved_to_ambassador_by_push(recap)

        return recap

    async def generate_recap_pdf(self) -> models.RecapFile:
        """Generate a PDF with recap details and images, upload it, and return the file."""
        if not isinstance(self.input, inputs.GenerateRecapPdfInput):
            raise GraphQLError("Invalid input type.")

        try:
            recap_id = resolve_id_to_int(self.input.id)
        except (TypeError, ValueError, GraphQLError):
            raise GraphQLError("Recap not found.")

        @sync_to_async
        def fetch_recap():
            return (
                models.Recap.objects.select_related(
                    "event",
                    "event__tenant",
                    "event__event_type",
                    "job",
                    "retailer",
                    "ambassador",
                    "ambassador__user",
                )
                .prefetch_related(
                    Prefetch(
                        "recap_files",
                        queryset=models.RecapFile.objects.select_related(
                            "file_type",
                            "file_recap_category",
                        ),
                    ),
                    "consumer_engagements",
                    Prefetch(
                        "product_samples",
                        queryset=models.ProductSamples.objects.select_related(
                            "product"
                        ),
                    ),
                    Prefetch(
                        "sales_performance",
                        queryset=models.SalesPerformance.objects.select_related(
                            "product",
                            "type_of_good",
                        ),
                    ),
                    "consumer_feedback",
                    "account_feedback",
                )
                .get(id=recap_id)
            )

        try:
            recap = await fetch_recap()
        except models.Recap.DoesNotExist:
            raise GraphQLError("Recap not found.")

        image_entries = []
        for recap_file in recap.recap_files.all():
            blob_name = extract_blob_name_from_url(str(recap_file.file))
            if not blob_name:
                continue
            image_bytes = download_blob_bytes(blob_name)
            if not image_bytes:
                continue
            if not should_embed_recap_file(recap_file) and not is_image_bytes(image_bytes):
                continue
            image_entries.append(
                {
                    "name": recap_file.name,
                    "bytes": image_bytes,
                    "category": (
                        recap_file.file_recap_category.name
                        if recap_file.file_recap_category
                        else "Uncategorized"
                    ),
                }
            )

        pdf_bytes = build_recap_pdf(recap, image_entries)
        timestamp = timezone.now().strftime("%Y%m%d%H%M%S")
        blob_name = f"recaps/pdfs/{recap.uuid}-{timestamp}.pdf"
        upload_bytes(blob_name, pdf_bytes, content_type="application/pdf")

        @sync_to_async
        def create_recap_pdf_file():
            file_type = FileType.objects.filter(
                Q(extension__iexact=".pdf") | Q(extension__iexact="pdf")
            ).first()
            if not file_type:
                raise GraphQLError("No PDF file type available.")
            existing_files = list(
                models.RecapFile.objects.filter(recap=recap, file_type=file_type)
            )
            existing_blob_names = [
                extract_blob_name_from_url(str(item.file)) for item in existing_files
            ]
            if existing_files:
                models.RecapFile.objects.filter(
                    id__in=[item.id for item in existing_files]
                ).delete()
            recap_file = models.RecapFile.objects.create(
                name=f"Recap PDF - {recap.name}",
                file=blob_name,
                file_type=file_type,
                recap=recap,
                approved=False,
                created_by=self.user,
            )
            return recap_file, existing_blob_names

        try:
            recap_file, existing_blob_names = await create_recap_pdf_file()
        except Exception:
            delete_blob(blob_name)
            raise
        for existing_blob_name in existing_blob_names:
            if existing_blob_name:
                delete_blob(existing_blob_name)
        return recap_file

    async def export_recaps_xlsx(self) -> str:
        """Generate an Excel report with all recaps for a tenant and return a signed URL."""
        if not isinstance(self.input, inputs.ExportRecapsXlsxInput):
            raise GraphQLError("Invalid input type.")

        resolved_tenant_id: int | None = None
        if self.input.tenant_id not in (None, ""):
            try:
                resolved_tenant_id = resolve_id_to_int(self.input.tenant_id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid tenant ID.")

        if self.is_spark_schema_request(self.info, user=self.user):
            if resolved_tenant_id is None:
                raise GraphQLError("Tenant ID is required.")
            tenant = await self._get_tenant_without_membership(
                tenant_id=resolved_tenant_id
            )
        else:
            tenant = await self.get_user_tenant(
                self.info,
                tenant_id=resolved_tenant_id,
                user=self.user,
            )
        start_date = self.input.start_date
        end_date = self.input.end_date

        client_origin = (
            self.info.context.request.META.get("HTTP_ORIGIN")
            if self.info and self.info.context and self.info.context.request
            else None
        )
        frontend_base_url = client_origin or settings.ADMIN_FRONTEND_URL

        @sync_to_async
        def build_xlsx_for_tenant():
            service = RecapQueriesService()
            queryset = service.get_filtered_queryset(
                tenant_id=tenant.id,
                start_date=start_date,
                end_date=end_date,
            )
            recaps = list(
                queryset.select_related(
                    "event__request__retailer",
                    "event__request__distributor",
                    "ambassador",
                    "ambassador__user",
                )
            )
            return build_recaps_xlsx(recaps, frontend_base_url=frontend_base_url)

        xlsx_bytes = await build_xlsx_for_tenant()

        timestamp = timezone.now().strftime("%Y%m%d%H%M%S")
        tenant_slug = slugify(getattr(tenant, "name", "") or "tenant")
        export_prefix = f"recaps/exports/{tenant_slug}-"
        blob_name = f"{export_prefix}{timestamp}.xlsx"

        @sync_to_async
        def delete_previous_exports():
            client = get_gcs_client()
            bucket = client.bucket(settings.GS_BUCKET_NAME)
            for blob in bucket.list_blobs(prefix=export_prefix):
                if blob.name != blob_name:
                    blob.delete()

        await delete_previous_exports()
        upload_bytes(
            blob_name,
            xlsx_bytes,
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )
        return generate_download_url(blob_name)

    async def export_recap_xlsx(self) -> str:
        """Generate an Excel report for a single recap and return a signed URL."""
        if not isinstance(self.input, inputs.ExportRecapXlsxInput):
            raise GraphQLError("Invalid input type.")

        try:
            recap_id = resolve_id_to_int(self.input.id)
        except (TypeError, ValueError, GraphQLError):
            raise GraphQLError("Invalid recap ID.")

        client_origin = (
            self.info.context.request.META.get("HTTP_ORIGIN")
            if self.info and self.info.context and self.info.context.request
            else None
        )
        frontend_base_url = client_origin or settings.ADMIN_FRONTEND_URL

        @sync_to_async
        def build_xlsx_for_recap():
            try:
                recap = (
                    RecapQueriesService()
                    .get_queryset()
                    .select_related(
                        "event__request__retailer",
                        "event__request__distributor",
                        "event__tenant",
                        "ambassador",
                        "ambassador__user",
                    )
                    .get(id=recap_id)
                )
            except models.Recap.DoesNotExist:
                return None, None, None
            tenant_name = getattr(getattr(recap, "event", None), "tenant", None)
            return (
                build_recaps_xlsx([recap], frontend_base_url=frontend_base_url),
                recap.uuid,
                getattr(tenant_name, "name", None),
            )

        xlsx_bytes, recap_uuid, tenant_name = await build_xlsx_for_recap()
        if xlsx_bytes is None or recap_uuid is None:
            raise GraphQLError("Recap not found.")

        timestamp = timezone.now().strftime("%Y%m%d%H%M%S")
        tenant_slug = slugify(tenant_name or "tenant")
        export_prefix = f"recaps/exports/{tenant_slug}-{recap_uuid}-"
        blob_name = f"{export_prefix}{timestamp}.xlsx"

        @sync_to_async
        def delete_previous_exports():
            client = get_gcs_client()
            bucket = client.bucket(settings.GS_BUCKET_NAME)
            for blob in bucket.list_blobs(prefix=export_prefix):
                if blob.name != blob_name:
                    blob.delete()

        await delete_previous_exports()
        upload_bytes(
            blob_name,
            xlsx_bytes,
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )
        return generate_download_url(blob_name)

    async def get_recap_file_download_url(self) -> str:
        """Return a signed download URL for a recap file."""
        if not isinstance(self.input, inputs.RecapFileDownloadUrlInput):
            raise GraphQLError("Invalid input type.")

        recap_file_uuid = str(self.input.uuid)

        if self.is_spark_schema_request(self.info, user=self.user):
            @sync_to_async
            def fetch_recap_file():
                return models.RecapFile.objects.select_related(
                    "recap",
                    "recap__event",
                ).get(uuid=recap_file_uuid)
        else:
            tenant = await self.get_user_tenant(self.info, user=self.user)

            @sync_to_async
            def fetch_recap_file():
                return models.RecapFile.objects.select_related(
                    "recap",
                    "recap__event",
                ).get(uuid=recap_file_uuid, recap__event__tenant_id=tenant.id)

        try:
            recap_file = await fetch_recap_file()
        except models.RecapFile.DoesNotExist:
            raise GraphQLError("Recap file not found.")

        blob_name = extract_blob_name_from_url(str(recap_file.file))
        if not blob_name:
            raise GraphQLError("Recap file not found.")
        return generate_download_url(blob_name)


@strawberry.type
class RecapMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_recap(
        self,
        info: strawberry.Info,
        input: inputs.CreateRecapInput,
    ) -> types.RecapDetailResponse:
        """Create a new recap with multiple files."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            recap = await service.create_recap()
            # Reload with the same related-object strategy used by recap queries
            # so mutation responses can include nested recap data consistently.
            recap = await sync_to_async(RecapQueriesService().get_queryset().get)(
                id=recap.id
            )
            return build_mutation_response(
                types.RecapDetailResponse,
                success=True,
                message="Recap created successfully.",
                input_obj=input,
                recap=recap,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RecapDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_recap(
        self,
        info: strawberry.Info,
        input: inputs.UpdateRecapInput,
    ) -> types.RecapDetailResponse:
        """Update an existing recap."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            recap = await service.update_recap()
            return build_mutation_response(
                types.RecapDetailResponse,
                success=True,
                message="Recap updated successfully.",
                input_obj=input,
                recap=recap,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RecapDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_custom_recap(
        self,
        info: strawberry.Info,
        input: inputs.CreateCustomRecapInput,
    ) -> types.CustomRecapDetailResponse:
        """Create a new custom recap."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            custom_recap = await service.create_custom_recap()
            return build_mutation_response(
                types.CustomRecapDetailResponse,
                success=True,
                message="Custom recap created successfully.",
                input_obj=input,
                custom_recap=custom_recap,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.CustomRecapDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_custom_recap(
        self,
        info: strawberry.Info,
        input: inputs.UpdateCustomRecapInput,
    ) -> types.CustomRecapDetailResponse:
        """Update an existing custom recap."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            custom_recap = await service.update_custom_recap()
            return build_mutation_response(
                types.CustomRecapDetailResponse,
                success=True,
                message="Custom recap updated successfully.",
                input_obj=input,
                custom_recap=custom_recap,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.CustomRecapDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_custom_field(
        self,
        info: strawberry.Info,
        input: inputs.CreateCustomFieldInput,
    ) -> types.CustomFieldDetailResponse:
        """Create a new custom field."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            custom_field = await service.create_custom_field()
            return build_mutation_response(
                types.CustomFieldDetailResponse,
                success=True,
                message="Custom field created successfully.",
                input_obj=input,
                custom_field=custom_field,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.CustomFieldDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_custom_field(
        self,
        info: strawberry.Info,
        input: inputs.UpdateCustomFieldInput,
    ) -> types.CustomFieldDetailResponse:
        """Update an existing custom field."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            custom_field = await service.update_custom_field()
            return build_mutation_response(
                types.CustomFieldDetailResponse,
                success=True,
                message="Custom field updated successfully.",
                input_obj=input,
                custom_field=custom_field,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.CustomFieldDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_custom_recap_template(
        self,
        info: strawberry.Info,
        input: inputs.CreateCustomRecapTemplateInput,
    ) -> types.CustomRecapTemplateDetailResponse:
        """Create a new custom recap template."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            custom_recap_template = await service.create_custom_recap_template()
            return build_mutation_response(
                types.CustomRecapTemplateDetailResponse,
                success=True,
                message="Custom recap template created successfully.",
                input_obj=input,
                custom_recap_template=custom_recap_template,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.CustomRecapTemplateDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_custom_recap_template(
        self,
        info: strawberry.Info,
        input: inputs.UpdateCustomRecapTemplateInput,
    ) -> types.CustomRecapTemplateDetailResponse:
        """Update an existing custom recap template."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            custom_recap_template = await service.update_custom_recap_template()
            return build_mutation_response(
                types.CustomRecapTemplateDetailResponse,
                success=True,
                message="Custom recap template updated successfully.",
                input_obj=input,
                custom_recap_template=custom_recap_template,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.CustomRecapTemplateDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_custom_recap_field_type(
        self,
        info: strawberry.Info,
        input: inputs.CreateCustomRecapFieldTypeInput,
    ) -> types.CustomRecapFieldTypeDetailResponse:
        """Create a new custom recap field type."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            custom_recap_field_type = await service.create_custom_recap_field_type()
            return build_mutation_response(
                types.CustomRecapFieldTypeDetailResponse,
                success=True,
                message="Custom recap field type created successfully.",
                input_obj=input,
                custom_recap_field_type=custom_recap_field_type,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.CustomRecapFieldTypeDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_custom_recap_field_type(
        self,
        info: strawberry.Info,
        input: inputs.UpdateCustomRecapFieldTypeInput,
    ) -> types.CustomRecapFieldTypeDetailResponse:
        """Update an existing custom recap field type."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            custom_recap_field_type = await service.update_custom_recap_field_type()
            return build_mutation_response(
                types.CustomRecapFieldTypeDetailResponse,
                success=True,
                message="Custom recap field type updated successfully.",
                input_obj=input,
                custom_recap_field_type=custom_recap_field_type,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.CustomRecapFieldTypeDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_recap_section(
        self,
        info: strawberry.Info,
        input: inputs.CreateRecapSectionInput,
    ) -> types.RecapSectionDetailResponse:
        """Create a new recap section."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            recap_section = await service.create_recap_section()
            return build_mutation_response(
                types.RecapSectionDetailResponse,
                success=True,
                message="Recap section created successfully.",
                input_obj=input,
                recap_section=recap_section,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RecapSectionDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_recap_section(
        self,
        info: strawberry.Info,
        input: inputs.UpdateRecapSectionInput,
    ) -> types.RecapSectionDetailResponse:
        """Update an existing recap section."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            recap_section = await service.update_recap_section()
            return build_mutation_response(
                types.RecapSectionDetailResponse,
                success=True,
                message="Recap section updated successfully.",
                input_obj=input,
                recap_section=recap_section,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RecapSectionDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def delete_recap(
        self,
        info: strawberry.Info,
        input: inputs.DeleteRecapInput,
    ) -> types.RecapDetailResponse:
        """Delete a recap."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            await service.delete_recap()
            return build_mutation_response(
                types.RecapDetailResponse,
                success=True,
                message="Recap deleted successfully.",
                input_obj=input,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RecapDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def delete_recap_file(
        self,
        info: strawberry.Info,
        input: inputs.DeleteRecapFileInput,
    ) -> types.RecapFileDetailResponse:
        """Delete a recap file and its blob."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            await service.delete_recap_file()
            return build_mutation_response(
                types.RecapFileDetailResponse,
                success=True,
                message="Recap file deleted successfully.",
                input_obj=input,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RecapFileDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def approve_recap(
        self,
        info: strawberry.Info,
        input: inputs.ApproveRecapInput,
    ) -> types.RecapDetailResponse:
        """Approve or decline a recap."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            recap = await service.approve_recap()
            message = (
                "Recap approved successfully."
                if input.approved
                else "Recap declined successfully."
            )
            return build_mutation_response(
                types.RecapDetailResponse,
                success=True,
                message=message,
                input_obj=input,
                recap=recap,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RecapDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def generate_recap_pdf(
        self,
        info: strawberry.Info,
        input: inputs.GenerateRecapPdfInput,
    ) -> types.RecapFileDetailResponse:
        """Generate a recap PDF and return the resulting file."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            recap_file = await service.generate_recap_pdf()
            return build_mutation_response(
                types.RecapFileDetailResponse,
                success=True,
                message="Recap PDF generated successfully.",
                input_obj=input,
                recap_file=recap_file,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RecapFileDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def export_recaps_xlsx(
        self,
        info: strawberry.Info,
        input: inputs.ExportRecapsXlsxInput,
    ) -> types.RecapExportResponse:
        """Export all recaps for a tenant to an Excel file."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            file_url = await service.export_recaps_xlsx()
            return build_mutation_response(
                types.RecapExportResponse,
                success=True,
                message="Recaps exported successfully.",
                input_obj=input,
                file_url=file_url,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RecapExportResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def export_recap_xlsx(
        self,
        info: strawberry.Info,
        input: inputs.ExportRecapXlsxInput,
    ) -> types.RecapExportResponse:
        """Export a single recap to an Excel file."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            file_url = await service.export_recap_xlsx()
            return build_mutation_response(
                types.RecapExportResponse,
                success=True,
                message="Recap exported successfully.",
                input_obj=input,
                file_url=file_url,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RecapExportResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def recap_file_download_url(
        self,
        info: strawberry.Info,
        input: inputs.RecapFileDownloadUrlInput,
    ) -> types.RecapFileUrlResponse:
        """Return a signed download URL for a recap file."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            file_url = await service.get_recap_file_download_url()
            return build_mutation_response(
                types.RecapFileUrlResponse,
                success=True,
                message="Recap file URL generated successfully.",
                input_obj=input,
                file_url=file_url,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RecapFileUrlResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )
