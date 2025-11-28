# Import strawberry_django first to ensure strawberry.django is available
import strawberry_django
import strawberry
from graphql import GraphQLError
from django.contrib.auth import get_user_model
from gqlauth.user import relay as mutations
from gqlauth.user.queries import UserQueries
from strawberry_django.permissions import IsAuthenticated
from utils.graphql.permissions import StrictIsAuthenticated

from .models import Role, Tenant
from .mutations import (
    AmbassadorsCustomRegister,
    ClientsCustomRegister,
    SparkCustomRegister,
)
from .dashboard.schema import DashboardQueries
from utils.graphql.relay import (
    CountableConnection,
    connection_from_queryset_async,
)

User = get_user_model()


@strawberry_django.type(Role)
class RoleType:
    id: strawberry.auto
    uuid: strawberry.auto
    name: strawberry.auto


# @strawberry.django.type(Tenant)
@strawberry_django.type(Tenant)
class TenantType:
    id: strawberry.auto
    uuid: strawberry.auto
    name: strawberry.auto


# @strawberry.django.type(model=get_user_model(), name="CustomUserType")
@strawberry_django.type(User)
class CustomUserType:
    id: strawberry.auto
    uuid: strawberry.auto
    username: strawberry.auto
    email: strawberry.auto
    first_name: strawberry.auto
    last_name: strawberry.auto
    role: RoleType


# Spark Schema
@strawberry.type()
class QuerySpark:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    def me(self, info) -> CustomUserType:
        return info.context.request.user

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def tenants(
        self,
        info,
        user_uuid: strawberry.ID | None = None,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
    ) -> CountableConnection[TenantType]:
        queryset = Tenant.objects.all()
        if user_uuid:
            queryset = queryset.filter(
                tenanted_users__is_active=True,
                tenanted_users__user__uuid=user_uuid,
            )
        queryset = queryset.distinct()

        try:
            return await connection_from_queryset_async(
                queryset,
                first=first,
                after=after,
                last=last,
                before=before,
                default_limit=10,
                max_limit=100,
            )
        except ValueError as exc:
            raise GraphQLError(str(exc)) from exc


# Import dashboard queries (moved to top after imports)


@strawberry.type
class MutationSpark(SparkCustomRegister):
    verify_token = mutations.VerifyToken.field
    token_auth = mutations.ObtainJSONWebToken.field
    refresh_token = mutations.RefreshToken.field
    verify_account = mutations.VerifyAccount.field


# Ambassadors Schema
# @strawberry.django.type(model=get_user_model())
@strawberry_django.type(User)
class QueryAmbassadors:
    @strawberry.field
    def me(self, info) -> CustomUserType:
        return info.context.request.user


@strawberry.type
class MutationAmbassadors(AmbassadorsCustomRegister):
    verify_token = mutations.VerifyToken.field
    token_auth = mutations.ObtainJSONWebToken.field
    refresh_token = mutations.RefreshToken.field
    verify_account = mutations.VerifyAccount.field


# Clients Schemas
# @strawberry.django.type(model=get_user_model())
@strawberry_django.type(User)
class QueryClients:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    def me(self, info) -> CustomUserType:
        return info.context.request.user

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def tenants(
        self,
        info,
        user_uuid: strawberry.ID | None = None,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
    ) -> CountableConnection[TenantType]:
        user = info.context.request.user

        filters = {
            "tenanted_users__is_active": True,
        }
        if user_uuid:
            filters["tenanted_users__user__uuid"] = user_uuid
        else:
            filters["tenanted_users__user"] = user

        queryset = Tenant.objects.filter(**filters).distinct()
        try:
            return await connection_from_queryset_async(
                queryset,
                first=first,
                after=after,
                last=last,
                before=before,
                default_limit=10,
                max_limit=100,
            )
        except ValueError as exc:
            raise GraphQLError(str(exc)) from exc


@strawberry.type
class MutationClients(ClientsCustomRegister):
    verify_token = mutations.VerifyToken.field
    token_auth = mutations.ObtainJSONWebToken.field
    refresh_token = mutations.RefreshToken.field
    verify_account = mutations.VerifyAccount.field


# Mobile Schemas
# @strawberry.django.type(model=get_user_model())
@strawberry_django.type(User)
class QueryMobile:
    @strawberry.field
    def me(self, info) -> CustomUserType:
        return info.context.request.user


@strawberry.type
class MutationMobile(ClientsCustomRegister):
    verify_token = mutations.VerifyToken.field
    token_auth = mutations.ObtainJSONWebToken.field
    refresh_token = mutations.RefreshToken.field
    verify_account = mutations.VerifyAccount.field
