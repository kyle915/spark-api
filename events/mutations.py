import datetime

import strawberry
from strawberry import relay
from strawberry.extensions import MaxTokensLimiter
from graphql import GraphQLError
from asgiref.sync import sync_to_async
from typing import Any, Type, TypeVar, Union

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
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
from utils.gcs import delete_blob, extract_blob_name_from_url
from tenants.models import Tenant, User

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
            self.tenant_id = existing_obj.tenant_id
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
            setattr(model, key, value)

        # set the tenant id
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

    @staticmethod
    def _strip_tzinfo(dt_value: Any) -> Any:
        """Remove timezone info to keep provided clock time unchanged."""
        if dt_value is None:
            return None

        if isinstance(dt_value, datetime.datetime):
            return dt_value.replace(tzinfo=None)

        if isinstance(dt_value, str):
            cleaned_value = dt_value
            if dt_value.endswith("Z"):
                cleaned_value = dt_value[:-1] + "+00:00"

            try:
                parsed = datetime.datetime.fromisoformat(cleaned_value)
                return parsed.replace(tzinfo=None)
            except ValueError:
                return dt_value

        return dt_value

    async def save(self) -> Model:
        """Save event keeping start/end times as provided (no TZ conversion)."""
        if self.input:
            self.input.start_time = self._strip_tzinfo(getattr(self.input, "start_time", None))
            self.input.end_time = self._strip_tzinfo(getattr(self.input, "end_time", None))

        return await super().save()


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
            user: User = await service.get_user(info)
            service.info = info
            service.user = user
            service.is_spark_schema = service.is_spark_schema_request(info, user=user)

            request_id = getattr(input, "request_id", None)
            tenant_id: int | None = None

            if request_id:
                try:
                    request_id = resolve_id_to_int(request_id)
                except (TypeError, ValueError, GraphQLError):
                    raise GraphQLError("Invalid request ID.")
                input.request_id = request_id
                request: models.Request = await sync_to_async(
                    models.Request.objects.get
                )(id=request_id)
                tenant_id = request.tenant_id

                is_spark_admin = await user.role.is_spark_admin
                if not service.is_spark_schema and not is_spark_admin:
                    try:
                        await sync_to_async(user.get_tenant)(tenant_id=tenant_id)
                    except Exception:
                        raise GraphQLError(
                            "You are not authorized to create events for this tenant."
                        )
            else:
                input_tenant_id = getattr(input, "tenant_id", None)
                if service.is_spark_schema and input_tenant_id:
                    tenant_id = await service._resolve_tenant_without_membership(
                        input_tenant_id
                    )
                    setattr(input, "tenant_id", tenant_id)
                else:
                    resolved_tenant_id = input_tenant_id
                    if input_tenant_id is not None:
                        try:
                            resolved_tenant_id = resolve_id_to_int(input_tenant_id)
                        except (TypeError, ValueError, GraphQLError):
                            raise GraphQLError("Invalid tenant ID.")
                    tenant = await service.get_tenant(user, resolved_tenant_id)
                    tenant_id = tenant.id

            if tenant_id is None:
                raise GraphQLError(
                    "Tenant ID is required when creating an event without a request."
                )

            approved_status = await sync_to_async(models.EventStatus.objects.get)(
                slug="approved", tenant_id=tenant_id
            )

            # Force the event to use the approved status for the tenant
            input.status_id = approved_status.id
            service.tenant_id = tenant_id

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
        except models.EventStatus.DoesNotExist:
            return build_mutation_response(
                types.EventDetailResponse,
                success=False,
                message="Approved event status not found. Please ensure you have a status with slug 'approved' for this tenant.",
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_event(
        self,
        info: strawberry.Info,
        input: inputs.UpdateEventInput,
    ) -> types.EventDetailResponse:
        try:
            event: models.Event = await EventMutationService.process_create_or_update(
                input=input, info=info
            )
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
                request = await sync_to_async(models.Request.objects.get)(
                    id=request_id
                )
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
            raise GraphQLError(
                "You cannot move store managers to a different tenant."
            )


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
            return build_mutation_response(
                types.RequestDetailResponse,
                success=True,
                message="Request created successfully.",
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


class RequestWithDependenciesMutationService(BaseMutationService):
    """Service for request with dependencies mutations."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Request

    def _save_sync(self, params: dict[str, Any]) -> models.Request:
        """Synchronous save method to handle transaction."""
        with transaction.atomic():
            # Create the request
            request = models.Request(**params)
            if self.user:
                request.created_by = self.user

            if self.is_public and self.input.tenant_id:
                request.tenant_id = self.input.tenant_id
            elif self.tenant_id:
                request.tenant_id = self.tenant_id

            request.save()

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
                    product_params = self._normalize_id_fields(
                        product_input.to_dict()
                    )
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

        # set the parameters
        params: dict[str, Any] = self.input.to_dict(
            ["tenant_id", "id", "details", "products"]
        )
        params = self._normalize_id_fields(params)

        return await sync_to_async(self._save_sync)(params)


@strawberry.type
class RequestMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_request(
        self,
        info: strawberry.Info,
        input: inputs.CreateRequestInput,
    ) -> types.RequestDetailResponse:
        """Create a new request as an authenticated user."""
        try:
            request: models.Request = (
                await RequestWithDependenciesMutationService.process_create_or_update(
                    input=input, info=info
                )
            )
            return build_mutation_response(
                types.RequestDetailResponse,
                success=True,
                message="Request created successfully.",
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
            request: models.Request = await sync_to_async(models.Request.objects.get)(
                id=input.id
            )
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
            await sync_to_async(request.save)()
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

            # Get the request first to access its tenant
            request: models.Request = await sync_to_async(models.Request.objects.get)(
                id=input.id
            )
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
            )(slug="decline", tenant=tenant.id)
            if not decline_status:
                raise GraphQLError(
                    "Decline status not found. Please ensure you have a status with slug 'decline'."
                )
            request.status = decline_status
            await sync_to_async(request.save)()

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
