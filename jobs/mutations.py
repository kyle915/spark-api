import strawberry
from django.db.models import Model
from strawberry import relay
from graphql import GraphQLError
from typing import Type

from jobs import models, inputs, types
from utils.graphql.mixins import BaseMutationService
from utils.graphql.permissions import StrictIsAuthenticated
from utils.graphql.mixins import BaseMutationMixin


class StatusMutationService(BaseMutationService):
    """Service for status mutations."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Status


@strawberry.type
class StatusMutations(BaseMutationMixin):
    service_class = StatusMutationService
    create_input_class = inputs.CreateStatusInput
    update_input_class = inputs.UpdateStatusInput
    response_class = types.StatusDetailResponse
    model_field_name = "status"

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_status(
        self,
        info: strawberry.Info,
        input: inputs.CreateStatusInput,
    ) -> types.StatusDetailResponse:
        return await self.create(info, input)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_status(
        self,
        info: strawberry.Info,
        input: inputs.UpdateStatusInput,
    ) -> types.StatusDetailResponse:
        return await self.update(info, input)
