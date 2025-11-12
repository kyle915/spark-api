import strawberry
from strawberry_django.permissions import IsAuthenticated
from strawberry.extensions import MaxTokensLimiter
from graphql import GraphQLError
from asgiref.sync import sync_to_async
from typing import Union

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.db.models import Model

from events import types
from events import models
from events import inputs
from utils.graphql.inputs import SparkGraphQLInput
from utils.graphql.types import SparkGraphQLErrorResponse
from utils.graphql.mixins import SparkGraphQLMixin
from utils.utils import ROLE_ID
from tenants.models import Tenant, User

User = get_user_model()


class BaseMutationService(SparkGraphQLMixin):
    """Base class for mutation services."""

    input: SparkGraphQLInput | None = None
    info: strawberry.Info | None = None
    user: User | None = None
    tenant_id: int | None = None
    is_public: bool = False

    @classmethod
    def with_input(cls, input: SparkGraphQLInput) -> 'BaseMutationService':
        """Create a new instance of the service with the input."""
        service = cls()
        service.set_input(input)
        return service

    @classmethod
    async def process_create_or_update(cls, input: SparkGraphQLInput, info: strawberry.Info) -> Model:
        """Process the create or update operation."""
        service = cls.with_input(input)
        await service.set_user_and_tenant(info)
        return await service.save()

    def set_input(self, input: SparkGraphQLInput) -> 'BaseMutationService':
        """Set the input for the service."""
        self.input = input
        return self

    async def set_user_and_tenant(self, info: strawberry.Info) -> 'BaseMutationService':
        """Set the user and tenant for the service."""
        self.user = await self.get_user(info)
        self.tenant_id = (await self.get_tenant(self.user, self.input.tenant_id)).id
        return self

    def set_is_public(self, is_public: bool) -> 'BaseMutationService':
        """Set the is public for the service."""
        self.is_public = is_public
        return self

    def get_model(self) -> Model:
        """Get the model for the service."""
        raise NotImplementedError("Subclasses must implement this method.")

    def validations(self):
        """Before save validations."""
        if self.is_public and not self.input.tenant_id:
            raise GraphQLError("Tenant ID is required.")
        if not self.is_public and self.user.role_id != ROLE_ID.SparkAdmin and self.input.tenant_id:
            raise GraphQLError("Tenant ID should not be provided.")

    async def save(self) -> Model:
        """Save the model."""
        # validate the input
        self.validations()

        # get the model
        model_class = self.get_model()
        is_update: bool = hasattr(
            self.input, 'id') and self.input.id is not None
        if is_update:
            model = await sync_to_async(model_class.objects.get)(id=self.input.id)
            if self.user:
                setattr(model, 'updated_by', self.user)
        else:
            model = model_class()
            if self.user:
                setattr(model, 'created_by', self.user)
            if self.is_public and self.input.tenant_id:
                self.tenant_id = self.input.tenant_id

        # set the parameters
        params: dict[str, Any] = self.input.to_dict(['tenant_id', 'id'])
        for key, value in params.items():
            setattr(model, key, value)

        # set the tenant id
        setattr(model, 'tenant_id', self.tenant_id)
        await sync_to_async(model.save)()
        return model


class EventMutationService(BaseMutationService):
    """Service for event mutations."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Event


@strawberry.type
class EventMutations:

    @strawberry.mutation(extensions=[IsAuthenticated()])
    async def create_event(
        self,
        info: strawberry.Info,
        input: inputs.CreateEventInput,
    ) -> types.EventDetailResponse:
        try:
            event: models.Event = await EventMutationService.process_create_or_update(input=input, info=info)
            return types.EventDetailResponse(
                success=True,
                message="Event created successfully.",
                event=event,
            )
        except GraphQLError as e:
            return types.EventDetailResponse(
                success=False,
                message=str(e),
            )

    @strawberry.mutation(extensions=[IsAuthenticated()])
    async def update_event(
        self,
        info: strawberry.Info,
        input: inputs.UpdateEventInput,
    ) -> types.EventDetailResponse:
        try:
            event: models.Event = await EventMutationService.process_create_or_update(input=input, info=info)
            return types.EventDetailResponse(
                success=True,
                message="Event updated successfully.",
                event=event,
            )
        except GraphQLError as e:
            return types.EventDetailResponse(
                success=False,
                message=str(e),
            )


class EventTypeMutationService(BaseMutationService):
    """Service for event type mutations."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.EventType


@strawberry.type
class EventTypeMutations:
    @strawberry.mutation(extensions=[IsAuthenticated()])
    async def create_event_type(
        self,
        info: strawberry.Info,
        input: inputs.CreateEventTypeInput,
    ) -> types.EventTypeDetailResponse:
        """Create a new event type."""
        try:
            event_type: models.EventType = await EventTypeMutationService.process_create_or_update(
                input=input,
                info=info,
            )
            return types.EventTypeDetailResponse(
                success=True,
                message="Event type created successfully.",
                event_type=event_type,
            )
        except GraphQLError as e:
            return types.EventTypeDetailResponse(
                success=False,
                message=str(e)
            )

    @strawberry.mutation(extensions=[IsAuthenticated()])
    async def update_event_type(
        self,
        info: strawberry.Info,
        input: inputs.UpdateEventTypeInput,
    ) -> types.EventTypeDetailResponse:
        """Update an existing event type."""
        try:
            event_type: models.EventType = await EventTypeMutationService.process_create_or_update(
                input=input,
                info=info,
            )
            return types.EventTypeDetailResponse(
                success=True,
                message="Event type updated successfully.",
                event_type=event_type,
            )

        except GraphQLError as e:
            return types.EventTypeDetailResponse(
                success=False,
                message=str(e),
            )


class EventStatusMutationService(BaseMutationService):
    """Service for event status mutations."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.EventStatus


@strawberry.type
class EventStatusMutations:
    @strawberry.mutation(extensions=[IsAuthenticated()])
    async def create_event_status(
        self,
        info: strawberry.Info,
        input: inputs.CreateEventStatusInput,
    ) -> types.EventStatusDetailResponse:
        """Create a new event status."""
        try:
            event_status: models.EventStatus = await EventStatusMutationService.process_create_or_update(
                input=input,
                info=info,
            )
            return types.EventStatusDetailResponse(
                success=True,
                message="Event status created successfully.",
                event_status=event_status,
            )
        except GraphQLError as e:
            return types.EventStatusDetailResponse(
                success=False,
                message=str(e),
            )

    @strawberry.mutation(extensions=[IsAuthenticated()])
    async def update_event_status(
        self,
        info: strawberry.Info,
        input: inputs.UpdateEventStatusInput,
    ) -> types.EventStatusDetailResponse:
        """Update an existing event status."""
        try:
            event_status: models.EventStatus = await EventStatusMutationService.process_create_or_update(
                input=input,
                info=info,
            )
            return types.EventStatusDetailResponse(
                success=True,
                message="Event status updated successfully.",
                event_status=event_status,
            )
        except GraphQLError as e:
            return types.EventStatusDetailResponse(
                success=False,
                message=str(e),
            )


class LocationMutationService(BaseMutationService):
    """Service for location mutations."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Location


@strawberry.type
class LocationMutations:
    @strawberry.mutation(extensions=[IsAuthenticated()])
    async def create_location(
        self,
        info: strawberry.Info,
        input: inputs.CreateLocationInput,
    ) -> types.LocationDetailResponse:
        """Create a new location."""
        try:
            location: models.Location = await LocationMutationService.process_create_or_update(input=input, info=info)
            return types.LocationDetailResponse(
                success=True,
                message="Location created successfully.",
                location=location,
            )
        except GraphQLError as e:
            return types.LocationDetailResponse(
                success=False,
                message=str(e),
            )

    @strawberry.mutation(extensions=[IsAuthenticated()])
    async def update_location(
        self,
        info: strawberry.Info,
        input: inputs.UpdateLocationInput,
    ) -> types.LocationDetailResponse:
        """Update an existing location."""
        try:
            location: models.Location = await LocationMutationService.process_create_or_update(input=input, info=info)
            return types.LocationDetailResponse(
                success=True,
                message="Location updated successfully.",
                location=location,
            )
        except GraphQLError as e:
            return types.LocationDetailResponse(
                success=False,
                message=str(e),
            )


class ClientMutationService(BaseMutationService):
    """Service for client mutations."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Client


@strawberry.type
class ClientMutations:
    @strawberry.mutation(extensions=[IsAuthenticated()])
    async def create_client(
        self,
        info: strawberry.Info,
        input: inputs.CreateClientInput,
    ) -> types.ClientDetailResponse:
        """Create a new client."""
        try:
            client: models.Client = await ClientMutationService.process_create_or_update(input=input, info=info)
            return types.ClientDetailResponse(
                success=True,
                message="Client created successfully.",
                client=client,
            )
        except GraphQLError as e:
            return types.ClientDetailResponse(
                success=False,
                message=str(e),
            )

    @strawberry.mutation(extensions=[IsAuthenticated()])
    async def update_client(
        self,
        info: strawberry.Info,
        input: inputs.UpdateClientInput,
    ) -> types.ClientDetailResponse:
        """Update an existing client."""
        try:
            client: models.Client = await ClientMutationService.process_create_or_update(input=input, info=info)
            return types.ClientDetailResponse(
                success=True,
                message="Client updated successfully.",
                client=client,
            )
        except GraphQLError as e:
            return types.ClientDetailResponse(
                success=False,
                message=str(e),
            )


class DistributorMutationService(BaseMutationService):
    """Service for distributor mutations."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Distributor


@strawberry.type
class DistributorMutations:

    @strawberry.mutation(extensions=[IsAuthenticated()])
    async def create_distributor(
        self,
        info: strawberry.Info,
        input: inputs.CreateDistributorInput,
    ) -> types.DistributorDetailResponse:
        try:
            """Create a new distributor."""
            distributor: models.Distributor = await DistributorMutationService.process_create_or_update(input=input, info=info)
            return types.DistributorDetailResponse(
                success=True,
                message="Distributor created successfully.",
                distributor=distributor,
            )
        except GraphQLError as e:
            return types.DistributorDetailResponse(
                success=False,
                message=str(e),
            )

    @strawberry.mutation(extensions=[IsAuthenticated()])
    async def update_distributor(
        self,
        info: strawberry.Info,
        input: inputs.UpdateDistributorInput,
    ) -> types.DistributorDetailResponse:
        """Update an existing distributor."""
        try:
            distributor: models.Distributor = await DistributorMutationService.process_create_or_update(
                input=input,
                info=info,
            )
            return types.DistributorDetailResponse(
                success=True,
                message="Distributor updated successfully.",
                distributor=distributor,
            )
        except GraphQLError as e:
            return types.DistributorDetailResponse(
                success=False,
                message=str(e),
            )


class RetailerMutationService(BaseMutationService):
    """Service for retailer mutations."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Retailer


@strawberry.type
class RetailerMutations:
    @strawberry.mutation(extensions=[IsAuthenticated()])
    async def create_retailer(
        self,
        info: strawberry.Info,
        input: inputs.CreateRetailerInput,
    ) -> types.RetailerDetailResponse:
        """Create a new retailer."""
        try:
            retailer: models.Retailer = await RetailerMutationService.process_create_or_update(
                input=input,
                info=info,
            )
            return types.RetailerDetailResponse(
                success=True,
                message="Retailer created successfully.",
                retailer=retailer,
            )
        except GraphQLError as e:
            return types.RetailerDetailResponse(
                success=False,
                message=str(e),
            )

    @strawberry.mutation(extensions=[IsAuthenticated()])
    async def update_retailer(
        self,
        info: strawberry.Info,
        input: inputs.UpdateRetailerInput,
    ) -> types.RetailerDetailResponse:
        """Update an existing retailer."""
        try:
            retailer: models.Retailer = await RetailerMutationService.process_create_or_update(
                input=input,
                info=info,
            )
            return types.RetailerDetailResponse(
                success=True,
                message="Retailer updated successfully.",
                retailer=retailer,
            )
        except GraphQLError as e:
            return types.RetailerDetailResponse(
                success=False,
                message=str(e),
            )


class ProductTypeMutationService(BaseMutationService):
    """Service for product type mutations."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.ProductType


@strawberry.type
class ProductTypeMutations:
    @strawberry.mutation(extensions=[IsAuthenticated()])
    async def create_product_type(
        self,
        info: strawberry.Info,
        input: inputs.CreateProductTypeInput,
    ) -> types.ProductTypeDetailResponse:
        """Create a new product type."""
        try:
            product_type: models.ProductType = await ProductTypeMutationService.process_create_or_update(input=input, info=info)
            return types.ProductTypeDetailResponse(
                success=True,
                message="Product type created successfully.",
                product_type=product_type,
            )
        except GraphQLError as e:
            return types.ProductTypeDetailResponse(
                success=False,
                message=str(e),
            )

    @strawberry.mutation(extensions=[IsAuthenticated()])
    async def update_product_type(
        self,
        info: strawberry.Info,
        input: inputs.UpdateProductTypeInput,
    ) -> types.ProductTypeDetailResponse:
        """Update an existing product type."""
        try:
            product_type: models.ProductType = await ProductTypeMutationService.process_create_or_update(input=input, info=info)
            return types.ProductTypeDetailResponse(
                success=True,
                message="Product type updated successfully.",
                product_type=product_type,
            )
        except GraphQLError as e:
            return types.ProductTypeDetailResponse(
                success=False,
                message=str(e),
            )


class ProductMutationService(BaseMutationService):
    """Service for product mutations."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Product


@strawberry.type
class ProductMutations:
    @strawberry.mutation(extensions=[IsAuthenticated()])
    async def create_product(
        self,
        info: strawberry.Info,
        input: inputs.CreateProductInput,
    ) -> types.ProductDetailResponse:
        """Create a new product."""
        try:
            product: models.Product = await ProductMutationService.process_create_or_update(input=input, info=info)
            return types.ProductDetailResponse(
                success=True,
                message="Product created successfully.",
                product=product,
            )
        except GraphQLError as e:
            return types.ProductDetailResponse(
                success=False,
                message=str(e),
            )

    @strawberry.mutation(extensions=[IsAuthenticated()])
    async def update_product(
        self,
        info: strawberry.Info,
        input: inputs.UpdateProductInput,
    ) -> types.ProductDetailResponse:
        """Update an existing product."""
        try:
            product: models.Product = await ProductMutationService.process_create_or_update(input=input, info=info)
            return types.ProductDetailResponse(
                success=True,
                message="Product updated successfully.",
                product=product,
            )
        except GraphQLError as e:
            return types.ProductDetailResponse(
                success=False,
                message=str(e),
            )


class RequestTypeMutationService(BaseMutationService):
    """Service for request type mutations."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.RequestType


@strawberry.type
class RequestTypeMutations:
    @strawberry.mutation(extensions=[IsAuthenticated()])
    async def create_request_type(
        self,
        info: strawberry.Info,
        input: inputs.CreateRequestTypeInput,
    ) -> types.RequestTypeDetailResponse:
        """Create a new request type."""
        try:
            request_type: models.RequestType = await RequestTypeMutationService.process_create_or_update(input=input, info=info)
            return types.RequestTypeDetailResponse(
                success=True,
                message="Request type created successfully.",
                request_type=request_type,
            )
        except GraphQLError as e:
            return types.RequestTypeDetailResponse(
                success=False,
                message=str(e),
            )

    @strawberry.mutation(extensions=[IsAuthenticated()])
    async def update_request_type(
        self,
        info: strawberry.Info,
        input: inputs.UpdateRequestTypeInput,
    ) -> types.RequestTypeDetailResponse:
        """Update an existing request type."""
        try:
            request_type: models.RequestType = await RequestTypeMutationService.process_create_or_update(input=input, info=info)
            return types.RequestTypeDetailResponse(
                success=True,
                message="Request type updated successfully.",
                request_type=request_type,
            )
        except GraphQLError as e:
            return types.RequestTypeDetailResponse(
                success=False,
                message=str(e),
            )


@strawberry.type
class RequestStatusMutations:
    @strawberry.mutation(extensions=[IsAuthenticated()])
    async def create_request_status(
        self,
        info: strawberry.Info,
        input: inputs.CreateRequestStatusInput,
    ) -> types.RequestStatusDetailResponse:
        """Create a new request status."""
        try:
            request_status: models.RequestStatus = await RequestStatusMutationService.process_create_or_update(
                input=input,
                info=info
            )
            return types.RequestStatusDetailResponse(
                success=True,
                message="Request status created successfully.",
                request_status=request_status,
            )
        except GraphQLError as e:
            return types.RequestStatusDetailResponse(
                success=False,
                message=str(e),
            )

    @strawberry.mutation(extensions=[IsAuthenticated()])
    async def update_request_status(
        self,
        info: strawberry.Info,
        input: inputs.UpdateRequestStatusInput,
    ) -> types.RequestStatusDetailResponse:
        """Update an existing request status."""
        try:
            request_status: models.RequestStatus = await RequestStatusMutationService.process_create_or_update(
                input=input,
                info=info
            )
            return types.RequestStatusDetailResponse(
                success=True,
                message="Request status updated successfully.",
                request_status=request_status,
            )
        except GraphQLError as e:
            return types.RequestStatusDetailResponse(
                success=False,
                message=str(e),
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
    @strawberry.mutation
    async def create_request(
        self,
        input: inputs.CreateRequestInput,
    ) -> types.RequestDetailResponse:
        """Create a new request."""
        try:
            service: RequestMutationService = RequestMutationService.with_input(
                input=input)
            service.set_is_public(True)
            request: models.Request = await service.save()
            return types.RequestDetailResponse(
                success=True,
                message="Request created successfully.",
                request=request,
            )
        except GraphQLError as e:
            return types.RequestDetailResponse(
                success=False,
                message=str(e),
            )


@strawberry.type
class RequestMutations:

    async def update_request(
        self,
        info: strawberry.Info,
        input: inputs.UpdateRequestInput,
    ) -> types.RequestDetailResponse:
        """Update an existing request."""
        try:
            request: models.Request = await RequestMutationService.process_create_or_update(input=input, info=info)
            return types.RequestDetailResponse(
                success=True,
                message="Request updated successfully.",
                request=request,
            )
        except GraphQLError as e:
            return types.RequestDetailResponse(
                success=False,
                message=str(e),
            )

    @strawberry.mutation(extensions=[IsAuthenticated()])
    async def approve_request(
        self,
        info: strawberry.Info,
        id: strawberry.ID,
    ) -> types.ApproveRequestResponse:
        """Approve a request."""
        try:
            service: RequestMutationService = RequestMutationService()
            user: User = await service.get_user(info)
            tenant: Tenant = await sync_to_async(user.get_tenant)()
            # if user.role_id == ROLE_ID.Ambassadors:
            #     raise GraphQLError(
            #         "You are not authorized to approve requests.")

            if not tenant:
                raise GraphQLError(
                    "Tenant not found. Please ensure you are a member of a tenant.")

            approval_status = await sync_to_async(
                models.RequestStatus.objects.get_for_approval)(tenant=tenant.id)
            if not approval_status:
                raise GraphQLError(
                    "Approval status not found. Please ensure you have a status for approval.")

            request: models.Request = await sync_to_async(models.Request.objects.get)(id=id)
            request.status = approval_status
            await sync_to_async(request.save)()
            event: models.Event = await models.Event.objects.from_request(
                request=request,
                created_by=user
            )

            return types.ApproveRequestResponse(
                success=True,
                message="Request approved successfully.",
                request=request,
                event=event,
            )
        except GraphQLError as e:
            return types.ApproveRequestResponse(
                success=False,
                message=str(e),
            )
        except Exception as e:
            raise e
            return types.ApproveRequestResponse(
                success=False,
                message=str(e),
            )
