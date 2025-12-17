import strawberry
from enum import Enum


@strawberry.enum
class RoleFilterEnum(Enum):
    AMBASSADOR = "ambassador"
    CLIENT = "client"
    SPARK = "spark-admin"


@strawberry.input
class TenantFiltersInput:
    name: str | None = None
    request_url_name: str | None = None


@strawberry.input
class UserFiltersInput:
    tenant_id: strawberry.ID | None = None
    name: str | None = None
    email: str | None = None
    role: RoleFilterEnum | None = None
