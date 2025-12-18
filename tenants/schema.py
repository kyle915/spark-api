# Import strawberry_django first to ensure strawberry.django is available
import strawberry_django
import strawberry
from graphql import GraphQLError
from django.contrib.auth import get_user_model
from gqlauth.user import relay as mutations
from gqlauth.user.queries import UserQueries
from strawberry_django.permissions import IsAuthenticated
from django.db.models import Q
from utils.gcs import extract_blob_name_from_url, generate_download_url
from utils.graphql.permissions import StrictIsAuthenticated

from .models import Role, Tenant
from .types import RoleType, TenantType
from .inputs import TenantFiltersInput, UserFiltersInput
from .mutations import (
    AmbassadorsCustomRegister,
    ClientsCustomRegister,
    SparkCustomRegister,
    SparkTenantMutations,
    SparkUserMutations,
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

    @strawberry.field
    def image(self) -> str | None:
        """Return a signed URL for the user image if it exists."""
        if not self.image:
            return None

        blob_name = extract_blob_name_from_url(self.image.name)
        if not blob_name:
            return None

        return generate_download_url(blob_name)


# Spark Schema
@strawberry.type()
class QuerySpark(GoogleCalendarQueries):
    @strawberry.field
    def healthcheck(self) -> str:
        return "ok"

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    def me(self, info) -> CustomUserType:
        return info.context.request.user

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def user(
        self,
        info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> CustomUserType:
        requester = info.context.request.user

        try:
            is_spark_admin = await requester.role.is_spark_admin
        except Exception as exc:
            raise GraphQLError(f"Error checking permissions: {exc}") from exc

        if not is_spark_admin:
            raise GraphQLError("You do not have permission to perform this action.")

        if not id and not uuid:
            raise GraphQLError("Provide id or uuid to fetch a user.")

        try:
            if id:
                return await User.objects.select_related("role").aget(pk=id)
            return await User.objects.select_related("role").aget(uuid=uuid)
        except User.DoesNotExist as exc:
            raise GraphQLError("User not found.") from exc

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def users(
        self,
        info,
        filters: UserFiltersInput | None = None,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
    ) -> CountableConnection[CustomUserType]:
        user = info.context.request.user

        try:
            is_spark_admin = await user.role.is_spark_admin
        except Exception as exc:
            raise GraphQLError(f"Error checking permissions: {exc}") from exc

        if not is_spark_admin:
            raise GraphQLError("You do not have permission to perform this action.")

        queryset = User.objects.select_related("role").all()

        if filters:
            if filters.tenant_id:
                try:
                    tenant_id = int(filters.tenant_id)
                except (TypeError, ValueError) as exc:
                    raise GraphQLError("Invalid tenantId.") from exc
                queryset = queryset.filter(
                    tenanted_users__is_active=True,
                    tenanted_users__tenant_id=tenant_id,
                )
            if filters.name:
                queryset = queryset.filter(
                    Q(first_name__icontains=filters.name)
                    | Q(last_name__icontains=filters.name)
                )
            if filters.email:
                queryset = queryset.filter(email__icontains=filters.email)
            if filters.role:
                queryset = queryset.filter(role__slug=filters.role.value)

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
class MutationSpark(
    SparkCustomRegister,
    SparkTenantMutations,
    SparkUserMutations,
    GoogleCalendarMutations,
):
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
