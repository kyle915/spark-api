import strawberry
from django.db.models import Model
from strawberry import relay

from jobs import models, inputs, types
from utils.graphql.mixins import BaseMutationService
from utils.graphql.permissions import StrictIsAuthenticated


class StatusMutationService(BaseMutationService):
    """Service for status mutations."""
    response_class = types.StatusDetailResponse
    model_field_name = "status"

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Status


@strawberry.type
class StatusMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_ambassador_job_status(
        self,
        info: strawberry.Info,
        input: inputs.CreateStatusInput,
    ) -> types.StatusDetailResponse:
        return await StatusMutationService.create(input, info)

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_ambassador_job_status(
        self,
        info: strawberry.Info,
        input: inputs.UpdateStatusInput,
    ) -> types.StatusDetailResponse:
        return await StatusMutationService.update(input, info)
