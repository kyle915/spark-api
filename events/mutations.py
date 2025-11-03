import strawberry
from strawberry_django.permissions import IsAuthenticated
from graphql import GraphQLError
from asgiref.sync import sync_to_async
from typing import Union

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.db.models import Model

from .types import EventDetailResponse, EventTypeDetailResponse, EventStatusDetailResponse
from .models import Event, EventType, EventStatus
from .inputs import (
    CreateEventInput,
    CreateEventTypeInput,
    CreateEventStatusInput,
    UpdateEventInput,
    UpdateEventTypeInput,
    UpdateEventStatusInput,
)
from utils.graphql.inputs import SparkGraphQLInput
from utils.graphql.types import SparkGraphQLErrorResponse
from utils.graphql.mixins import SparkGraphQLMixin
from utils.utils import ROLE_ID
from tenants.models import Tenant

User = get_user_model()


class BaseMutationService(SparkGraphQLMixin):
    """Base class for mutation services."""

    input: SparkGraphQLInput | None = None
    info: strawberry.Info | None = None
    user: User | None = None
    tenant_id: int | None = None

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

    def get_model(self) -> Model:
        """Get the model for the service."""
        raise NotImplementedError("Subclasses must implement this method.")

    def validations(self):
        """Before save validations."""
        if self.user.role_id != ROLE_ID.SparkAdmin and self.input.tenant_id:
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
            setattr(model, 'updated_by', self.user)
        else:
            model = model_class()
            setattr(model, 'created_by', self.user)

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
        return Event


@strawberry.type
class EventMutations:

    @strawberry.mutation(extensions=[IsAuthenticated()])
    async def create_event(
        self,
        info: strawberry.Info,
        input: CreateEventInput,
    ) -> EventDetailResponse:
        try:
            event: Event = await EventMutationService.process_create_or_update(input=input, info=info)
            return EventDetailResponse(
                success=True,
                message="Event created successfully.",
                event=event,
            )
        except GraphQLError as e:
            return EventDetailResponse(
                success=False,
                message=str(e),
            )

    @strawberry.mutation(extensions=[IsAuthenticated()])
    async def update_event(
        self,
        info: strawberry.Info,
        input: UpdateEventInput,
    ) -> EventDetailResponse:
        try:
            event: Event = await EventMutationService.process_create_or_update(input=input, info=info)
            return EventDetailResponse(
                success=True,
                message="Event updated successfully.",
                event=event,
            )
        except GraphQLError as e:
            return EventDetailResponse(
                success=False,
                message=str(e),
            )


class EventTypeMutationService(BaseMutationService):
    """Service for event type mutations."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return EventType


@strawberry.type
class EventTypeMutations:
    @strawberry.mutation(extensions=[IsAuthenticated()])
    async def create_event_type(
        self,
        info: strawberry.Info,
        input: CreateEventTypeInput,
    ) -> EventTypeDetailResponse:
        """Create a new event type."""
        try:
            event_type: EventType = await EventTypeMutationService.process_create_or_update(
                input=input,
                info=info,
            )
            return EventTypeDetailResponse(
                success=True,
                message="Event type created successfully.",
                event_type=event_type,
            )
        except GraphQLError as e:
            return EventTypeDetailResponse(
                success=False,
                message=str(e)
            )

    @strawberry.mutation(extensions=[IsAuthenticated()])
    async def update_event_type(
        self,
        info: strawberry.Info,
        input: UpdateEventTypeInput,
    ) -> EventTypeDetailResponse:
        """Update an existing event type."""
        try:
            event_type: EventType = await EventTypeMutationService.process_create_or_update(
                input=input,
                info=info,
            )
            return EventTypeDetailResponse(
                success=True,
                message="Event type updated successfully.",
                event_type=event_type,
            )

        except GraphQLError as e:
            return EventTypeDetailResponse(
                success=False,
                message=str(e),
            )


class EventStatusMutationService(BaseMutationService):
    """Service for event status mutations."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return EventStatus


@strawberry.type
class EventStatusMutations:
    @strawberry.mutation(extensions=[IsAuthenticated()])
    async def create_event_status(
        self,
        info: strawberry.Info,
        input: CreateEventStatusInput,
    ) -> EventStatusDetailResponse:
        """Create a new event status."""
        try:
            event_status: EventStatus = await EventStatusMutationService.process_create_or_update(
                input=input,
                info=info,
            )
            return EventStatusDetailResponse(
                success=True,
                message="Event status created successfully.",
                event_status=event_status,
            )
        except GraphQLError as e:
            return EventStatusDetailResponse(
                success=False,
                message=str(e),
            )

    @strawberry.mutation(extensions=[IsAuthenticated()])
    async def update_event_status(
        self,
        info: strawberry.Info,
        input: UpdateEventStatusInput,
    ) -> EventStatusDetailResponse:
        """Update an existing event status."""
        try:
            event_status: EventStatus = await EventStatusMutationService.process_create_or_update(
                input=input,
                info=info,
            )
            return EventStatusDetailResponse(
                success=True,
                message="Event status updated successfully.",
                event_status=event_status,
            )
        except GraphQLError as e:
            return EventStatusDetailResponse(
                success=False,
                message=str(e),
            )
