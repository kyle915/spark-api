import strawberry
from typing import List

from utils.graphql.inputs import SparkGraphQLInput, BaseTenantInput


@strawberry.input
class CreatePublicAmbassadorInput(SparkGraphQLInput):
    """Input for public ambassador creation."""
    first_name: str
    email: str
    password1: str
    password2: str
    address: str | None = None
    coordinates: List[float] | None = None  # [latitude, longitude]


@strawberry.input
class CreateAmbassadorInvitationInput(BaseTenantInput):
    """Input for creating ambassador invitation."""
    email: str


@strawberry.input
class AcceptAmbassadorInvitationInput(SparkGraphQLInput):
    """Input for accepting ambassador invitation."""
    token: str
    first_name: str
    password1: str
    password2: str
    address: str | None = None
    coordinates: List[float] | None = None  # [latitude, longitude]


@strawberry.input
class ApproveAmbassadorInput(SparkGraphQLInput):
    """Input for approving an ambassador."""
    ambassador_id: strawberry.ID
    tenant_id: strawberry.ID | None = None
