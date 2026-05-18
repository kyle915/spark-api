import strawberry
import datetime

import strawberry
from strawberry import relay
from strawberry.extensions import MaxTokensLimiter
from graphql import GraphQLError
from asgiref.sync import sync_to_async
from typing import Any, Type, TypeVar, Union

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.conf import settings
from django.db.models import Model
from django.db import transaction
from django.db.models.deletion import RestrictedError

from events import types
from events import models
from events import inputs
from utils.graphql.inputs import SparkGraphQLInput
from utils.graphql.permissions import StrictIsAuthenticated
from utils.graphql.relay import ensure_relay_mutation
from utils.graphql.types import SparkGraphQLErrorResponse
from utils.graphql.mixins import SparkGraphQLMixin, resolve_id_to_int
from utils.utils import ROLE_ID, build_mutation_response
from .envelopes import (
    EventApprovedNotificationMailer,
    RequestorRequestApprovedMailer,
    RequestorRequestDeclinedMailer,
    RequestCreatedNotificationMailer,
    RequestorRequestCreatedMailer,
    RmmAssignedRequestMailer,
)
from .routing import (
    assign_rmm_for_request,
    extract_state_code,
    IGNITE_REVIEW_CC,
)
from utils.gcs import (
    delete_blob,
    download_blob_bytes,
    extract_blob_name_from_url,
    generate_download_url,
    upload_bytes,
)
from tenants.models import Tenant, User
from events.batch_requests import (
    build_request_batch_template_xlsx,
    import_requests_from_excel_bytes,
)
from jobs.envelopes import (
    AmbassadorAppliedJobUpdatedMailer,
    AmbassadorEventSuspendedMailer,
    AmbassadorInvitedJobUpdatedMailer,
    AmbassadorJobUpdatedMailer,
)
from jobs import models as job_models
from jobs.notification_rules import should_send_ambassador_event_email

ensure_relay_mutation()

User = get_user_model()

# MutationResponseType = TypeVar("MutationResponseType")


# def build_mutation_response(
#     response_cls: Type[MutationResponseType],
#     *,
#     success: bool,
#     message: str,
#     input_obj: SparkGraphQLInput | None = None,
#     **extra_fields: Any,
# ) -> MutationResponseType:
#     """Helper to keep relay clientMutationId propagation consistent."""
#     client_mutation_id = getattr(input_obj, "client_mutation_id", None)
#     return response_cls(
#         success=success,
#         message=message,
#         client_mutation_id=client_mutation_id,
#         **extra_fields,
#     )


class BaseMutationService(SparkGraphQLMixin):
    """Base class for mutation services."""

    input: SparkGraphQLInput | None = None
    info: strawberry.Info | None = None
    user: User | None = None
    tenant_id: int | None = None
    is_public: bool = False
    is_spark_schema: bool = False

    @classmethod
    def with_input(cls, input: SparkGraphQLInput) -> "BaseMutationService":
        """Create a new instance of the service with the input."""
        service = cls()
        service.set_input(input)
        return service

    @classmethod
    async def process_create_or_update(
        cls, input: SparkGraphQLInput, info: strawberry.Info
    ) -> Model:
        """Process the create or update operation."""
        service = cls.with_input(input)
        await service.set_user_and_tenant(info)
        return await service.save()

    def set_input(self, input: SparkGraphQLInput) -> "BaseMutationService":
        """Set the input for the service."""
        self.input = input
        return self

    async def set_user_and_tenant(self, info: strawberry.Info) -> "BaseMutationService":
        """Set the user and tenant for the service."""
        self.info = info
        self.user = await self.get_user(info)
        self.is_spark_schema = self.is_spark_schema_request(info, user=self.user)
        tenant_id = getattr(self.input, "tenant_id", None)
        is_update = hasattr(self.input, "id") and self.input.id is not None
        if is_update:
            self.input.id = resolve_id_to_int(self.input.id)

        if self.is_spark_schema and tenant_id:
            # Spark user with explicit tenant_id
            self.tenant_id = await self._resolve_tenant_without_membership(tenant_id)
        elif self.is_spark_schema and is_update:
            # Spark user updating without tenant_id - use existing object's tenant
            model_class = self.get_model()
            existing_obj = await sync_to_async(model_class.objects.get)(
                id=self.input.id
            )
            self.tenant_id = getattr(existing_obj, "tenant_id", None)
        else:
            # Non-spark user or spark user creating without tenant_id
            tenant = await self.get_tenant(self.user, tenant_id)
            self.tenant_id = tenant.id
        return self

    def set_is_public(self, is_public: bool) -> "BaseMutationService":
        """Set the is public for the service."""
        self.is_public = is_public
        return self

    async def set_tenant_from_request_url_name(
        self, request_url_name: str | None = None
    ) -> "BaseMutationService":
        """Resolve tenant_id using a provided request_url_name value."""
        request_url_name = request_url_name or getattr(
            self.input, "request_url_name", None
        )
        if not request_url_name:
            raise GraphQLError("Tenant request url name is required.")

        try:
            tenant: Tenant = await sync_to_async(Tenant.objects.get)(
                request_url_name=request_url_name
            )
        except Tenant.DoesNotExist:
            raise GraphQLError("Tenant not found for the provided request url name.")

        self.tenant_id = tenant.id
        setattr(self.input, "tenant_id", tenant.id)
        return self

    def get_model(self) -> Model:
        """Get the model for the service."""
        raise NotImplementedError("Subclasses must implement this method.")

    async def validations(self):
        """Before save validations."""
        tenant_id = getattr(self.input, "tenant_id", None)
        if self.is_public and not tenant_id:
            raise GraphQLError("Tenant ID is required.")

        # Allow anonymous/public flows to skip role-based tenant restrictions
        if not self.user or isinstance(self.user, AnonymousUser):
            return

        role = getattr(self.user, "role", None)
        is_client = await role.is_client if role else False
        if (
            not self.is_public
            and not self.is_spark_schema
            and getattr(self.user, "role_id", None) != ROLE_ID.SparkAdmin
            and not is_client
            and tenant_id
        ):
            raise GraphQLError("Tenant ID should not be provided.")

    async def _resolve_tenant_without_membership(
        self, tenant_id: Union[str, int]
    ) -> int:
        """Resolve tenant ID for Spark schema requests without membership restrictions."""
        try:
            tenant_pk = resolve_id_to_int(tenant_id)
        except (TypeError, ValueError, GraphQLError):
            raise GraphQLError("Invalid tenant ID.")

        try:
            await sync_to_async(Tenant.objects.get)(id=tenant_pk)
        except Tenant.DoesNotExist:
            raise GraphQLError("Tenant not found.")

        return tenant_pk

    async def save(self) -> Model:
        """Save the model."""
        # validate the input
        await self.validations()

        # get the model
        model_class = self.get_model()
        is_update: bool = hasattr(self.input, "id") and self.input.id is not None
        if is_update:
            model = await sync_to_async(model_class.objects.get)(id=self.input.id)
            if self.user:
                setattr(model, "updated_by", self.user)
        else:
            model = model_class()
            if self.user:
                setattr(model, "created_by", self.user)
            if self.is_public and self.input.tenant_id:
                self.tenant_id = self.input.tenant_id

        # set the parameters
        params: dict[str, Any] = self.input.to_dict(["tenant_id", "id"])
        params = self._normalize_id_fields(params)
        for key, value in params.items():
            if not hasattr(model, key):
                continue
            if key == "store_number" and isinstance(value, str):
                value = value.strip() or None
            setattr(model, key, value)

        # set the tenant id
        if hasattr(model, "tenant_id"):
            setattr(model, "tenant_id", self.tenant_id)
        await sync_to_async(model.save)()
        return model

    @staticmethod
    def _normalize_id_fields(params: dict[str, Any]) -> dict[str, Any]:
        """Convert relay/global IDs into database IDs for *_id fields."""
        normalized: dict[str, Any] = {}
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, str) and value.strip() == "":
                continue
            if key == "id" or key.endswith("_id"):
                normalized[key] = resolve_id_to_int(value)
            else:
                normalized[key] = value
        return normalized


class EventMutationService(BaseMutationService):
    """Service for event mutations."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Event

    async def prepare_create(self, info: strawberry.Info) -> "EventMutationService":
        """Resolve tenant and default approved status for event creation."""
        user: User = await self.get_user(info)
        self.info = info
        self.user = user
        self.is_spark_schema = self.is_spark_schema_request(info, user=user)

        request_id = getattr(self.input, "request_id", None)
        tenant_id: int | None = None

        if request_id:
            try:
                request_id = resolve_id_to_int(request_id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid request ID.")

            self.input.request_id = request_id
            request: models.Request = await sync_to_async(models.Request.objects.get)(
                id=request_id
            )
            tenant_id = request.tenant_id

            is_spark_admin = await user.role.is_spark_admin
            if not self.is_spark_schema and not is_spark_admin:
                try:
                    await sync_to_async(user.get_tenant)(tenant_id=tenant_id)
                except Exception:
                    raise GraphQLError(
                        "You are not authorized to create events for this tenant."
                    )
        else:
            input_tenant_id = getattr(self.input, "tenant_id", None)
            if self.is_spark_schema and input_tenant_id:
                tenant_id = await self._resolve_tenant_without_membership(input_tenant_id)
                setattr(self.input, "tenant_id", tenant_id)
            else:
                resolved_tenant_id = input_tenant_id
                if input_tenant_id is not None:
                    try:
                        resolved_tenant_id = resolve_id_to_int(input_tenant_id)
                    except (TypeError, ValueError, GraphQLError):
                        raise GraphQLError("Invalid tenant ID.")
                tenant = await self.get_tenant(user, resolved_tenant_id)
                tenant_id = tenant.id

        if tenant_id is None:
            raise GraphQLError(
                "Tenant ID is required when creating an event without a request."
            )

        try:
            approved_status = await sync_to_async(models.EventStatus.objects.get)(
                slug="approved", tenant_id=tenant_id
            )
        except models.EventStatus.DoesNotExist:
            raise GraphQLError(
                "Approved event status not found. Please ensure you have a status with slug 'approved' for this tenant."
            )

        self.input.status_id = approved_status.id
        self.tenant_id = tenant_id
        return self

    def save_sync(self) -> models.Event:
        """Save an event synchronously so it can be composed inside transactions."""
        model_class = self.get_model()
        is_update: bool = hasattr(self.input, "id") and self.input.id is not None

        if is_update:
            model = model_class.objects.get(id=self.input.id)
            if self.user:
                setattr(model, "updated_by", self.user)
        else:
            model = model_class()
            if self.user:
                setattr(model, "created_by", self.user)
            if self.is_public and self.input.tenant_id:
                self.tenant_id = self.input.tenant_id

        params: dict[str, Any] = self.input.to_dict(["tenant_id", "id"])
        params = self._normalize_id_fields(params)
        for key, value in params.items():
            if not hasattr(model, key):
                continue
            setattr(model, key, value)

        if hasattr(model, "tenant_id"):
            setattr(model, "tenant_id", self.tenant_id)

        model.save()
        return model

    async def save(self) -> models.Event:
        """Save event using the synchronous helper."""
        await self.validations()
        return await sync_to_async(self.save_sync)()


async def _resolve_event_location(
    event: models.Event,
) -> models.Location | None:
    if not event.request_id:
        return None

    try:
        request: models.Request = await sync_to_async(
            models.Request.objects.select_related(
                "retailer__location", "distributor__location"
            ).get
        )(id=event.request_id)
    except models.Request.DoesNotExist:
        return None

    if request.retailer and request.retailer.location_id:
        return request.retailer.location
    if request.distributor and request.distributor.location_id:
        return request.distributor.location
    return None


async def _resolve_notification_group_ids(
    location: models.Location,
    tenant_id: int | None,
) -> list[int]:
    if not location:
        return []

    location_groups = models.NotificationGroupLocation.objects.filter(
        location_id=location.id,
        notification_group__state=False,
    ).values_list("notification_group_id", flat=True)

    if location.state_id:
        state_groups = models.NotificationGroupLocation.objects.filter(
            state_id=location.state_id,
            notification_group__state=True,
        ).values_list("notification_group_id", flat=True)
        state_group_ids = await sync_to_async(list)(state_groups.distinct())
    else:
        state_group_ids = []

    location_group_ids = await sync_to_async(list)(location_groups.distinct())
    if not state_group_ids:
        return location_group_ids

    return list(set(location_group_ids + state_group_ids))


async def _notify_notification_group_users_for_event(
    event: models.Event,
    location: models.Location | None,
) -> None:
    if not location:
        return

    group_ids = await _resolve_notification_group_ids(
        location=location,
        tenant_id=event.tenant_id,
    )
    if not group_ids:
        return

    to_emails = await sync_to_async(list)(
        models.NotificationGroupUser.objects.filter(
            notification_group_id__in=group_ids,
            user__is_active=True,
            user__tenanted_users__tenant_id=event.tenant_id,
            user__tenanted_users__is_active=True,
        )
        .exclude(user__email__isnull=True)
        .exclude(user__email="")
        .values_list("user__email", flat=True)
        .distinct()
    )
    if not to_emails:
        return

    mailer = EventApprovedNotificationMailer(
        event=event,
        location=location,
        to_emails=to_emails,
    )
    await sync_to_async(mailer.send)()


async def _notify_assigned_ambassadors_for_event_update(event_id: int) -> None:
    ambassador_jobs = await sync_to_async(list)(
        job_models.AmbassadorJob.objects.filter(job__event_id=event_id)
        .select_related(
            "ambassador",
            "ambassador__user",
            "job",
            "job__event",
            "job__event__timezone",
            "job__event__retailer",
            "job__event__retailer__location",
            "job__event__retailer__location__state",
            "tenant",
            "status",
            "rate",
        )
        .distinct()
    )

    for ambassador_job in ambassador_jobs:
        if not should_send_ambassador_event_email(ambassador_job):
            continue

        user = getattr(getattr(ambassador_job, "ambassador", None), "user", None)
        email = (getattr(user, "email", None) or "").strip()
        if not email:
            continue

        status_slug = (
            (getattr(getattr(ambassador_job, "status", None), "slug", None) or "")
            .strip()
            .lower()
        )
        if status_slug in {"pending", "apply"}:
            mailer_class = AmbassadorAppliedJobUpdatedMailer
        elif status_slug == "invited":
            mailer_class = AmbassadorInvitedJobUpdatedMailer
        else:
            mailer_class = AmbassadorJobUpdatedMailer

        mailer = mailer_class(
            ambassador_job=ambassador_job,
            to_emails=[email],
            recipient_first_name=(getattr(user, "first_name", None) or "").strip() or None,
        )
        await sync_to_async(mailer.send)()


async def _notify_assigned_ambassadors_for_suspended_event(event_id: int) -> None:
    ambassador_jobs = await sync_to_async(list)(
        job_models.AmbassadorJob.objects.filter(job__event_id=event_id)
        .select_related(
            "ambassador",
            "ambassador__user",
            "job",
            "job__event",
            "job__event__timezone",
            "job__event__retailer",
            "job__event__retailer__location",
            "job__event__retailer__location__state",
            "tenant",
            "status",
            "rate",
        )
        .distinct()
    )

    for ambassador_job in ambassador_jobs:
        if not should_send_ambassador_event_email(ambassador_job):
            continue

        user = getattr(getattr(ambassador_job, "ambassador", None), "user", None)
        email = (getattr(user, "email", None) or "").strip()
        if not email:
            continue

        mailer = AmbassadorEventSuspendedMailer(
            ambassador_job=ambassador_job,
            to_emails=[email],
            recipient_first_name=(getattr(user, "first_name", None) or "").strip() or None,
        )
        await sync_to_async(mailer.send)()


@strawberry.type
class EventMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_event(
        self,
        info: strawberry.Info,
        input: inputs.CreateEventInput,
    ) -> types.EventDetailResponse:
        try:
            service = EventMutationService.with_input(input)
            await service.prepare_create(info)
            event: models.Event = await service.save()
            return build_mutation_response(
                types.EventDetailResponse,
                success=True,
                message="Event created successfully.",
                input_obj=input,
                event=event,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.EventDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )
        except models.Request.DoesNotExist:
            return build_mutation_response(
                types.EventDetailResponse,
                success=False,
                message="Request not found.",
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_event_with_request(
        self,
        info: strawberry.Info,
        input: inputs.CreateEventWithRequestInput,
    ) -> types.EventWithRequestDetailResponse:
        try:
            request_input = input.request
            event_input = input.event

            if getattr(event_input, "request_id", None):
                raise GraphQLError(
                    "request_id should not be provided when creating an event with a nested request."
                )

            request_service = RequestWithDependenciesMutationService.with_input(
                request_input
            )
            await request_service.set_user_and_tenant(info)
            await request_service.validations()
            request_service.auto_approve = True

            request_params: dict[str, Any] = request_input.to_dict(
                ["tenant_id", "id", "details", "products"]
            )
            request_params = request_service._normalize_id_fields(request_params)

            event_service = EventMutationService.with_input(event_input)
            event_service.info = info
            event_service.user = request_service.user
            event_service.is_spark_schema = request_service.is_spark_schema

            event_tenant_id = getattr(event_input, "tenant_id", None)
            if event_tenant_id is not None:
                try:
                    event_tenant_id = resolve_id_to_int(event_tenant_id)
                except (TypeError, ValueError, GraphQLError):
                    raise GraphQLError("Invalid tenant ID.")

                if event_tenant_id != request_service.tenant_id:
                    raise GraphQLError(
                        "Event tenant must match the tenant resolved for the nested request."
                    )

                event_input.tenant_id = None

            approved_status = await sync_to_async(models.EventStatus.objects.get)(
                slug="approved", tenant_id=request_service.tenant_id
            )

            event_input.status_id = approved_status.id

            await event_service.validations()

            def _create_request_and_event() -> tuple[models.Request, models.Event]:
                with transaction.atomic():
                    request = request_service._save_sync(request_params)
                    event_service.tenant_id = request.tenant_id
                    event_service.input.request_id = request.id
                    event = event_service.save_sync()
                    return request, event

            request, event = await sync_to_async(_create_request_and_event)()

            request = await sync_to_async(
                models.Request.objects.select_related(
                    "tenant",
                    "timezone",
                    "request_type",
                    "retailer__location__state",
                    "distributor__location__state",
                ).get
            )(id=request.id)
            event = await sync_to_async(
                models.Event.objects.select_related(
                    "request",
                    "tenant",
                    "event_type",
                    "status",
                    "timezone",
                    "retailer",
                    "distributor",
                    "location",
                    "state",
                    "rmm_asigned",
                ).get
            )(id=event.id)

            return build_mutation_response(
                types.EventWithRequestDetailResponse,
                success=True,
                message="Event and request created successfully.",
                input_obj=input,
                request=request,
                event=event,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.EventWithRequestDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )
        except models.EventStatus.DoesNotExist:
            return build_mutation_response(
                types.EventWithRequestDetailResponse,
                success=False,
                message="Approved event status not found. Please ensure you have a status with slug 'approved' for this tenant.",
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_event_with_request(
        self,
        info: strawberry.Info,
        input: inputs.UpdateEventWithRequestInput,
    ) -> types.EventWithRequestDetailResponse:
        try:
            request_input = input.request
            event_input = input.event

            request_service = RequestMutationService.with_input(request_input)
            await request_service.set_user_and_tenant(info)
            await request_service.validations()

            event_tenant_id = getattr(event_input, "tenant_id", None)
            if event_tenant_id is not None:
                try:
                    event_tenant_id = resolve_id_to_int(event_tenant_id)
                except (TypeError, ValueError, GraphQLError):
                    raise GraphQLError("Invalid tenant ID.")

                if event_tenant_id != request_service.tenant_id:
                    raise GraphQLError(
                        "Event tenant must match the tenant resolved for the nested request."
                    )

                event_input.tenant_id = None

            event_service = EventMutationService.with_input(event_input)
            await event_service.set_user_and_tenant(info)
            await event_service.validations()

            if request_service.tenant_id != event_service.tenant_id:
                raise GraphQLError("Event and request must belong to the same tenant.")

            event_request_id = getattr(event_input, "request_id", None)
            if event_request_id is not None:
                try:
                    event_request_id = resolve_id_to_int(event_request_id)
                except (TypeError, ValueError, GraphQLError):
                    raise GraphQLError("Invalid request ID.")

                if event_request_id != request_input.id:
                    raise GraphQLError(
                        "Event request_id must match the nested request being updated."
                    )

            event_input.request_id = request_input.id

            def _update_request_and_event() -> tuple[models.Request, models.Event]:
                with transaction.atomic():
                    request = request_service.save_sync()
                    event_service.tenant_id = request.tenant_id
                    event_service.input.request_id = request.id
                    event = event_service.save_sync()
                    return request, event

            request, event = await sync_to_async(_update_request_and_event)()

            request = await sync_to_async(
                models.Request.objects.select_related(
                    "tenant",
                    "timezone",
                    "request_type",
                    "retailer__location__state",
                    "distributor__location__state",
                ).get
            )(id=request.id)
            event = await sync_to_async(
                models.Event.objects.select_related(
                    "request",
                    "tenant",
                    "event_type",
                    "status",
                    "timezone",
                    "retailer",
                    "distributor",
                    "location",
                    "state",
                    "rmm_asigned",
                ).get
            )(id=event.id)

            return build_mutation_response(
                types.EventWithRequestDetailResponse,
                success=True,
                message="Event and request updated successfully.",
                input_obj=input,
                request=request,
                event=event,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.EventWithRequestDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_event(
        self,
        info: strawberry.Info,
        input: inputs.UpdateEventInput,
    ) -> types.EventDetailResponse:
        try:
            event_id = resolve_id_to_int(input.id)
            original_event = await sync_to_async(models.Event.objects.get)(id=event_id)
            relevant_fields_before = {
                "date": original_event.date,
                "start_time": original_event.start_time,
                "end_time": original_event.end_time,
                "new_end_time": original_event.new_end_time,
                "address": original_event.address,
                "retailer_id": original_event.retailer_id,
            }
            event: models.Event = await EventMutationService.process_create_or_update(
                input=input, info=info
            )
            event = await sync_to_async(
                models.Event.objects.select_related("rmm_asigned").get
            )(id=event.id)
            relevant_fields_after = {
                "date": event.date,
                "start_time": event.start_time,
                "end_time": event.end_time,
                "new_end_time": event.new_end_time,
                "address": event.address,
                "retailer_id": event.retailer_id,
            }
            if relevant_fields_before != relevant_fields_after:
                await _notify_assigned_ambassadors_for_event_update(event.id)
            return build_mutation_response(
                types.EventDetailResponse,
                success=True,
                message="Event updated successfully.",
                input_obj=input,
                event=event,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.EventDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def suspend_event(
        self,
        info: strawberry.Info,
        input: inputs.SuspendEventInput,
    ) -> types.EventDetailResponse:
        """Suspend an event."""
        try:
            service = EventMutationService()
            user: User = await service.get_user(info)

            try:
                input.id = resolve_id_to_int(input.id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid event ID.")

            event: models.Event = await sync_to_async(models.Event.objects.get)(
                id=input.id
            )

            is_spark_admin = await user.role.is_spark_admin
            if not is_spark_admin:
                try:
                    await sync_to_async(user.get_tenant)(tenant_id=event.tenant_id)
                except Exception:
                    raise GraphQLError(
                        "You are not authorized to suspend events for this tenant."
                    )

            suspended_status = await sync_to_async(models.EventStatus.objects.get)(
                slug="suspended", tenant_id=event.tenant_id
            )
            event.status = suspended_status
            event.updated_by = user
            await sync_to_async(event.save)()
            await _notify_assigned_ambassadors_for_suspended_event(event.id)

            return build_mutation_response(
                types.EventDetailResponse,
                success=True,
                message="Event suspended successfully.",
                input_obj=input,
                event=event,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.EventDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )
        except models.Event.DoesNotExist:
            return build_mutation_response(
                types.EventDetailResponse,
                success=False,
                message="Event not found.",
                input_obj=input,
            )
        except models.EventStatus.DoesNotExist:
            return build_mutation_response(
                types.EventDetailResponse,
                success=False,
                message="Suspended event status not found. Please ensure you have a status with slug 'suspended' for this tenant.",
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def archive_event(
        self,
        info: strawberry.Info,
        input: inputs.ArchiveEventInput,
    ) -> types.EventDetailResponse:
        """Archive an event."""
        try:
            service = EventMutationService()
            user: User = await service.get_user(info)

            try:
                input.id = resolve_id_to_int(input.id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid event ID.")

            event: models.Event = await sync_to_async(models.Event.objects.get)(
                id=input.id
            )

            is_spark_admin = await user.role.is_spark_admin
            if not is_spark_admin:
                try:
                    await sync_to_async(user.get_tenant)(tenant_id=event.tenant_id)
                except Exception:
                    raise GraphQLError(
                        "You are not authorized to archive events for this tenant."
                    )

            archived_status = await sync_to_async(models.EventStatus.objects.get)(
                slug="archived", tenant_id=event.tenant_id
            )
            event.status = archived_status
            event.updated_by = user
            await sync_to_async(event.save)()

            return build_mutation_response(
                types.EventDetailResponse,
                success=True,
                message="Event archived successfully.",
                input_obj=input,
                event=event,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.EventDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )
        except models.Event.DoesNotExist:
            return build_mutation_response(
                types.EventDetailResponse,
                success=False,
                message="Event not found.",
                input_obj=input,
            )
        except models.EventStatus.DoesNotExist:
            return build_mutation_response(
                types.EventDetailResponse,
                success=False,
                message="Archived event status not found. Please ensure you have a status with slug 'archived' for this tenant.",
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def approve_event(
        self,
        info: strawberry.Info,
        input: inputs.ApproveEventInput,
    ) -> types.EventDetailResponse:
        """Approve an event."""
        try:
            service = EventMutationService()
            user: User = await service.get_user(info)

            try:
                input.id = resolve_id_to_int(input.id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid event ID.")

            event: models.Event = await sync_to_async(models.Event.objects.get)(
                id=input.id
            )

            is_spark_admin = await user.role.is_spark_admin
            if not is_spark_admin:
                try:
                    await sync_to_async(user.get_tenant)(tenant_id=event.tenant_id)
                except Exception:
                    raise GraphQLError(
                        "You are not authorized to approve events for this tenant."
                    )

            approved_status = await sync_to_async(models.EventStatus.objects.get)(
                slug="approved", tenant_id=event.tenant_id
            )
            event.status = approved_status
            event.updated_by = user
            await sync_to_async(event.save)()

            return build_mutation_response(
                types.EventDetailResponse,
                success=True,
                message="Event approved successfully.",
                input_obj=input,
                event=event,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.EventDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )
        except models.Event.DoesNotExist:
            return build_mutation_response(
                types.EventDetailResponse,
                success=False,
                message="Event not found.",
                input_obj=input,
            )
        except models.EventStatus.DoesNotExist:
            return build_mutation_response(
                types.EventDetailResponse,
                success=False,
                message="Approved event status not found. Please ensure you have a status with slug 'approved' for this tenant.",
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def decline_event(
        self,
        info: strawberry.Info,
        input: inputs.DeclineEventInput,
    ) -> types.EventDetailResponse:
        """Decline an event."""
        try:
            service = EventMutationService()
            user: User = await service.get_user(info)

            try:
                input.id = resolve_id_to_int(input.id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid event ID.")

            event: models.Event = await sync_to_async(models.Event.objects.get)(
                id=input.id
            )

            is_spark_admin = await user.role.is_spark_admin
            if not is_spark_admin:
                try:
                    await sync_to_async(user.get_tenant)(tenant_id=event.tenant_id)
                except Exception:
                    raise GraphQLError(
                        "You are not authorized to decline events for this tenant."
                    )

            declined_status = await sync_to_async(models.EventStatus.objects.get)(
                slug="declined", tenant_id=event.tenant_id
            )
            event.status = declined_status
            event.updated_by = user
            await sync_to_async(event.save)()

            return build_mutation_response(
                types.EventDetailResponse,
                success=True,
                message="Event declined successfully.",
                input_obj=input,
                event=event,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.EventDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )
        except models.Event.DoesNotExist:
            return build_mutation_response(
                types.EventDetailResponse,
                success=False,
                message="Event not found.",
                input_obj=input,
            )
        except models.EventStatus.DoesNotExist:
            return build_mutation_response(
                types.EventDetailResponse,
                success=False,
                message="Declined event status not found. Please ensure you have a status with slug 'declined' for this tenant.",
                input_obj=input,
            )


class EventTypeMutationService(BaseMutationService):
    """Service for event type mutations."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.EventType


@strawberry.type
class EventTypeMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_event_type(
        self,
        info: strawberry.Info,
        input: inputs.CreateEventTypeInput,
    ) -> types.EventTypeDetailResponse:
        """Create a new event type."""
        try:
            print("INPUT", input)
            event_type: models.EventType = (
                await EventTypeMutationService.process_create_or_update(
                    input=input,
                    info=info,
                )
            )
            return build_mutation_response(
                types.EventTypeDetailResponse,
                success=True,
                message="Event type created successfully.",
                input_obj=input,
                event_type=event_type,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.EventTypeDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_event_type(
        self,
        info: strawberry.Info,
        input: inputs.UpdateEventTypeInput,
    ) -> types.EventTypeDetailResponse:
        """Update an existing event type."""
        try:
            event_type: models.EventType = (
                await EventTypeMutationService.process_create_or_update(
                    input=input,
                    info=info,
                )
            )
            return build_mutation_response(
                types.EventTypeDetailResponse,
                success=True,
                message="Event type updated successfully.",
                input_obj=input,
                event_type=event_type,
            )

        except GraphQLError as e:
            return build_mutation_response(
                types.EventTypeDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )


class EventStatusMutationService(BaseMutationService):
    """Service for event status mutations."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.EventStatus


@strawberry.type
class EventStatusMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_event_status(
        self,
        info: strawberry.Info,
        input: inputs.CreateEventStatusInput,
    ) -> types.EventStatusDetailResponse:
        """Create a new event status."""
        try:
            event_status: models.EventStatus = (
                await EventStatusMutationService.process_create_or_update(
                    input=input,
                    info=info,
                )
            )
            return build_mutation_response(
                types.EventStatusDetailResponse,
                success=True,
                message="Event status created successfully.",
                input_obj=input,
                event_status=event_status,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.EventStatusDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_event_status(
        self,
        info: strawberry.Info,
        input: inputs.UpdateEventStatusInput,
    ) -> types.EventStatusDetailResponse:
        """Update an existing event status."""
        try:
            event_status: models.EventStatus = (
                await EventStatusMutationService.process_create_or_update(
                    input=input,
                    info=info,
                )
            )
            return build_mutation_response(
                types.EventStatusDetailResponse,
                success=True,
                message="Event status updated successfully.",
                input_obj=input,
                event_status=event_status,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.EventStatusDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )


class LocationMutationService(BaseMutationService):
    """Service for location mutations."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Location


@strawberry.type
class LocationMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_location(
        self,
        info: strawberry.Info,
        input: inputs.CreateLocationInput,
    ) -> types.LocationDetailResponse:
        """Create a new location."""
        try:
            location: models.Location = (
                await LocationMutationService.process_create_or_update(
                    input=input, info=info
                )
            )
            return build_mutation_response(
                types.LocationDetailResponse,
                success=True,
                message="Location created successfully.",
                input_obj=input,
                location=location,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.LocationDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_location(
        self,
        info: strawberry.Info,
        input: inputs.UpdateLocationInput,
    ) -> types.LocationDetailResponse:
        """Update an existing location."""
        try:
            location: models.Location = (
                await LocationMutationService.process_create_or_update(
                    input=input, info=info
                )
            )
            return build_mutation_response(
                types.LocationDetailResponse,
                success=True,
                message="Location updated successfully.",
                input_obj=input,
                location=location,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.LocationDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )


class ClientMutationService(BaseMutationService):
    """Service for client mutations."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Client


@strawberry.type
class ClientMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_client(
        self,
        info: strawberry.Info,
        input: inputs.CreateClientInput,
    ) -> types.ClientDetailResponse:
        """Create a new client."""
        try:
            client: models.Client = (
                await ClientMutationService.process_create_or_update(
                    input=input, info=info
                )
            )
            return build_mutation_response(
                types.ClientDetailResponse,
                success=True,
                message="Client created successfully.",
                input_obj=input,
                client=client,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.ClientDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_client(
        self,
        info: strawberry.Info,
        input: inputs.UpdateClientInput,
    ) -> types.ClientDetailResponse:
        """Update an existing client."""
        try:
            client: models.Client = (
                await ClientMutationService.process_create_or_update(
                    input=input, info=info
                )
            )
            return build_mutation_response(
                types.ClientDetailResponse,
                success=True,
                message="Client updated successfully.",
                input_obj=input,
                client=client,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.ClientDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )


class DistributorMutationService(BaseMutationService):
    """Service for distributor mutations."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Distributor


@strawberry.type
class DistributorMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_distributor(
        self,
        info: strawberry.Info,
        input: inputs.CreateDistributorInput,
    ) -> types.DistributorDetailResponse:
        try:
            """Create a new distributor."""
            distributor: models.Distributor = (
                await DistributorMutationService.process_create_or_update(
                    input=input, info=info
                )
            )
            return build_mutation_response(
                types.DistributorDetailResponse,
                success=True,
                message="Distributor created successfully.",
                input_obj=input,
                distributor=distributor,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.DistributorDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_distributor(
        self,
        info: strawberry.Info,
        input: inputs.UpdateDistributorInput,
    ) -> types.DistributorDetailResponse:
        """Update an existing distributor."""
        try:
            distributor: models.Distributor = (
                await DistributorMutationService.process_create_or_update(
                    input=input,
                    info=info,
                )
            )
            return build_mutation_response(
                types.DistributorDetailResponse,
                success=True,
                message="Distributor updated successfully.",
                input_obj=input,
                distributor=distributor,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.DistributorDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )


class RetailerMutationService(BaseMutationService):
    """Service for retailer mutations."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Retailer


@strawberry.type
class RetailerMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_retailer(
        self,
        info: strawberry.Info,
        input: inputs.CreateRetailerInput,
    ) -> types.RetailerDetailResponse:
        """Create a new retailer."""
        try:
            retailer: models.Retailer = (
                await RetailerMutationService.process_create_or_update(
                    input=input,
                    info=info,
                )
            )
            return build_mutation_response(
                types.RetailerDetailResponse,
                success=True,
                message="Retailer created successfully.",
                input_obj=input,
                retailer=retailer,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RetailerDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_retailer(
        self,
        info: strawberry.Info,
        input: inputs.UpdateRetailerInput,
    ) -> types.RetailerDetailResponse:
        """Update an existing retailer."""
        try:
            retailer: models.Retailer = (
                await RetailerMutationService.process_create_or_update(
                    input=input,
                    info=info,
                )
            )
            return build_mutation_response(
                types.RetailerDetailResponse,
                success=True,
                message="Retailer updated successfully.",
                input_obj=input,
                retailer=retailer,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RetailerDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )


class ProductTypeMutationService(BaseMutationService):
    """Service for product type mutations."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.ProductType


@strawberry.type
class ProductTypeMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_product_type(
        self,
        info: strawberry.Info,
        input: inputs.CreateProductTypeInput,
    ) -> types.ProductTypeDetailResponse:
        """Create a new product type."""
        try:
            product_type: models.ProductType = (
                await ProductTypeMutationService.process_create_or_update(
                    input=input, info=info
                )
            )
            return build_mutation_response(
                types.ProductTypeDetailResponse,
                success=True,
                message="Product type created successfully.",
                input_obj=input,
                product_type=product_type,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.ProductTypeDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_product_type(
        self,
        info: strawberry.Info,
        input: inputs.UpdateProductTypeInput,
    ) -> types.ProductTypeDetailResponse:
        """Update an existing product type."""
        try:
            product_type: models.ProductType = (
                await ProductTypeMutationService.process_create_or_update(
                    input=input, info=info
                )
            )
            return build_mutation_response(
                types.ProductTypeDetailResponse,
                success=True,
                message="Product type updated successfully.",
                input_obj=input,
                product_type=product_type,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.ProductTypeDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )


class ProductMutationService(BaseMutationService):
    """Service for product mutations."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Product


@strawberry.type
class ProductMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_product(
        self,
        info: strawberry.Info,
        input: inputs.CreateProductInput,
    ) -> types.ProductDetailResponse:
        """Create a new product."""
        if input.image:
            input.image = extract_blob_name_from_url(input.image)
        try:
            product: models.Product = (
                await ProductMutationService.process_create_or_update(
                    input=input, info=info
                )
            )
            return build_mutation_response(
                types.ProductDetailResponse,
                success=True,
                message="Product created successfully.",
                input_obj=input,
                product=product,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.ProductDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_product(
        self,
        info: strawberry.Info,
        input: inputs.UpdateProductInput,
    ) -> types.ProductDetailResponse:
        """Update an existing product."""
        old_image_path: str | None = None
        try:
            input.id = resolve_id_to_int(input.id)
        except (TypeError, ValueError, GraphQLError):
            return build_mutation_response(
                types.ProductDetailResponse,
                success=False,
                message="Invalid product ID.",
                input_obj=input,
            )

        if input.image is not None:
            try:
                existing_product: models.Product = await sync_to_async(
                    models.Product.objects.get
                )(id=input.id)
                old_image_path = (
                    existing_product.image.name if existing_product.image else None
                )
                input.image = extract_blob_name_from_url(input.image)
            except models.Product.DoesNotExist:
                return build_mutation_response(
                    types.ProductDetailResponse,
                    success=False,
                    message="Product not found.",
                    input_obj=input,
                )

        try:
            product: models.Product = (
                await ProductMutationService.process_create_or_update(
                    input=input, info=info
                )
            )

            new_image_path = product.image.name if product.image else None
            if new_image_path and old_image_path and new_image_path != old_image_path:
                delete_blob(old_image_path)

            return build_mutation_response(
                types.ProductDetailResponse,
                success=True,
                message="Product updated successfully.",
                input_obj=input,
                product=product,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.ProductDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def delete_product(
        self,
        info: strawberry.Info,
        input: inputs.DeleteProductInput,
    ) -> types.ProductDetailResponse:
        """Delete a product and its associated image in GCS."""
        service = ProductMutationService()
        try:
            user: User = await service.get_user(info)
            is_spark_request = service.is_spark_schema_request(info, user=user)

            try:
                input.id = resolve_id_to_int(input.id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid product ID.")

            try:
                product: models.Product = await sync_to_async(
                    models.Product.objects.get
                )(id=input.id)
            except models.Product.DoesNotExist:
                raise GraphQLError("Product not found.")

            if not is_spark_request:
                try:
                    await sync_to_async(user.get_tenant)(tenant_id=product.tenant_id)
                except Exception:
                    raise GraphQLError(
                        "You are not authorized to delete products for this tenant."
                    )

            image_path = product.image.name if product.image else None
            if image_path:
                delete_blob(image_path)

            await sync_to_async(product.delete)()
            return build_mutation_response(
                types.ProductDetailResponse,
                success=True,
                message="Product deleted successfully.",
                input_obj=input,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.ProductDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )
        except RestrictedError:
            return build_mutation_response(
                types.ProductDetailResponse,
                success=False,
                message="Cannot delete this product because it is in use.",
                input_obj=input,
            )
        except Exception as e:
            return build_mutation_response(
                types.ProductDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )


class RequestTypeMutationService(BaseMutationService):
    """Service for request type mutations."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.RequestType


@strawberry.type
class RequestTypeMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_request_type(
        self,
        info: strawberry.Info,
        input: inputs.CreateRequestTypeInput,
    ) -> types.RequestTypeDetailResponse:
        """Create a new request type."""
        try:
            request_type: models.RequestType = (
                await RequestTypeMutationService.process_create_or_update(
                    input=input, info=info
                )
            )
            return build_mutation_response(
                types.RequestTypeDetailResponse,
                success=True,
                message="Request type created successfully.",
                input_obj=input,
                request_type=request_type,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RequestTypeDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_request_type(
        self,
        info: strawberry.Info,
        input: inputs.UpdateRequestTypeInput,
    ) -> types.RequestTypeDetailResponse:
        """Update an existing request type."""
        try:
            request_type: models.RequestType = (
                await RequestTypeMutationService.process_create_or_update(
                    input=input, info=info
                )
            )
            return build_mutation_response(
                types.RequestTypeDetailResponse,
                success=True,
                message="Request type updated successfully.",
                input_obj=input,
                request_type=request_type,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RequestTypeDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )


class BillingEntityMutationService(BaseMutationService):
    """Service for billing entity mutations."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.BillingEntity


@strawberry.type
class BillingEntityMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_billing_entity(
        self,
        info: strawberry.Info,
        input: inputs.CreateBillingEntityInput,
    ) -> types.BillingEntityDetailResponse:
        """Create a new billing entity."""
        try:
            billing_entity: models.BillingEntity = (
                await BillingEntityMutationService.process_create_or_update(
                    input=input, info=info
                )
            )
            return build_mutation_response(
                types.BillingEntityDetailResponse,
                success=True,
                message="Billing entity created successfully.",
                input_obj=input,
                billing_entity=billing_entity,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.BillingEntityDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_billing_entity(
        self,
        info: strawberry.Info,
        input: inputs.UpdateBillingEntityInput,
    ) -> types.BillingEntityDetailResponse:
        """Update an existing billing entity."""
        try:
            billing_entity: models.BillingEntity = (
                await BillingEntityMutationService.process_create_or_update(
                    input=input, info=info
                )
            )
            return build_mutation_response(
                types.BillingEntityDetailResponse,
                success=True,
                message="Billing entity updated successfully.",
                input_obj=input,
                billing_entity=billing_entity,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.BillingEntityDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )


@strawberry.type
class RequestStatusMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_request_status(
        self,
        info: strawberry.Info,
        input: inputs.CreateRequestStatusInput,
    ) -> types.RequestStatusDetailResponse:
        """Create a new request status."""
        try:
            request_status: models.RequestStatus = (
                await RequestStatusMutationService.process_create_or_update(
                    input=input, info=info
                )
            )
            return build_mutation_response(
                types.RequestStatusDetailResponse,
                success=True,
                message="Request status created successfully.",
                input_obj=input,
                request_status=request_status,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RequestStatusDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_request_status(
        self,
        info: strawberry.Info,
        input: inputs.UpdateRequestStatusInput,
    ) -> types.RequestStatusDetailResponse:
        """Update an existing request status."""
        try:
            request_status: models.RequestStatus = (
                await RequestStatusMutationService.process_create_or_update(
                    input=input, info=info
                )
            )
            return build_mutation_response(
                types.RequestStatusDetailResponse,
                success=True,
                message="Request status updated successfully.",
                input_obj=input,
                request_status=request_status,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RequestStatusDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )


class RequestStatusMutationService(BaseMutationService):
    """Service for request status mutations"""

    def get_model(self) -> Model:
        """Get the model for the service"""
        return models.RequestStatus


class RequestMutationService(BaseMutationService):
    """Service for request mutations."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Request

    def _replace_request_products_sync(self, request: models.Request) -> None:
        """Replace request products synchronously when input provides a list."""
        products = getattr(self.input, "products", None)
        if products is None:
            return

        product_ids: list[int] = []
        for product_input in products:
            product_params = self._normalize_id_fields(product_input.to_dict())
            product_id = product_params.get("product_id")
            if product_id:
                product_ids.append(product_id)

        with transaction.atomic():
            models.RequestProduct.objects.filter(request_id=request.id).delete()
            for product_id in product_ids:
                request_product = models.RequestProduct(
                    request_id=request.id,
                    product_id=product_id,
                    tenant_id=request.tenant_id,
                )
                if self.user:
                    request_product.created_by = self.user
                    request_product.updated_by = self.user
                request_product.save()

    def _sync_request_store_manager(self, request: models.Request) -> None:
        """Reassign the selected store manager to the request when provided."""
        store_manager_id = getattr(self.input, "store_manager_id", None)
        if store_manager_id is None:
            return

        try:
            manager_id = resolve_id_to_int(store_manager_id)
        except (TypeError, ValueError, GraphQLError):
            raise GraphQLError("Invalid request store manager ID.")

        try:
            manager = models.RequestStoreManager.objects.get(id=manager_id)
        except models.RequestStoreManager.DoesNotExist:
            raise GraphQLError("Request store manager not found.")

        if manager.tenant_id and manager.tenant_id != request.tenant_id:
            raise GraphQLError("Request store manager belongs to a different tenant.")

        other_managers = models.RequestStoreManager.objects.filter(request_id=request.id)
        other_managers = other_managers.exclude(id=manager.id)
        if self.user:
            other_managers.update(request=None, updated_by=self.user)
        else:
            other_managers.update(request=None)

        manager.request = request
        if not manager.tenant_id:
            manager.tenant_id = request.tenant_id
        if self.user:
            manager.updated_by = self.user
        manager.save()

    async def _replace_request_products(self, request: models.Request) -> None:
        """Replace request products when input provides a list."""
        await sync_to_async(self._replace_request_products_sync)(request)

    def save_sync(self) -> models.Request:
        """Save a request synchronously so it can be composed inside transactions."""
        model_class = self.get_model()
        is_update: bool = hasattr(self.input, "id") and self.input.id is not None

        if is_update:
            model = model_class.objects.get(id=self.input.id)
            if self.user:
                setattr(model, "updated_by", self.user)
        else:
            model = model_class()
            if self.user:
                setattr(model, "created_by", self.user)
            if self.is_public and self.input.tenant_id:
                self.tenant_id = self.input.tenant_id

        params: dict[str, Any] = self.input.to_dict(["tenant_id", "id", "store_manager_id"])
        params = self._normalize_id_fields(params)
        for key, value in params.items():
            if not hasattr(model, key):
                continue
            if key == "store_number" and isinstance(value, str):
                value = value.strip() or None
            setattr(model, key, value)

        if hasattr(model, "tenant_id"):
            setattr(model, "tenant_id", self.tenant_id)

        model.save()
        self._replace_request_products_sync(model)
        self._sync_request_store_manager(model)
        return model

    async def save(self) -> Model:
        """Save request and update related products if provided."""
        await self.validations()
        return await sync_to_async(self.save_sync)()


class RequestStoreManagerMutationService(BaseMutationService):
    """Service for request store manager mutations."""

    _request: models.Request | None = None
    _manager: models.RequestStoreManager | None = None

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.RequestStoreManager

    async def _get_manager(self) -> models.RequestStoreManager | None:
        """Fetch the store manager if an id was provided."""
        if self._manager:
            return self._manager
        manager_id = getattr(self.input, "id", None)
        if not manager_id:
            return None
        try:
            manager_id = resolve_id_to_int(manager_id)
        except (TypeError, ValueError, GraphQLError):
            raise GraphQLError("Invalid store manager ID.")
        try:
            self._manager = await sync_to_async(
                models.RequestStoreManager.objects.select_related("request").get
            )(id=manager_id)
            return self._manager
        except models.RequestStoreManager.DoesNotExist:
            raise GraphQLError("Request store manager not found.")

    async def _get_request(self) -> models.Request:
        """Resolve the request linked to the manager."""
        if self._request:
            return self._request

        request_id = getattr(self.input, "request_id", None)
        if request_id:
            try:
                request_id = resolve_id_to_int(request_id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid request ID.")
            try:
                request = await sync_to_async(models.Request.objects.get)(id=request_id)
            except models.Request.DoesNotExist:
                raise GraphQLError("Request not found.")
        else:
            manager = await self._get_manager()
            if manager and manager.request_id:
                request = manager.request
                setattr(self.input, "request_id", manager.request_id)
            else:
                request = None

        self._request = request
        return request

    async def set_user_and_tenant(self, info: strawberry.Info) -> "BaseMutationService":
        """Set user/tenant using the related request instead of tenant_id input."""
        self.info = info
        self.user = await self.get_user(info)
        self.is_spark_schema = self.is_spark_schema_request(info, user=self.user)
        request = await self._get_request()

        if request:
            self.tenant_id = request.tenant_id
        else:
            tenant_id = getattr(self.input, "tenant_id", None)
            if self.is_spark_schema and tenant_id:
                tenant_id = await self._resolve_tenant_without_membership(tenant_id)
                setattr(self.input, "tenant_id", tenant_id)
            else:
                tenant = await self.get_tenant(self.user, tenant_id)
                tenant_id = tenant.id

            if tenant_id is None:
                raise GraphQLError(
                    "Tenant ID is required when creating/updating a store manager without a request."
                )

            self.tenant_id = tenant_id

        return self

    async def _ensure_tenant_access(self, tenant_id: int) -> bool:
        """Validate that the user can operate on the request's tenant."""
        role = getattr(self.user, "role", None)
        is_spark_admin = await role.is_spark_admin if role else False
        if self.is_spark_schema or is_spark_admin:
            return True

        try:
            await sync_to_async(self.user.get_tenant)(tenant_id=tenant_id)
        except Exception:
            raise GraphQLError(
                "You are not authorized to manage store managers for this tenant."
            )
        return False

    async def validations(self):
        """Validate request existence and tenant membership."""
        await super().validations()
        await self._get_manager()
        request = await self._get_request()
        tenant_id = request.tenant_id if request else self.tenant_id
        if tenant_id is None:
            raise GraphQLError("Tenant could not be resolved for store manager.")
        is_admin = await self._ensure_tenant_access(tenant_id)
        self.tenant_id = tenant_id

        manager = await self._get_manager()
        if (
            manager
            and manager.tenant_id
            and tenant_id
            and manager.tenant_id != tenant_id
            and not is_admin
        ):
            raise GraphQLError("You cannot move store managers to a different tenant.")


@strawberry.type
class PublicRequestMutations:
    @relay.mutation
    async def create_request_by_url(
        self,
        info: strawberry.Info,
        input: inputs.CreateRequestWithDependenciesInput,
        request_url_name: str,
    ) -> types.RequestDetailResponse:
        """Create a new request with dependencies by URL."""
        try:
            service: RequestWithDependenciesMutationService = (
                RequestWithDependenciesMutationService.with_input(input=input)
            )
            service.set_is_public(True)
            if not getattr(input, "tenant_id", None):
                await service.set_tenant_from_request_url_name(
                    request_url_name=request_url_name
                )
            request: models.Request = await service.save()
            request_with_relations: models.Request = await sync_to_async(
                models.Request.objects.select_related(
                    "tenant",
                    "timezone",
                    "request_type",
                    "retailer__location__state",
                    "distributor__location__state",
                ).get
            )(id=request.id)
            location = await _resolve_request_location(request_with_relations)
            await _notify_notification_group_users_for_request_created(
                request_with_relations, location, delay_seconds=0
            )
            await _notify_spark_admins_for_client_request(
                request_with_relations, location, delay_seconds=1
            )
            # Per-tenant state-based RMM routing (LD only today). The
            # helper sets request.rmm_asigned_id + returns the TO email
            # list (a single RMM, or every RMM in the table if the
            # state isn't covered).
            assigned_rmm, rmm_emails = await assign_rmm_for_request(
                request_with_relations, request_url_name
            )
            if rmm_emails:
                await _notify_rmm_for_request_created(
                    request_with_relations,
                    location,
                    rmm_emails,
                    assigned_rmm,
                )
                # Refresh the in-memory model so the requestor email
                # below picks up the freshly-set rmm_asigned (used in
                # the 'routed to {name}' line).
                request_with_relations = await sync_to_async(
                    models.Request.objects.select_related(
                        "tenant", "timezone", "request_type",
                        "retailer__location__state",
                        "distributor__location__state",
                        "rmm_asigned",
                    ).get
                )(id=request_with_relations.id)
            await _notify_requestor_for_request_created(
                request_with_relations, location, delay_seconds=2
            )
            return build_mutation_response(
                types.RequestDetailResponse,
                success=True,
                message="Request created successfully.",
                input_obj=input,
                request=request_with_relations,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RequestDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )


class RequestWithDependenciesMutationService(BaseMutationService):
    """Service for request with dependencies mutations."""

    auto_approve: bool = False

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Request

    def _save_sync(self, params: dict[str, Any]) -> models.Request:
        """Synchronous save method to handle transaction."""
        with transaction.atomic():
            store_manager_id = params.pop("store_manager_id", None)
            # Create the request
            request = models.Request(**params)
            if self.user:
                request.created_by = self.user

            if self.is_public and self.input.tenant_id:
                request.tenant_id = self.input.tenant_id
            elif self.tenant_id:
                request.tenant_id = self.tenant_id

            if self.is_public:
                pending_status = models.RequestStatus.objects.get_by_slug(
                    slug="pending", tenant=request.tenant_id
                )
                if not pending_status:
                    raise GraphQLError(
                        "Pending status not found. Please ensure you have a status with slug 'pending'."
                    )
                request.status = pending_status
            elif self.auto_approve:
                approval_status = models.RequestStatus.objects.get_by_slug(
                    slug="approved", tenant=request.tenant_id
                )
                if not approval_status:
                    raise GraphQLError(
                        "Approval status not found. Please ensure you have a status with slug 'approved'."
                    )
                request.status = approval_status
                if self.user:
                    request.approved_by = self.user
            else:
                pending_status = models.RequestStatus.objects.get_by_slug(
                    slug="pending", tenant=request.tenant_id
                )
                if not pending_status:
                    raise GraphQLError(
                        "Pending status not found. Please ensure you have a status with slug 'pending'."
                    )
                request.status = pending_status

            request.save()

            if store_manager_id:
                try:
                    manager = models.RequestStoreManager.objects.get(
                        id=store_manager_id
                    )
                except models.RequestStoreManager.DoesNotExist:
                    raise GraphQLError("Request store manager not found.")

                if manager.tenant_id and manager.tenant_id != request.tenant_id:
                    raise GraphQLError(
                        "Request store manager belongs to a different tenant."
                    )

                manager.request = request
                if not manager.tenant_id:
                    manager.tenant_id = request.tenant_id
                if self.user:
                    manager.updated_by = self.user
                manager.save()

            # Create details
            if self.input.details:
                for detail_input in self.input.details:
                    detail_params = detail_input.to_dict()
                    detail = models.RequestDetail(**detail_params)
                    detail.request = request
                    detail.tenant_id = request.tenant_id
                    if self.user:
                        detail.created_by = self.user
                    detail.save()

            # Create products
            if self.input.products:
                for product_input in self.input.products:
                    product_params = self._normalize_id_fields(product_input.to_dict())
                    product = models.RequestProduct(**product_params)
                    product.request = request
                    product.tenant_id = request.tenant_id
                    if self.user:
                        product.created_by = self.user
                    product.save()

            return request

    async def save(self) -> models.Request:
        """Save the request with dependencies."""
        # validate the input
        await self.validations()

        if self.user:
            role = getattr(self.user, "role", None)
            self.auto_approve = await role.is_client if role else False

        # set the parameters
        params: dict[str, Any] = self.input.to_dict(
            ["tenant_id", "id", "details", "products"]
        )
        params = self._normalize_id_fields(params)

        return await sync_to_async(self._save_sync)(params)


async def _resolve_request_location(
    request: models.Request,
) -> models.Location | None:
    def _get_location() -> models.Location | None:
        req = models.Request.objects.select_related(
            "retailer__location",
            "distributor__location",
        ).get(id=request.id)
        if req.retailer and req.retailer.location_id:
            return req.retailer.location
        if req.distributor and req.distributor.location_id:
            return req.distributor.location
        return None

    return await sync_to_async(_get_location)()


async def _notify_notification_group_users_for_request(
    request: models.Request,
    location: models.Location | None,
) -> None:
    if not location:
        return

    group_ids = await _resolve_notification_group_ids(
        location=location,
        tenant_id=request.tenant_id,
    )
    if not group_ids:
        return

    to_emails = await sync_to_async(list)(
        models.NotificationGroupUser.objects.filter(
            notification_group_id__in=group_ids,
            user__is_active=True,
            user__tenanted_users__tenant_id=request.tenant_id,
            user__tenanted_users__is_active=True,
        )
        .exclude(user__email__isnull=True)
        .exclude(user__email="")
        .values_list("user__email", flat=True)
        .distinct()
    )
    if not to_emails:
        return

    mailer = RequestorRequestApprovedMailer(
        request=request,
        location=location,
        to_emails=to_emails,
    )
    await sync_to_async(mailer.send)()


async def _notify_notification_group_users_for_request_created(
    request: models.Request,
    location: models.Location | None,
    delay_seconds: int | float | None = None,
) -> None:
    if not location:
        return

    group_ids = await _resolve_notification_group_ids(
        location=location,
        tenant_id=request.tenant_id,
    )
    if not group_ids:
        return

    recipients = await sync_to_async(list)(
        models.NotificationGroupUser.objects.filter(
            notification_group_id__in=group_ids,
            user__is_active=True,
            user__tenanted_users__tenant_id=request.tenant_id,
            user__tenanted_users__is_active=True,
        )
        .exclude(user__email__isnull=True)
        .exclude(user__email="")
        .values("user__email", "user__first_name", "user__last_name")
        .distinct()
    )
    if not recipients:
        return

    for recipient in recipients:
        recipient_email = (recipient.get("user__email") or "").strip()
        if not recipient_email:
            continue
        first_name = (recipient.get("user__first_name") or "").strip()
        last_name = (recipient.get("user__last_name") or "").strip()
        recipient_name = " ".join([part for part in [first_name, last_name] if part])
        if not recipient_name:
            recipient_name = recipient_email

        mailer = RequestCreatedNotificationMailer(
            request=request,
            location=location,
            to_emails=[recipient_email],
            recipient_name=recipient_name,
        )
        await sync_to_async(mailer.send)(delay_seconds=delay_seconds)


async def _notify_spark_admins_for_client_request(
    request: models.Request,
    location: models.Location | None,
    delay_seconds: int | float | None = None,
) -> None:
    return


async def _notify_rmm_for_request_created(
    request: models.Request,
    location: models.Location | None,
    to_emails: list[str],
    assigned_rmm,
) -> None:
    """Send the routing email to the territory's RMM(s) and CC the
    Ignite team."""
    rmm_first = (
        (getattr(assigned_rmm, "first_name", None) or "").strip()
        or (assigned_rmm.email.split("@")[0] if assigned_rmm else "team")
    )
    state_code = extract_state_code(getattr(request, "address", None))
    mailer = RmmAssignedRequestMailer(
        request=request,
        location=location,
        to_emails=to_emails,
        cc_emails=IGNITE_REVIEW_CC,
        rmm_first_name=rmm_first,
        state_code=state_code,
    )
    await sync_to_async(mailer.send)()


async def _notify_requestor_for_request_created(
    request: models.Request,
    location: models.Location | None,
    delay_seconds: int | float | None = None,
) -> None:
    requestor_email = await _resolve_requestor_email(request)
    if not requestor_email:
        return

    request.requestor_email = requestor_email
    mailer = RequestorRequestCreatedMailer(
        request=request,
        location=location,
        to_emails=[requestor_email],
    )
    await sync_to_async(mailer.send)(delay_seconds=delay_seconds)


def _get_request_review_copy_emails(exclude_email: str | None = None) -> list[str]:
    configured_emails = getattr(settings, "REQUEST_REVIEW_COPY_EMAILS", []) or []
    normalized_exclude = (exclude_email or "").strip().lower()
    unique_emails: list[str] = []
    seen_emails: set[str] = set()

    for email in configured_emails:
        normalized_email = (email or "").strip()
        if not normalized_email:
            continue
        key = normalized_email.lower()
        if key == normalized_exclude or key in seen_emails:
            continue
        seen_emails.add(key)
        unique_emails.append(normalized_email)

    return unique_emails


async def _notify_requestor_for_request_approved(
    request: models.Request,
    location: models.Location | None,
    delay_seconds: int | float | None = None,
) -> None:
    requestor_email = await _resolve_requestor_email(request)
    if not requestor_email:
        return

    request.requestor_email = requestor_email
    copy_emails = _get_request_review_copy_emails(exclude_email=requestor_email)
    mailer = RequestorRequestApprovedMailer(
        request=request,
        location=location,
        to_emails=[requestor_email],
        cc_emails=copy_emails,
    )
    await sync_to_async(mailer.send)(delay_seconds=delay_seconds)


async def _notify_requestor_for_request_declined(
    request: models.Request,
    location: models.Location | None,
    reviewed_by_name: str | None = None,
    reviewed_by_email: str | None = None,
    delay_seconds: int | float | None = None,
) -> None:
    requestor_email = await _resolve_requestor_email(request)
    if not requestor_email:
        return

    request.requestor_email = requestor_email
    copy_emails = _get_request_review_copy_emails(exclude_email=requestor_email)
    mailer = RequestorRequestDeclinedMailer(
        request=request,
        location=location,
        to_emails=[requestor_email],
        cc_emails=copy_emails,
        reviewed_by_name=reviewed_by_name,
        reviewed_by_email=reviewed_by_email,
    )
    await sync_to_async(mailer.send)(delay_seconds=delay_seconds)


async def _notify_requestor_for_request_auto_approved(
    request: models.Request,
    location: models.Location | None,
    delay_seconds: int | float | None = None,
) -> None:
    requestor_email = await _resolve_requestor_email(request)
    if not requestor_email:
        return

    request.requestor_email = requestor_email

    # Auto-approved requests should notify with the same approval email flow.
    await _notify_requestor_for_request_approved(
        request=request,
        location=location,
        delay_seconds=delay_seconds,
    )


async def _resolve_requestor_email(request: models.Request) -> str:
    requestor_email = (request.requestor_email or "").strip()
    if requestor_email:
        return requestor_email

    if not request.created_by_id:
        return ""

    requestor_email = await sync_to_async(
        lambda: (
            User.objects.filter(id=request.created_by_id)
            .values_list("email", flat=True)
            .first()
            or ""
        )
    )()
    return requestor_email.strip()


@strawberry.type
class RequestMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def request_batch_template_download_url(
        self,
        info: strawberry.Info,
        input: inputs.RequestBatchTemplateInput,
    ) -> types.RequestBatchTemplateResponse:
        """Generate and return a signed download URL for the batch request template."""
        try:
            service = RequestMutationService()
            user: User = await service.get_user(info)
            is_spark_request = service.is_spark_schema_request(info, user=user)

            input_tenant_id = getattr(input, "tenant_id", None)
            tenant_id: int

            if is_spark_request and input_tenant_id:
                tenant_id = await service._resolve_tenant_without_membership(
                    input_tenant_id
                )
            else:
                resolved_tenant_id: int | None = None
                if input_tenant_id not in (None, ""):
                    resolved_tenant_id = resolve_id_to_int(input_tenant_id)
                tenant = await service.get_tenant(user, resolved_tenant_id)
                tenant_id = tenant.id

            template_bytes = await sync_to_async(build_request_batch_template_xlsx)(
                tenant_id=tenant_id
            )
            timestamp = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
            blob_name = (
                f"requests/import-templates/tenant-{tenant_id}/"
                f"requests-import-template-{timestamp}.xlsx"
            )

            await sync_to_async(upload_bytes)(
                blob_name,
                template_bytes,
                content_type=(
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                ),
            )
            from utils.gcs import public_url
            file_url = public_url(blob_name) or ""

            return build_mutation_response(
                types.RequestBatchTemplateResponse,
                success=True,
                message="Template URL generated successfully.",
                input_obj=input,
                file_url=file_url,
            )
        except (GraphQLError, ValueError) as e:
            return build_mutation_response(
                types.RequestBatchTemplateResponse,
                success=False,
                message=str(e),
                input_obj=input,
                file_url=None,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def import_requests_batch(
        self,
        info: strawberry.Info,
        input: inputs.ImportRequestsBatchInput,
    ) -> types.RequestBatchImportResponse:
        """Import requests from an Excel file stored in GCS."""
        try:
            service = RequestMutationService()
            user: User = await service.get_user(info)
            is_spark_request = service.is_spark_schema_request(info, user=user)

            input_tenant_id = getattr(input, "tenant_id", None)
            tenant_id: int

            if is_spark_request and input_tenant_id:
                tenant_id = await service._resolve_tenant_without_membership(
                    input_tenant_id
                )
            else:
                resolved_tenant_id: int | None = None
                if input_tenant_id not in (None, ""):
                    resolved_tenant_id = resolve_id_to_int(input_tenant_id)
                tenant = await service.get_tenant(user, resolved_tenant_id)
                tenant_id = tenant.id

            default_timezone_id = (
                resolve_id_to_int(input.default_timezone_id)
                if input.default_timezone_id not in (None, "")
                else None
            )
            default_request_type_id = (
                resolve_id_to_int(input.default_request_type_id)
                if input.default_request_type_id not in (None, "")
                else None
            )

            blob_name = extract_blob_name_from_url(input.file)
            if not blob_name:
                raise GraphQLError("Invalid file path.")

            file_bytes = await sync_to_async(download_blob_bytes)(blob_name)
            if not file_bytes:
                raise GraphQLError("Batch file not found.")

            sheet_name: str | int = input.sheet_name
            if isinstance(sheet_name, str) and sheet_name.isdigit():
                sheet_name = int(sheet_name)

            result = await sync_to_async(import_requests_from_excel_bytes)(
                file_bytes=file_bytes,
                tenant_id=tenant_id,
                created_by_id=user.id,
                default_timezone_id=default_timezone_id,
                default_request_type_id=default_request_type_id,
                sheet_name=sheet_name,
                dry_run=input.dry_run,
                rollback_on_error=input.rollback_on_error,
            )

            rows = [
                types.RequestBatchRowResult(
                    row_number=row.row_number,
                    success=row.success,
                    message=row.message,
                    request_id=str(row.request_id) if row.request_id else None,
                    request_uuid=row.request_uuid,
                )
                for row in result.rows
            ]
            errors = [
                f"row {row.row_number}: {error_part.strip()}"
                for row in result.rows
                if (not row.success)
                and row.message != "Rolled back because another row failed."
                for error_part in str(row.message).split("|")
                if error_part.strip()
            ]
            if not errors and result.failed_count > 0:
                errors = [
                    f"row {row.row_number}: {row.message}"
                    for row in result.rows
                    if not row.success
                ]

            return build_mutation_response(
                types.RequestBatchImportResponse,
                success=result.failed_count == 0,
                message=(
                    "Batch validated successfully."
                    if input.dry_run and result.failed_count == 0
                    else "Batch validation finished with errors."
                    if input.dry_run
                    else "Batch failed and was rolled back."
                    if result.rolled_back
                    else "Batch imported successfully."
                    if result.failed_count == 0
                    else "Batch imported with row errors."
                ),
                input_obj=input,
                total_rows=result.total_rows,
                success_count=result.success_count,
                failed_count=result.failed_count,
                rolled_back=result.rolled_back,
                errors=errors,
                rows=rows,
            )
        except (GraphQLError, ValueError) as e:
            return build_mutation_response(
                types.RequestBatchImportResponse,
                success=False,
                message=str(e),
                input_obj=input,
                total_rows=0,
                success_count=0,
                failed_count=0,
                rolled_back=False,
                errors=[str(e)],
                rows=[],
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_request(
        self,
        info: strawberry.Info,
        input: inputs.CreateRequestInput,
    ) -> types.RequestDetailResponse:
        """Create a new request as an authenticated user."""
        try:
            service = RequestWithDependenciesMutationService.with_input(input)
            await service.set_user_and_tenant(info)
            request: models.Request = await service.save()
            request_with_relations: models.Request = await sync_to_async(
                models.Request.objects.select_related(
                    "tenant",
                    "timezone",
                    "request_type",
                    "retailer__location__state",
                    "distributor__location__state",
                ).get
            )(id=request.id)

            is_client = service.user is not None and await service.user.role.is_client
            if is_client:
                location = await _resolve_request_location(request_with_relations)
                await _notify_spark_admins_for_client_request(
                    request_with_relations, location
                )
                await _notify_requestor_for_request_auto_approved(
                    request_with_relations, location, delay_seconds=1
                )
            return build_mutation_response(
                types.RequestDetailResponse,
                success=True,
                message="Request created successfully.",
                input_obj=input,
                request=request_with_relations,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RequestDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_request(
        self,
        info: strawberry.Info,
        input: inputs.UpdateRequestInput,
    ) -> types.RequestDetailResponse:
        """Update an existing request."""
        try:
            request: models.Request = (
                await RequestMutationService.process_create_or_update(
                    input=input, info=info
                )
            )
            request = await sync_to_async(
                models.Request.objects.select_related("timezone", "rmm_asigned").get
            )(id=request.id)
            return build_mutation_response(
                types.RequestDetailResponse,
                success=True,
                message="Request updated successfully.",
                input_obj=input,
                request=request,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RequestDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def approve_request(
        self,
        info: strawberry.Info,
        input: inputs.ApproveRequestInput,
    ) -> types.ApproveRequestResponse:
        """Approve a request."""
        try:
            service: RequestMutationService = RequestMutationService()
            user: User = await service.get_user(info)
            if user.role_id == ROLE_ID.Ambassadors:
                raise GraphQLError("You are not authorized to approve requests.")

            try:
                input.id = resolve_id_to_int(input.id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid request ID.")

            # Get the request first to access its tenant
            request: models.Request = await sync_to_async(
                models.Request.objects.select_related(
                    "timezone",
                    "retailer__location__state",
                    "distributor__location__state",
                ).get
            )(id=input.id)
            # Get tenant using tenant_id to avoid async context issues with foreign key access
            tenant: Tenant = await sync_to_async(Tenant.objects.get)(
                id=request.tenant_id
            )

            # Verify user is a member of the request's tenant (unless user is SparkAdmin)
            is_spark_admin = await user.role.is_spark_admin
            if not is_spark_admin:
                try:
                    await sync_to_async(user.get_tenant)(tenant_id=tenant.id)
                except Exception:
                    raise GraphQLError(
                        "You are not authorized to approve requests for this tenant."
                    )

            # Get the approved status for this tenant
            approval_status = await sync_to_async(
                models.RequestStatus.objects.get_by_slug
            )(slug="approved", tenant=tenant.id)
            if not approval_status:
                raise GraphQLError(
                    "Approval status not found. Please ensure you have a status with slug 'approved'."
                )
            request.status = approval_status
            request.approved_by = user
            await sync_to_async(request.save)()

            location = await _resolve_request_location(request)
            await _notify_notification_group_users_for_request(request, location)
            await _notify_requestor_for_request_approved(request, location)

            return build_mutation_response(
                types.ApproveRequestResponse,
                success=True,
                message="Request approved successfully.",
                input_obj=input,
                request=request,
                event=None,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.ApproveRequestResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )
        except Exception as e:
            return build_mutation_response(
                types.ApproveRequestResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def decline_request(
        self,
        info: strawberry.Info,
        input: inputs.DeclineRequestInput,
    ) -> types.DeclineRequestResponse:
        """Decline a request."""
        try:
            service: RequestMutationService = RequestMutationService()
            user: User = await service.get_user(info)
            if user.role_id == ROLE_ID.Ambassadors:
                raise GraphQLError("You are not authorized to decline requests.")

            try:
                input.id = resolve_id_to_int(input.id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid request ID.")

            # Get the request first to access its tenant
            request: models.Request = await sync_to_async(
                models.Request.objects.select_related("timezone").get
            )(id=input.id)
            # Get tenant using tenant_id to avoid async context issues with foreign key access
            tenant: Tenant = await sync_to_async(Tenant.objects.get)(
                id=request.tenant_id
            )

            # Verify user is a member of the request's tenant (unless user is SparkAdmin)
            is_spark_admin = await user.role.is_spark_admin
            if not is_spark_admin:
                try:
                    await sync_to_async(user.get_tenant)(tenant_id=tenant.id)
                except Exception:
                    raise GraphQLError(
                        "You are not authorized to decline requests for this tenant."
                    )

            # Get the decline status for this tenant
            decline_status = await sync_to_async(
                models.RequestStatus.objects.get_by_slug
            )(slug="declined", tenant=tenant.id)
            if not decline_status:
                raise GraphQLError(
                    "Decline status not found. Please ensure you have a status with slug 'decline'."
                )
            request.status = decline_status
            request.decline_reason = input.decline_reason
            await sync_to_async(request.save)()
            location = await _resolve_request_location(request)
            reviewed_by_name = (user.get_full_name() or user.email or "").strip() or "-"
            reviewed_by_email = (user.email or "").strip() or "-"
            await _notify_requestor_for_request_declined(
                request=request,
                location=location,
                reviewed_by_name=reviewed_by_name,
                reviewed_by_email=reviewed_by_email,
            )

            return build_mutation_response(
                types.DeclineRequestResponse,
                success=True,
                message="Request declined successfully.",
                input_obj=input,
                request=request,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.DeclineRequestResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )
        except Exception as e:
            return build_mutation_response(
                types.DeclineRequestResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def upsert_request_reviewed(
        self,
        info: strawberry.Info,
        input: inputs.UpsertRequestReviewedInput,
    ) -> types.RequestDetailResponse:
        """Update request reviewed flag."""
        try:
            service: RequestMutationService = RequestMutationService()
            user: User = await service.get_user(info)
            if user.role_id == ROLE_ID.Ambassadors:
                raise GraphQLError(
                    "You are not authorized to update request review status."
                )

            try:
                input.id = resolve_id_to_int(input.id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid request ID.")

            request: models.Request = await sync_to_async(
                models.Request.objects.select_related("timezone").get
            )(id=input.id)

            tenant: Tenant = await sync_to_async(Tenant.objects.get)(
                id=request.tenant_id
            )

            is_spark_admin = await user.role.is_spark_admin
            if not is_spark_admin:
                try:
                    await sync_to_async(user.get_tenant)(tenant_id=tenant.id)
                except Exception:
                    raise GraphQLError(
                        "You are not authorized to update requests for this tenant."
                    )

            request.reviewed = input.reviewed
            request.updated_by = user
            await sync_to_async(request.save)()

            return build_mutation_response(
                types.RequestDetailResponse,
                success=True,
                message="Request review status updated successfully.",
                input_obj=input,
                request=request,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RequestDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )
        except Exception as e:
            return build_mutation_response(
                types.RequestDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )


@strawberry.type
class RequestStoreManagerMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_request_store_manager(
        self,
        info: strawberry.Info,
        input: inputs.CreateRequestStoreManagerInput,
    ) -> types.RequestStoreManagerDetailResponse:
        """Create a new request store manager."""
        service = RequestStoreManagerMutationService.with_input(input)
        try:
            await service.set_user_and_tenant(info)
            manager: models.RequestStoreManager = await service.save()
            return build_mutation_response(
                types.RequestStoreManagerDetailResponse,
                success=True,
                message="Request store manager created successfully.",
                input_obj=input,
                request_store_manager=manager,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RequestStoreManagerDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_request_store_manager(
        self,
        info: strawberry.Info,
        input: inputs.UpdateRequestStoreManagerInput,
    ) -> types.RequestStoreManagerDetailResponse:
        """Update an existing request store manager."""
        service = RequestStoreManagerMutationService.with_input(input)
        try:
            await service.set_user_and_tenant(info)
            manager: models.RequestStoreManager = await service.save()
            return build_mutation_response(
                types.RequestStoreManagerDetailResponse,
                success=True,
                message="Request store manager updated successfully.",
                input_obj=input,
                request_store_manager=manager,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RequestStoreManagerDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )


@strawberry.type
class TimeZoneMutations:
    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_timezone(
        self, info: strawberry.Info, input: inputs.CreateTimeZoneInput
    ) -> types.TimeZoneResponse:
        """Create a new timezone."""
        service = EventMutationService()
        user = await service.get_user(info)

        try:
            timezone = await sync_to_async(models.TimeZone.objects.create)(
                name=input.name,
                code=input.code,
                offset=input.offset,
                created_by=user,
            )
            return types.TimeZoneResponse(
                success=True,
                message="Timezone created successfully.",
                timezone=timezone,
            )
        except Exception as e:
            return types.TimeZoneResponse(
                success=False,
                message=f"Error creating timezone: {str(e)}",
            )

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_timezone(
        self, info: strawberry.Info, input: inputs.UpdateTimeZoneInput
    ) -> types.TimeZoneResponse:
        """Update an existing timezone."""
        service = EventMutationService()
        user = await service.get_user(info)

        try:
            timezone = await sync_to_async(models.TimeZone.objects.get)(id=input.id)

            timezone.name = input.name
            timezone.code = input.code
            timezone.offset = input.offset
            timezone.updated_by = user

            await sync_to_async(timezone.save)()

            return types.TimeZoneResponse(
                success=True,
                message="Timezone updated successfully.",
                timezone=timezone,
            )
        except models.TimeZone.DoesNotExist:
            return types.TimeZoneResponse(
                success=False,
                message="Timezone not found.",
            )
        except Exception as e:
            return types.TimeZoneResponse(
                success=False,
                message=f"Error updating timezone: {str(e)}",
            )
