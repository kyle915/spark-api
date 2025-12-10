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


@strawberry.input
class AmbassadorInvitationFiltersInput:
    """Filters for ambassador invitation queries."""
    tenant_id: strawberry.ID | None = None
    tenant_uuid: strawberry.ID | None = None
    is_expired: bool | None = None  # True for expired, False for active, None for all
    is_used: bool | None = None  # True for used, False for unused, None for all
    email: str | None = None  # Search by email (partial match)
    search: str | None = None  # Search by email or name (general search)


@strawberry.input
class AmbassadorFiltersInput:
    """Filters for ambassador queries."""
    tenant_id: strawberry.ID | None = None
    tenant_uuid: strawberry.ID | None = None
    is_active: bool | None = None  # True for active, False for inactive, None for all
    email: str | None = None  # Search by user email (partial match)
    name: str | None = None  # Search by user first_name or last_name
    address: str | None = None  # Search by address (partial match)
    search: str | None = None  # General search across email, name, address


@strawberry.input
class UpdateAmbassadorInput(SparkGraphQLInput):
    """Input for updating an ambassador."""
    ambassador_id: strawberry.ID
    address: str | None = None
    coordinates: List[float] | None = None
    is_active: bool | None = None
    tenant_id: strawberry.ID | None = None  # For assigning to tenant


@strawberry.input
class DeleteInvitationInput(SparkGraphQLInput):
    """Input for deleting an invitation."""
    invitation_id: strawberry.ID
