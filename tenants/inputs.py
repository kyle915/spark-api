import strawberry
from utils.graphql.inputs import SparkGraphQLInput
from enum import Enum


@strawberry.enum
class RoleFilterEnum(Enum):
    AMBASSADOR = "ambassador"
    CLIENT = "client"
    SPARK = "spark-admin"


@strawberry.enum
class ColorSchemeEnum(str, Enum):
    DARK = "dark"
    LIGHT = "light"


@strawberry.input
class TenantFiltersInput:
    name: str | None = None
    request_url_name: str | None = None


@strawberry.input
class TenantThemeFiltersInput:
    tenant_id: strawberry.ID | None = None
    color_scheme: ColorSchemeEnum | None = None


@strawberry.input
class CreateOrUpdateTenantThemeInput(SparkGraphQLInput):
    tenant_id: strawberry.ID
    color_scheme: ColorSchemeEnum
    name: str | None = None
    # Arbitrary JSON-like mapping of CSS variable names to values
    css_variables: strawberry.scalar(dict) | None = None


@strawberry.input
class UserFiltersInput:
    tenant_id: strawberry.ID | None = None
    name: str | None = None
    email: str | None = None
    role: RoleFilterEnum | None = None
