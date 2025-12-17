# Import strawberry_django first to ensure strawberry.django is available
import strawberry_django
import strawberry
from graphql import GraphQLError
from django.contrib.auth import get_user_model
from asgiref.sync import sync_to_async
from gqlauth.user import relay as mutations
from gqlauth.user.queries import UserQueries
from strawberry_django.permissions import IsAuthenticated
from utils.graphql.permissions import StrictIsAuthenticated

from .models import Role, Tenant, TenantTheme
from .types import RoleType, TenantType, TenantThemeType
from .inputs import TenantFiltersInput
from .mutations import (
    AmbassadorsCustomRegister,
    ClientsCustomRegister,
    SparkCustomRegister,
    SparkTenantMutations,
    TenantThemeMutations,
)
from .calendar import GoogleCalendarMutations, GoogleCalendarQueries
from .dashboard.schema import DashboardQueries
from utils.graphql.relay import (
    CountableConnection,
    connection_from_queryset_async,
)

User = get_user_model()


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
class QuerySpark(GoogleCalendarQueries):
    @strawberry.field
    def healthcheck(self) -> str:
        return "ok"

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    def me(self, info) -> CustomUserType:
        return info.context.request.user

    @strawberry.field
    async def tenant_theme_public(
        self,
        info,
        tenant_id: strawberry.ID,
        color_scheme: str = "dark",
    ) -> TenantThemeType | None:
        """
        Public query to fetch a tenant theme by tenant ID and color scheme.

        This is intentionally unauthenticated so that login and public pages
        can render tenant-specific branding.
        """
        try:
            tenant = await sync_to_async(Tenant.objects.get)(pk=tenant_id)
        except Tenant.DoesNotExist:
            return None

        theme = await sync_to_async(
            lambda: TenantTheme.objects.filter(
                tenant=tenant, color_scheme=color_scheme
            ).first()
        )()
        return theme

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def tenants(
        self,
        info,
        user_uuid: strawberry.ID | None = None,
        filters: TenantFiltersInput | None = None,
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
        if filters:
            if filters.name:
                queryset = queryset.filter(name__icontains=filters.name)
            if filters.request_url_name:
                queryset = queryset.filter(
                    request_url_name__icontains=filters.request_url_name
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
class MutationSpark(SparkCustomRegister, SparkTenantMutations, TenantThemeMutations, GoogleCalendarMutations):
    verify_token = mutations.VerifyToken.field
    token_auth = mutations.ObtainJSONWebToken.field
    refresh_token = mutations.RefreshToken.field
    verify_account = mutations.VerifyAccount.field


# Ambassadors Schema
# @strawberry.django.type(model=get_user_model())
@strawberry_django.type(User)
class QueryAmbassadors(GoogleCalendarQueries):
    @strawberry.field
    def healthcheck(self) -> str:
        return "ok"

    @strawberry.field
    def me(self, info) -> CustomUserType:
        return info.context.request.user


@strawberry.type
class MutationAmbassadors(AmbassadorsCustomRegister, GoogleCalendarMutations):
    verify_token = mutations.VerifyToken.field
    token_auth = mutations.ObtainJSONWebToken.field
    refresh_token = mutations.RefreshToken.field
    verify_account = mutations.VerifyAccount.field


# Clients Schemas
# @strawberry.django.type(model=get_user_model())
@strawberry_django.type(User)
class QueryClients(GoogleCalendarQueries):
    @strawberry.field
    def healthcheck(self) -> str:
        return "ok"

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    def me(self, info) -> CustomUserType:
        return info.context.request.user

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def tenants(
        self,
        info,
        user_uuid: strawberry.ID | None = None,
        filters: TenantFiltersInput | None = None,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
    ) -> CountableConnection[TenantType]:
        user = info.context.request.user

        filter_dict = {
            "tenanted_users__is_active": True,
        }
        if user_uuid:
            filter_dict["tenanted_users__user__uuid"] = user_uuid
        else:
            filter_dict["tenanted_users__user"] = user

        queryset = Tenant.objects.filter(**filter_dict)

        if filters:
            if filters.name:
                queryset = queryset.filter(name__icontains=filters.name)
            if filters.request_url_name:
                queryset = queryset.filter(
                    request_url_name__icontains=filters.request_url_name
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


@strawberry.type
class MutationClients(ClientsCustomRegister, GoogleCalendarMutations):
    verify_token = mutations.VerifyToken.field
    token_auth = mutations.ObtainJSONWebToken.field
    refresh_token = mutations.RefreshToken.field
    verify_account = mutations.VerifyAccount.field


# Mobile Schemas
# @strawberry.django.type(model=get_user_model())
@strawberry_django.type(User)
class QueryMobile:
    @strawberry.field
    def healthcheck(self) -> str:
        return "ok"

    @strawberry.field
    def me(self, info) -> CustomUserType:
        return info.context.request.user


class AppointmentSlot:
    pass


class Reservation:
    pass


class Customer:
    pass


@strawberry.type
class MutationMobile(AmbassadorsCustomRegister):
    verify_token = mutations.VerifyToken.field
    token_auth = mutations.ObtainJSONWebToken.field
    refresh_token = mutations.RefreshToken.field
    verify_account = mutations.VerifyAccount.field
