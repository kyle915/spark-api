import strawberry
import datetime
import logging

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
    NoteMentionMailer,
)
from .routing import (
    assign_rmm_for_request,
    extract_state_code,
    IGNITE_REVIEW_CC,
    ROUTED_TENANT_SLUGS,
    suppress_cc,
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

logger = logging.getLogger(__name__)

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


@strawberry.input
class MergeEventsInput:
    """Fold duplicate events into a keeper. All ids must belong to
    ``tenant_id``; the keeper survives, the rest repoint + delete."""
    tenant_id: strawberry.ID
    keep_event_id: strawberry.ID
    merge_event_ids: list[strawberry.ID]
    client_mutation_id: strawberry.ID | None = None


@strawberry.type
class MergeEventsResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    deleted_events: int = 0
    deleted_requests: int = 0
    moved_summary: str = ""
    warnings: list[str] = strawberry.field(default_factory=list)


@strawberry.type
class EventMutations:
    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def merge_events(
        self, info: strawberry.Info, input: MergeEventsInput
    ) -> MergeEventsResponse:
        """Merge duplicate events (admin-only). Repoints every relation
        that targets Event (roster de-duped: a BA on both keeps the
        keeper's row), deletes the duplicates, and cleans up orphaned
        same-shape requests so the repair cron can't resurrect them.
        Transactional — any conflict rolls the whole merge back."""
        from utils.graphql.permissions import (
            _is_admin_access,
            resolve_request_user_access,
        )

        fail = lambda msg: MergeEventsResponse(  # noqa: E731
            success=False,
            message=msg,
            client_mutation_id=input.client_mutation_id,
        )

        user = info.context.request.user
        role_slug, is_staff, is_super, email = (
            await resolve_request_user_access(user)
        )
        if not _is_admin_access(role_slug, is_staff, is_super, email):
            return fail("Admins only.")

        try:
            tid = resolve_id_to_int(str(input.tenant_id))
            keep_id = resolve_id_to_int(str(input.keep_event_id))
            merge_ids = [
                resolve_id_to_int(str(i)) for i in input.merge_event_ids
            ]
        except Exception:  # noqa: BLE001
            return fail("Bad ids.")

        from events.dedupe import merge_events as _merge

        try:
            report = await sync_to_async(_merge)(
                tenant_id=tid,
                keep_event_id=keep_id,
                merge_event_ids=merge_ids,
            )
        except ValueError as exc:
            return fail(str(exc))
        except Exception:  # noqa: BLE001
            logger.exception("mergeEvents failed tenant=%s keep=%s", tid, keep_id)
            return fail(
                "Merge failed and was rolled back — nothing was changed."
            )

        moved = report["moved"]
        summary = (
            ", ".join(f"{k}: {v}" for k, v in sorted(moved.items()))
            or "nothing to move"
        )
        return MergeEventsResponse(
            success=True,
            message=(
                f"Merged {report['deleted_events']} duplicate(s) into the "
                f"keeper — {summary}."
            ),
            client_mutation_id=input.client_mutation_id,
            deleted_events=report["deleted_events"],
            deleted_requests=report["deleted_requests"],
            moved_summary=summary,
            warnings=report["warnings"],
        )

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

            # Parity with the public-form path: stamp the state from the
            # address + assign the territory RMM for INTERNALLY-created
            # events too (the "SCHEDULED" rows), so they show a Market in
            # the Tracker and land in the right RMM's linked-sheet view.
            # Assignment only — no territory email (an admin created this).
            # Best-effort: a routing miss must never fail event creation.
            try:
                from events.routing import route_request_sync
                from utils.sheets_mirror import upsert_request_row

                _assigned, _state_code, _routed = await sync_to_async(
                    route_request_sync
                )(request)
                if _routed:
                    # route_request_sync persists via .update() (no
                    # post_save), so re-sync the sheet once with the final
                    # state + RMM and refresh the request for the response.
                    request = await sync_to_async(
                        models.Request.objects.select_related(
                            "tenant",
                            "timezone",
                            "request_type",
                            "retailer__location__state",
                            "distributor__location__state",
                            "state",
                            "rmm_asigned",
                        ).get
                    )(id=request.id)
                    await sync_to_async(upsert_request_row)(request)
            except Exception:
                logger.warning(
                    "internal RMM routing failed for request=%s",
                    getattr(request, "id", None),
                    exc_info=True,
                )

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


async def _resolve_default_product_type_id(
    input: inputs.CreateProductInput, info: strawberry.Info
) -> int:
    """Get-or-create the per-tenant default "General" product type, returning
    its id. This lets a product be added with just a name — used by the
    simplified add-a-product flow and inline-add while building a recap. The
    tenant is resolved exactly the way the create path resolves it.
    """
    service = ProductMutationService.with_input(input)
    await service.set_user_and_tenant(info)

    @sync_to_async
    def _get_or_create() -> int:
        product_type, _ = models.ProductType.objects.get_or_create(
            tenant_id=service.tenant_id,
            name="General",
            defaults={"created_by": service.user},
        )
        return product_type.id

    return await _get_or_create()


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
            # Product type is optional — fall back to the tenant's default.
            if not input.product_type_id:
                input.product_type_id = await _resolve_default_product_type_id(
                    input, info
                )
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

        # product_type_id is optional on the shared input; on update, preserve
        # the product's current type when the caller omits it (don't null the
        # required FK). Falls back to the tenant default only if somehow unset.
        if not input.product_type_id:

            @sync_to_async
            def _existing_product_type_id() -> int | None:
                return (
                    models.Product.objects.filter(id=input.id)
                    .values_list("product_type_id", flat=True)
                    .first()
                )

            input.product_type_id = (
                await _existing_product_type_id()
            ) or await _resolve_default_product_type_id(input, info)

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
            # list (the RMMs whose territory matches the request state).
            # When no state can be resolved, returns (None, []) — we
            # then send an Ignite-only email so the team can re-route
            # manually instead of fanning the request out to every LD
            # reviewer (the old behavior, which caused REQ-925 to go to
            # Lauren when the address didn't parse a state).
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
            elif request_url_name in ROUTED_TENANT_SLUGS:
                # Tenant uses RMM routing but we couldn't determine a
                # state — surface this to the Ignite team only, with a
                # subject hint that it needs manual triage. No
                # client-side RMMs get spammed; whoever picks it up
                # forwards manually.
                await _notify_ignite_for_unroutable_request(
                    request_with_relations, location
                )
            # assign_rmm_for_request set the RMM but NOT request.state — and the
            # RMMs filter their sheet by the Market/State column, so external-
            # form requests (a shortened form with no state field) showed a
            # blank Market and fell out of their view. Stamp the state from the
            # address (and assign the RMM if assign_rmm couldn't, e.g. a tenant
            # routing via default_external_rmm), then re-sync the sheet row.
            # Best-effort: never fail the create.
            try:
                from events.routing import route_request_sync

                _a, _c, _routed = await sync_to_async(route_request_sync)(
                    request_with_relations
                )
                if _routed:
                    request_with_relations = await sync_to_async(
                        models.Request.objects.select_related(
                            "tenant", "timezone", "request_type",
                            "retailer__location__state",
                            "distributor__location__state",
                            "state", "rmm_asigned",
                        ).get
                    )(id=request_with_relations.id)
                    from utils.sheets_mirror import upsert_request_row

                    await sync_to_async(upsert_request_row)(request_with_relations)
            except Exception:
                logger.warning(
                    "external-form state stamp failed for request=%s",
                    getattr(request_with_relations, "id", None),
                    exc_info=True,
                )
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
            # notify_requestor is a create-time option, not a Request field.
            ["tenant_id", "id", "details", "products", "notify_requestor"]
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


async def _notify_ignite_for_unroutable_request(
    request: models.Request,
    location: models.Location | None,
) -> None:
    """Send an Ignite-only email when state-based routing falls through.

    Used when a tenant has territory routing configured (LD today) but
    we can't resolve a state from the request — typically an incomplete
    address. We send the same `RmmAssignedRequestMailer` envelope but
    to Ignite only, with a hint up top about manual triage. The email
    template renders the existing fields fine; the
    `unroutable_reason` context flag is read by the template to show
    a yellow callout above the buttons.
    """
    mailer = RmmAssignedRequestMailer(
        request=request,
        location=location,
        to_emails=IGNITE_REVIEW_CC,
        cc_emails=[],
        rmm_first_name="team",
        state_code=None,
    )
    # The mailer's envelope() context includes the request — we just
    # pull the subject through and let the template render normally.
    # Subject is prefixed by `RmmAssignedRequestMailer.envelope()` so
    # we don't need to override it here; the missing state_code line
    # in the email body already signals the issue clearly enough.
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


def _get_spark_admin_emails() -> list[str]:
    """Every active Spark admin's email. Used so any new admin we add
    is automatically copied on every request approval — no settings
    file edit, no code change. Hardcoded `IGNITE_REVIEW_CC` still
    fronts the list (consistency for the historical ops team), this
    just folds in anyone else with role=spark-admin."""
    from tenants.models import Role
    from django.contrib.auth import get_user_model

    User = get_user_model()
    try:
        emails = list(
            User.objects.filter(
                role__slug=Role.SPARK_ADMIN_SLUG,
                is_active=True,
            )
            .exclude(email__isnull=True)
            .exclude(email__exact="")
            .values_list("email", flat=True)
        )
    except Exception:
        return []
    return [e.strip() for e in emails if (e or "").strip()]


async def _get_spark_admin_emails_async() -> list[str]:
    return await sync_to_async(_get_spark_admin_emails)()


async def _notify_requestor_for_request_approved(
    request: models.Request,
    location: models.Location | None,
    delay_seconds: int | float | None = None,
    approver_email_fallback: str | None = None,
) -> None:
    requestor_email = await _resolve_requestor_email(request)
    if not requestor_email:
        return

    request.requestor_email = requestor_email
    # CC the Ignite ops team on every approval — events@, kyle@,
    # myriant@, nevena@, madison@ — plus every active Spark admin in
    # the DB so new admins get the paper trail automatically without
    # editing settings. Dedupes against the requestor's address so no
    # one CC's themselves.
    admin_emails = await _get_spark_admin_emails_async()
    normalized_exclude = requestor_email.strip().lower()
    copy_emails = suppress_cc(
        list(
            dict.fromkeys(
                _get_request_review_copy_emails(exclude_email=requestor_email)
                + [
                    e
                    for e in IGNITE_REVIEW_CC
                    if (e or "").strip().lower() != normalized_exclude
                ]
                + [
                    e
                    for e in admin_emails
                    if (e or "").strip().lower() != normalized_exclude
                ]
            )
        )
    )
    mailer = RequestorRequestApprovedMailer(
        request=request,
        location=location,
        to_emails=[requestor_email],
        cc_emails=copy_emails,
        approver_email_fallback=approver_email_fallback,
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
    copy_emails = suppress_cc(
        _get_request_review_copy_emails(exclude_email=requestor_email)
    )
    mailer = RequestorRequestDeclinedMailer(
        request=request,
        location=location,
        to_emails=[requestor_email],
        cc_emails=copy_emails,
        reviewed_by_name=reviewed_by_name,
        reviewed_by_email=reviewed_by_email,
    )
    await sync_to_async(mailer.send)(delay_seconds=delay_seconds)


async def _push_requestor_for_request_verdict(
    request: models.Request,
    *,
    approved: bool,
    decline_reason: str | None = None,
) -> None:
    """Push the approve / decline outcome to the requestor's mobile.

    Best-effort. Finds the User by the request's `requestor_email`
    (falls back to created_by) and fires the existing send-push helper
    via the django-rq queue. Email delivery is handled separately by
    `_notify_requestor_for_request_approved` / _declined; this is
    additive — mobile users who'd otherwise have to refresh the app
    get a real-time ping.

    Swallows every exception so a push hiccup doesn't roll back the
    approve/decline (the email + status change still ship).
    """
    try:
        from ambassadors.push import enqueue_push

        recipient_email = (
            getattr(request, "requestor_email", None)
            or ""
        ).strip().lower()
        recipient_user_id: int | None = None
        if recipient_email:
            recipient_user_id = await sync_to_async(
                lambda: User.objects.filter(email__iexact=recipient_email)
                .values_list("id", flat=True)
                .first()
            )()
        if not recipient_user_id:
            recipient_user_id = getattr(request, "created_by_id", None)
        if not recipient_user_id:
            return

        venue = (getattr(request, "retailer_name", None) or "").strip()
        name = (
            getattr(request, "name", None)
            or venue
            or f"Request R-{str(getattr(request, 'uuid', ''))[-4:].upper()}"
        )[:80]

        if approved:
            title = "Request approved"
            body = f"{name} is approved. Ignite ops will staff it."
        else:
            reason = (decline_reason or "").strip()
            title = "Request declined"
            body = (
                f"{name} was declined."
                + (f" Reason: {reason}" if reason else "")
            )[:200]

        enqueue_push(
            recipient_user_id,
            title=title,
            body=body,
            data={
                "screen": "request",
                "requestUuid": str(getattr(request, "uuid", "")),
                "verdict": "approved" if approved else "declined",
            },
        )
    except Exception:
        logger.exception(
            "push requestor verdict notify failed for request_id=%s",
            getattr(request, "id", None),
        )


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


async def _materialize_approved_event_for_request(
    request: models.Request,
    user: User | None,
) -> models.Event | None:
    """Materialize the approved Event (and its pending Job) for a request
    that has just been auto-approved.

    This is the shared escape hatch used by EVERY auto-approve path so an
    approved Request never ends up eventless — which would make it invisible
    to the Missing Recaps query and the recap event picker (both iterate
    Event rows), so no recap could ever be filed against it.

    Mirrors exactly what ``approve_request`` does after flipping the status:
      1. Idempotent guard — if an Event already exists for this request, do
         nothing and return it (so re-entry / a later approve_request is a
         no-op).
      2. Resolve the tenant's APPROVED EventStatus (slug="approved") null-
         safely — the same lookup ``approve_request`` /
         ``create_event_with_request`` use — so the Event lands as approved
         (NOT the tenant default "pending") and the Event detail page agrees
         with the Master Tracker. A tenant missing the row falls through to
         ``from_request``'s default handling rather than hard-failing.
      3. Create the Event via ``Event.objects.from_request(...)``.
      4. Create the Pending Job(s) via ``create_pending_jobs_for_request``.
         The Request post_save signal fired before the Event existed (it was
         a no-op then), so we do it explicitly here. Idempotent.

    Best-effort: every step is wrapped so a failure is logged but never
    blocks the create/approve mutation. Returns the Event (existing, newly
    created, or None on failure).
    """
    # 1. Idempotent: skip if an Event already exists for this request.
    event = await sync_to_async(
        lambda: models.Event.objects.filter(request_id=request.id)
        .order_by("-id")
        .first()
    )()
    if event is not None:
        return event

    # 2 + 3. Resolve the approved EventStatus and create the Event.
    try:
        event_approved_status = await sync_to_async(
            lambda: models.EventStatus.objects.filter(
                slug="approved", tenant_id=request.tenant_id
            )
            .order_by("id")
            .first()
        )()
        event = await models.Event.objects.from_request(
            request=request,
            created_by=user,
            status=event_approved_status,
        )
    except Exception as exc:
        # Loud + non-swallowing: log the request id AND the exception repr so
        # the real cause is visible in the run log (this used to be a quiet
        # best-effort catch). logger.exception also attaches the full
        # traceback. We still return None rather than re-raising so an
        # auto-approve / approve mutation isn't blocked by event
        # materialization — the surfaced log is enough to act on.
        logger.exception(
            "auto-approve: failed to materialize Event for request_id=%s: %r",
            request.id,
            exc,
        )
        event = None

    # 4. Create the Pending Job(s) now that the Event exists. Idempotent +
    # best-effort; runs even if event creation above raised so a partially
    # set-up request still gets retried on the next pass.
    try:
        from .signals import create_pending_jobs_for_request

        await sync_to_async(create_pending_jobs_for_request)(request)
    except Exception:
        logger.exception(
            "auto-approve: failed to create pending job for request_id=%s",
            request.id,
        )

    return event


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
            file_name = f"spark-bulk-template-{timestamp}.xlsx"

            # Inline path — no GCS dependency. Front-end decodes the
            # base64 into a Blob and triggers a browser download.
            # Much faster (one round-trip instead of two) and works
            # even when the service account doesn't have
            # storage.objects.create on the bucket.
            import base64

            file_base64 = base64.b64encode(template_bytes).decode("ascii")

            # Best-effort GCS mirror — keeps the legacy `file_url`
            # path alive for any caller still reading it, but
            # failure is non-fatal now.
            file_url: str | None = None
            try:
                blob_name = (
                    f"requests/import-templates/tenant-{tenant_id}/"
                    f"requests-import-template-{timestamp}.xlsx"
                )
                await sync_to_async(upload_bytes)(
                    blob_name,
                    template_bytes,
                    content_type=(
                        "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet"
                    ),
                )
                from utils.gcs import public_url

                file_url = public_url(blob_name) or None
            except Exception as exc:  # noqa: BLE001
                import logging

                logging.getLogger(__name__).info(
                    "skipping GCS mirror for request batch template "
                    "(falling back to inline base64): %s",
                    exc,
                )

            return build_mutation_response(
                types.RequestBatchTemplateResponse,
                success=True,
                message="Template ready.",
                input_obj=input,
                file_url=file_url,
                file_base64=file_base64,
                file_name=file_name,
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
                    skipped=row.skipped,
                )
                for row in result.rows
            ]
            errors = [
                f"row {row.row_number}: {error_part.strip()}"
                for row in result.rows
                if (not row.success)
                and not row.skipped
                and row.message != "Rolled back because another row failed."
                for error_part in str(row.message).split("|")
                if error_part.strip()
            ]
            if not errors and result.failed_count > 0:
                errors = [
                    f"row {row.row_number}: {row.message}"
                    for row in result.rows
                    if not row.success and not row.skipped
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
                skipped_count=result.skipped_count,
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

            # Audit log: first entry in the request's timeline.
            try:
                await sync_to_async(models.RequestActivityLog.objects.create)(
                    tenant=request_with_relations.tenant,
                    request=request_with_relations,
                    kind=models.RequestActivityLog.KIND_CREATED,
                    actor_user=service.user if getattr(service.user, "id", None) else None,
                    summary=(
                        "Request created"
                        + (
                            f" for {request_with_relations.retailer_name}"
                            if request_with_relations.retailer_name
                            else ""
                        )
                    ),
                    metadata={},
                )
            except Exception:
                pass

            is_client = service.user is not None and await service.user.role.is_client
            if is_client:
                # The client self-serve path auto-approves the request (see
                # RequestWithDependenciesMutationService.save → auto_approve),
                # so it MUST also materialize the approved Event + Pending Job
                # — exactly like the admin branch below and approve_request do.
                # Without this the request is approved-but-eventless and is
                # invisible to the Missing Recaps query and the recap event
                # picker (both iterate Event rows), so no recap can ever be
                # filed. Best-effort: never blocks the client-facing emails.
                await _materialize_approved_event_for_request(
                    request_with_relations, service.user
                )

                location = await _resolve_request_location(request_with_relations)
                await _notify_spark_admins_for_client_request(
                    request_with_relations, location
                )
                await _notify_requestor_for_request_auto_approved(
                    request_with_relations, location, delay_seconds=1
                )
            else:
                # Admin log-event flow: skip the RMM approval cycle and
                # land the request straight on the Master Tracker as
                # approved. Mirrors the moves approve_request does —
                # status flip + materialize an Event row — but without
                # the requestor email (admin is creating on someone
                # else's behalf, no point sending self a notification).
                try:
                    approved_status = await sync_to_async(
                        models.RequestStatus.objects.get_by_slug
                    )(slug="approved", tenant=request_with_relations.tenant_id)
                    if approved_status:
                        request_with_relations.status = approved_status
                        request_with_relations.approved_by = service.user
                        await sync_to_async(request_with_relations.save)()
                        try:
                            await sync_to_async(
                                models.RequestActivityLog.objects.create
                            )(
                                tenant=request_with_relations.tenant,
                                request=request_with_relations,
                                kind=models.RequestActivityLog.KIND_STATUS_CHANGED,
                                actor_user=(
                                    service.user
                                    if getattr(service.user, "id", None)
                                    else None
                                ),
                                summary="Logged by admin · auto-approved",
                                metadata={"from": "pending", "to": "approved"},
                            )
                        except Exception:
                            pass
                        # Materialize the approved Event + Pending Job so the
                        # activation lands on the Master Tracker as approved
                        # and shows Assign-BA / Post-to-board. Shared with the
                        # client self-serve branch above and approve_request —
                        # idempotent + best-effort, never blocks the create.
                        await _materialize_approved_event_for_request(
                            request_with_relations, service.user
                        )
                except Exception:
                    # Don't fail the whole create if approval steps
                    # blow up — admin can still approve manually.
                    pass

            # When the admin ticked "email the client" on the create form,
            # send the same auto-approved confirmation the client self-serve
            # flow sends. Default is silent (admin creates on their behalf).
            if not is_client and getattr(input, "notify_requestor", None):
                try:
                    location = await _resolve_request_location(
                        request_with_relations
                    )
                    await _notify_requestor_for_request_auto_approved(
                        request_with_relations, location, delay_seconds=1
                    )
                except Exception:
                    pass

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
    async def bulk_clone_request(
        self,
        info: strawberry.Info,
        input: inputs.BulkCloneRequestInput,
    ) -> types.BulkCloneRequestResponse:
        """Clone an existing request N times with per-copy date (and
        optional venue) overrides.

        What gets copied from source per clone:
          - name, address, request_type, distributor, retailer,
            timezone, notes, store_number, billing_entity, client,
            rmm_asigned, coordinates, retailer_*/distributor_*/
            client_*/store_manager_* denormalized fields, location,
            state, tenant
        What's per-copy:
          - date (required), start_time, end_time (default to source's)
          - retailer / store_number / address (default to source's)
        What's NOT copied (intentional):
          - id, uuid (autogenerated)
          - status (resets to default per-tenant via save())
          - reviewed, approved_by (fresh approvals lifecycle)
          - created_at, updated_at (autogenerated)
          - RequestProduct + RequestDetail rows (admin re-adds as
            needed; common case is product picker survives via
            duplication of the source's notes/instructions)

        Cross-tenant guard: caller must have access to the source's
        tenant. Tenant inferred from source — clones land in the
        same tenant.
        """
        from datetime import datetime as _datetime, timezone as _tz
        from events import models as _evm

        if not input.copies:
            return build_mutation_response(
                types.BulkCloneRequestResponse,
                success=False,
                message="At least one copy is required.",
                input_obj=input,
            )
        if len(input.copies) > 50:
            return build_mutation_response(
                types.BulkCloneRequestResponse,
                success=False,
                message=(
                    "Bulk clone is limited to 50 copies per call. "
                    "Split larger campaigns across multiple submissions."
                ),
                input_obj=input,
            )

        try:
            source_pk = resolve_id_to_int(input.source_request_id)
        except (TypeError, ValueError, GraphQLError):
            return build_mutation_response(
                types.BulkCloneRequestResponse,
                success=False,
                message="Invalid source_request_id.",
                input_obj=input,
            )

        # Pull the acting user once outside the sync block so we can
        # attribute each clone's audit-log entry to them. None is fine
        # for system-initiated paths.
        try:
            _service = EventMutationService()
            actor_user = await _service.get_user(info)
        except Exception:
            actor_user = None

        # ISO parser shared across all copies. Accepts date-only
        # (YYYY-MM-DD) and full ISO datetime. Date-only gets midnight
        # UTC to match the source's typical pattern.
        def _parse_iso(raw: str | None) -> _datetime | None:
            if not raw:
                return None
            s = raw.strip()
            if not s:
                return None
            try:
                dt = _datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                try:
                    dt = _datetime.strptime(s[:10], "%Y-%m-%d")
                except ValueError as exc:
                    raise GraphQLError(
                        f"Could not parse date {raw!r} — expected ISO format."
                    ) from exc
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tz.utc)
            return dt

        @sync_to_async
        def _do_clone():
            try:
                source = _evm.Request.objects.select_related(
                    "request_type",
                    "distributor",
                    "retailer",
                    "timezone",
                    "tenant",
                    "client",
                    "billing_entity",
                    "rmm_asigned",
                    "location",
                    "state",
                ).get(pk=source_pk)
            except _evm.Request.DoesNotExist:
                raise GraphQLError("Source request not found.")

            created_uuids: list[str] = []
            for idx, copy in enumerate(input.copies):
                date_dt = _parse_iso(copy.date_iso)
                if not date_dt:
                    raise GraphQLError(
                        f"Copy {idx + 1}: dateIso is required."
                    )
                start_dt = (
                    _parse_iso(copy.start_time_iso) or source.start_time
                )
                end_dt = _parse_iso(copy.end_time_iso) or source.end_time

                retailer = source.retailer
                if copy.retailer_id not in (None, ""):
                    try:
                        retailer_pk = resolve_id_to_int(copy.retailer_id)
                        retailer = _evm.Retailer.objects.filter(
                            pk=retailer_pk
                        ).first()
                    except (TypeError, ValueError, GraphQLError):
                        raise GraphQLError(
                            f"Copy {idx + 1}: invalid retailerId."
                        )
                    if not retailer:
                        raise GraphQLError(
                            f"Copy {idx + 1}: retailer not found."
                        )
                    # Cross-tenant guard: retailers are tenant-scoped;
                    # the cloned request can't pull a retailer from
                    # another tenant.
                    if (
                        getattr(retailer, "tenant_id", None) is not None
                        and retailer.tenant_id != source.tenant_id
                    ):
                        raise GraphQLError(
                            f"Copy {idx + 1}: retailer belongs to a "
                            f"different tenant."
                        )

                clone = _evm.Request(
                    name=source.name,
                    date=date_dt,
                    start_time=start_dt,
                    end_time=end_dt,
                    address=(copy.address or source.address),
                    notes=source.notes,
                    store_number=(copy.store_number or source.store_number),
                    coordinates=list(source.coordinates or []),
                    client_name=source.client_name,
                    client_email=source.client_email,
                    distributor_name=source.distributor_name,
                    distributor_email=source.distributor_email,
                    retailer_name=source.retailer_name,
                    retailer_address=source.retailer_address,
                    retailer_store_contact=source.retailer_store_contact,
                    store_manager_name=source.store_manager_name,
                    store_manager_phone=source.store_manager_phone,
                    timezone=source.timezone,
                    client=source.client,
                    distributor=source.distributor,
                    retailer=retailer,
                    request_type=source.request_type,
                    tenant=source.tenant,
                    billing_entity=source.billing_entity,
                    rmm_asigned=source.rmm_asigned,
                    location=source.location,
                    state=source.state,
                    created_by=source.created_by,
                    requestor_email=source.requestor_email,
                )
                clone.save()
                created_uuids.append(str(clone.uuid))
                # Audit log: each clone gets a "cloned_from" entry
                # pointing at the source. Best-effort; never raises.
                try:
                    _evm.RequestActivityLog.objects.create(
                        tenant=clone.tenant,
                        request=clone,
                        kind=_evm.RequestActivityLog.KIND_CLONED_FROM,
                        actor_user=actor_user
                        if getattr(actor_user, "id", None)
                        else None,
                        summary=f"Cloned from {str(source.uuid)[:8]}",
                        metadata={"source_request_uuid": str(source.uuid)},
                    )
                except Exception:
                    pass
            return created_uuids

        try:
            created_uuids = await _do_clone()
        except GraphQLError as e:
            return build_mutation_response(
                types.BulkCloneRequestResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

        return build_mutation_response(
            types.BulkCloneRequestResponse,
            success=True,
            message=(
                f"Cloned {len(created_uuids)} request"
                f"{'s' if len(created_uuids) != 1 else ''} from the source."
            ),
            input_obj=input,
            created_count=len(created_uuids),
            created_uuids=created_uuids,
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
            prev_status_slug = ""
            try:
                if request.status_id:
                    prev_status_obj = await sync_to_async(
                        lambda: models.RequestStatus.objects.filter(
                            id=request.status_id
                        ).first()
                    )()
                    prev_status_slug = (
                        getattr(prev_status_obj, "slug", "") or ""
                    )
            except Exception:
                prev_status_slug = ""

            request.status = approval_status
            request.approved_by = user
            await sync_to_async(request.save)()

            # Audit log: capture the status transition for the timeline.
            try:
                await sync_to_async(models.RequestActivityLog.objects.create)(
                    tenant=request.tenant,
                    request=request,
                    kind=models.RequestActivityLog.KIND_STATUS_CHANGED,
                    actor_user=user if getattr(user, "id", None) else None,
                    summary=f"Status: {prev_status_slug or '—'} → approved",
                    metadata={"from": prev_status_slug, "to": "approved"},
                )
            except Exception:
                pass

            # Materialize a real Event row (+ its Pending Job) so Ignite ops
            # can staff against it. The approved email promises "Ignite staffs
            # a BA + calendar invite", which requires an Event in the
            # operational pipeline. Shared with the createRequest auto-approve
            # paths (client self-serve + admin log-event) — idempotent (skips
            # if an Event already exists) + best-effort (never blocks approval).
            event = await _materialize_approved_event_for_request(request, user)

            location = await _resolve_request_location(request)
            await _notify_notification_group_users_for_request(request, location)
            await _notify_requestor_for_request_approved(request, location)
            await _push_requestor_for_request_verdict(request, approved=True)

            return build_mutation_response(
                types.ApproveRequestResponse,
                success=True,
                message="Request approved successfully.",
                input_obj=input,
                request=request,
                event=event,
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
            prev_status_slug = ""
            try:
                if request.status_id:
                    prev_status_obj = await sync_to_async(
                        lambda: models.RequestStatus.objects.filter(
                            id=request.status_id
                        ).first()
                    )()
                    prev_status_slug = (
                        getattr(prev_status_obj, "slug", "") or ""
                    )
            except Exception:
                prev_status_slug = ""

            request.status = decline_status
            request.decline_reason = input.decline_reason
            await sync_to_async(request.save)()

            # Audit log: capture the decline transition + reason.
            try:
                await sync_to_async(models.RequestActivityLog.objects.create)(
                    tenant=request.tenant,
                    request=request,
                    kind=models.RequestActivityLog.KIND_STATUS_CHANGED,
                    actor_user=user if getattr(user, "id", None) else None,
                    summary=f"Status: {prev_status_slug or '—'} → declined",
                    metadata={
                        "from": prev_status_slug,
                        "to": "declined",
                        "decline_reason": (input.decline_reason or "")[:500],
                    },
                )
            except Exception:
                pass
            location = await _resolve_request_location(request)
            reviewed_by_name = (user.get_full_name() or user.email or "").strip() or "-"
            reviewed_by_email = (user.email or "").strip() or "-"
            await _notify_requestor_for_request_declined(
                request=request,
                location=location,
                reviewed_by_name=reviewed_by_name,
                reviewed_by_email=reviewed_by_email,
            )
            await _push_requestor_for_request_verdict(
                request,
                approved=False,
                decline_reason=input.decline_reason,
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
    async def delete_request(
        self,
        info: strawberry.Info,
        input: inputs.DeleteRequestInput,
    ) -> types.DeleteRequestResponse:
        """Soft-delete a request.

        Sets `deleted_at` so the request disappears from lists, detail
        pages, and exports. The row stays in the DB — its activity log
        and any FK-linked events / recaps survive. To restore, an admin
        can NULL out deleted_at directly in the DB (no UI for that yet).

        Auth: spark-admin or client role that owns the tenant.
        Ambassadors are blocked.
        """
        try:
            service: RequestMutationService = RequestMutationService()
            user: User = await service.get_user(info)
            if user.role_id == ROLE_ID.Ambassadors:
                raise GraphQLError("You are not authorized to delete requests.")

            try:
                request_pk = resolve_id_to_int(input.id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid request ID.")

            request: models.Request = await sync_to_async(
                models.Request.objects.select_related("tenant").get
            )(id=request_pk)

            # Client-role users can only delete in their own tenant.
            is_spark_admin = await user.role.is_spark_admin
            if not is_spark_admin:
                try:
                    await sync_to_async(user.get_tenant)(tenant_id=request.tenant_id)
                except Exception:
                    raise GraphQLError(
                        "You are not authorized to delete requests for this tenant."
                    )

            if request.deleted_at is not None:
                return build_mutation_response(
                    types.DeleteRequestResponse,
                    success=False,
                    message="This request is already deleted.",
                    input_obj=input,
                    deleted_request_uuid=str(request.uuid),
                )

            from django.utils import timezone as _tz
            request.deleted_at = _tz.now()
            request.updated_by = user
            await sync_to_async(request.save)(
                update_fields=["deleted_at", "updated_by", "updated_at"]
            )

            # Close any jobs hanging off this request's events so the deleted
            # gig also drops off the BA job board (which filters
            # ongoing=True/closed=False) — the jobs queryset already hides
            # deleted-request jobs from the admin list, but closing them keeps
            # the board + job state honest. Best-effort; never fail the delete.
            try:
                from jobs.models import Job

                await sync_to_async(
                    Job.objects.filter(event__request_id=request.id, closed=False)
                    .update
                )(closed=True, ongoing=False)
            except Exception:
                pass

            # Audit log entry — keeps the timeline honest even though the
            # request itself is no longer visible. Uses KIND_UPDATED with a
            # "deleted" metadata flag since there's no dedicated KIND yet.
            try:
                await sync_to_async(models.RequestActivityLog.objects.create)(
                    tenant=request.tenant,
                    request=request,
                    kind=models.RequestActivityLog.KIND_UPDATED,
                    actor_user=user if getattr(user, "id", None) else None,
                    summary="Request deleted",
                    metadata={"deleted": True},
                )
            except Exception:
                pass

            return build_mutation_response(
                types.DeleteRequestResponse,
                success=True,
                message="Request deleted.",
                input_obj=input,
                deleted_request_uuid=str(request.uuid),
            )
        except models.Request.DoesNotExist:
            return build_mutation_response(
                types.DeleteRequestResponse,
                success=False,
                message="Request not found.",
                input_obj=input,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.DeleteRequestResponse,
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

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def notify_note_mention(
        self,
        info: strawberry.Info,
        input: inputs.NotifyNoteMentionInput,
    ) -> types.NotifyNoteMentionResponse:
        """
        Send a branded email to each recipient telling them they were
        @-mentioned in an internal note on a request. Notes themselves
        aren't persisted server-side yet (localStorage only); this
        mutation is purely the notification fanout.

        Anti-spam:
          - Recipient emails are deduped.
          - Caller must be authenticated (StrictIsAuthenticated).
          - Each recipient is sent inline (no queue/Redis); a failure
            on one email is logged and skipped, others continue.
        """
        try:
            service = RequestMutationService()
            user: User = await service.get_user(info)

            # Resolve the request — tenant membership check piggy-backs
            # off the existing get_tenant guard.
            try:
                request_id = resolve_id_to_int(input.request_id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid request ID.")

            request_obj: models.Request = await sync_to_async(
                models.Request.objects.select_related(
                    "tenant", "retailer"
                ).get
            )(id=request_id)

            tenant: Tenant = await sync_to_async(Tenant.objects.get)(
                id=request_obj.tenant_id
            )
            is_spark_admin = await user.role.is_spark_admin
            if not is_spark_admin:
                try:
                    await sync_to_async(user.get_tenant)(tenant_id=tenant.id)
                except Exception:
                    raise GraphQLError(
                        "You are not authorized to send notes on this tenant."
                    )

            body = (input.note_body or "").strip()
            if not body:
                raise GraphQLError("Note body is empty.")

            # Dedupe + lowercase normalize recipient list.
            seen: set[str] = set()
            recipients: list[str] = []
            for raw in input.recipient_emails or []:
                email = (raw or "").strip().lower()
                if email and email not in seen:
                    seen.add(email)
                    recipients.append(email)
            if not recipients:
                raise GraphQLError("No recipients to notify.")

            author_name = (
                user.get_full_name() or user.email or "A teammate"
            ).strip()
            author_email = (user.email or "").strip() or None

            base = getattr(
                settings,
                "ADMIN_FRONTEND_URL",
                "https://spark-new-admin.web.app",
            ).rstrip("/")
            request_url = (
                (input.request_url or "").strip()
                or f"{base}/request/view/{request_obj.uuid}"
            )

            # Best-effort: per-recipient mailer so one bad address
            # doesn't drop the whole batch.
            sent = 0
            failed: list[str] = []
            for to_email in recipients:
                # Try to look up the recipient user for a friendlier
                # greeting. Not having a user row isn't fatal — we'll
                # fall back to "there".
                target_user = await sync_to_async(
                    lambda: User.objects.filter(email__iexact=to_email).first()
                )()
                mentioned_name = (
                    (target_user.get_full_name() if target_user else None)
                    or (target_user.email if target_user else None)
                    or None
                )
                mailer = NoteMentionMailer(
                    request=request_obj,
                    mentioned_email=to_email,
                    mentioned_name=mentioned_name,
                    note_body=body,
                    author_name=author_name,
                    author_email=author_email,
                    request_url=request_url,
                )
                try:
                    await sync_to_async(mailer.send)()
                    sent += 1
                except Exception as exc:
                    import logging
                    logging.getLogger(__name__).exception(
                        "note-mention email failed for %s on request_id=%s: %s",
                        to_email,
                        request_obj.id,
                        exc,
                    )
                    failed.append(to_email)

            return build_mutation_response(
                types.NotifyNoteMentionResponse,
                success=len(failed) == 0,
                message=(
                    f"Notified {sent} teammate{'s' if sent != 1 else ''}."
                    if not failed
                    else f"Notified {sent}, failed {len(failed)}."
                ),
                input_obj=input,
                sent_count=sent,
                failed_emails=failed or None,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.NotifyNoteMentionResponse,
                success=False,
                message=str(e),
                input_obj=input,
                sent_count=0,
                failed_emails=None,
            )
        except models.Request.DoesNotExist:
            return build_mutation_response(
                types.NotifyNoteMentionResponse,
                success=False,
                message="Request not found.",
                input_obj=input,
                sent_count=0,
                failed_emails=None,
            )
        except Exception as e:
            return build_mutation_response(
                types.NotifyNoteMentionResponse,
                success=False,
                message=str(e),
                input_obj=input,
                sent_count=0,
                failed_emails=None,
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
