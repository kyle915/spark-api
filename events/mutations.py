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
from utils.graphql.mixins import SparkGraphQLMixin
from utils.utils import ROLE_ID, build_mutation_response
from utils.gcs import delete_blob
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
        if (
            not self.is_public
            and not self.is_spark_schema
            and self.user.role_id != ROLE_ID.SparkAdmin
            and tenant_id
        ):
            raise GraphQLError("Tenant ID should not be provided.")

    async def _resolve_tenant_without_membership(
        self, tenant_id: Union[str, int]
    ) -> int:
        """Resolve tenant ID for Spark schema requests without membership restrictions."""
        try:
            tenant_pk = int(tenant_id)
        except (TypeError, ValueError):
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
        for key, value in params.items():
            setattr(model, key, value)

        # set the tenant id
        setattr(model, "tenant_id", self.tenant_id)
        await sync_to_async(model.save)()
        return model


class EventMutationService(BaseMutationService):
    """Service for event mutations."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Event


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

            # Use the request's tenant to resolve the approved status
            request: models.Request = await sync_to_async(models.Request.objects.get)(
                id=input.request_id
            )
            tenant: Tenant = await sync_to_async(Tenant.objects.get)(
                id=request.tenant_id
            )

            is_spark_admin = await user.role.is_spark_admin
            if not is_spark_admin:
                try:
                    await sync_to_async(user.get_tenant)(tenant_id=tenant.id)
                except Exception:
                    raise GraphQLError(
                        "You are not authorized to create events for this tenant."
                    )

            approved_status = await sync_to_async(models.EventStatus.objects.get)(
                slug="approved", tenant_id=tenant.id
            )

            # Force the event to use the approved status for the request's tenant
            input.status_id = approved_status.id
            service.tenant_id = tenant.id

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
        try:
            product: models.Product = (
                await ProductMutationService.process_create_or_update(
                    input=input, info=info
                )
            )
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
                product: models.Product = await sync_to_async(
                    models.Product.objects.get
                )(id=input.id)
            except models.Product.DoesNotExist:
                raise GraphQLError("Product not found.")

            if not is_spark_request:
                try:
                    await sync_to_async(user.get_tenant)(
                        tenant_id=product.tenant_id
                    )
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
                    product_params = product_input.to_dict()
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
                await RequestMutationService.process_create_or_update(
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
            event: models.Event = await models.Event.objects.from_request(
                request=request, created_by=user
            )

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
