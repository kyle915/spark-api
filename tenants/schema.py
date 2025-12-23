# Import strawberry_django first to ensure strawberry.django is available
import strawberry_django
import strawberry
from graphql import GraphQLError
from django.contrib.auth import get_user_model
from asgiref.sync import sync_to_async
from gqlauth.user import relay as mutations
from gqlauth.user.queries import UserQueries
from strawberry_django.permissions import IsAuthenticated
from django.db.models import Q
from asgiref.sync import sync_to_async
from utils.gcs import extract_blob_name_from_url, generate_download_url
from utils.graphql.permissions import StrictIsAuthenticated

from .models import Role, Tenant, TenantTheme, TenantedUser
from .types import RoleType, TenantType, TenantThemeType
from .inputs import ColorSchemeEnum, TenantFiltersInput, UserFiltersInput
from .mutations import (
    AmbassadorsCustomRegister,
    ClientsCustomRegister,
    SparkCustomRegister,
    SparkTenantMutations,
    TenantThemeMutations,
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


@strawberry.type
class TenantThemingQuery:
    @strawberry.field
    async def tenant_theme_public(
        self,
        info,
        request_url_name: str,
        color_scheme: ColorSchemeEnum = ColorSchemeEnum.DARK,
    ) -> TenantThemeType | None:
        """
        Public query to fetch a tenant theme by request URL name and color scheme.

        This is intentionally unauthenticated so that login and public pages
        can render tenant-specific branding.
        """
        try:
            tenant = await sync_to_async(Tenant.objects.get)(
                request_url_name=request_url_name
            )
        except Tenant.DoesNotExist:
            return None

        theme = await sync_to_async(
            lambda: TenantTheme.objects.filter(
                tenant=tenant, color_scheme=color_scheme.value
            ).first()
        )()
        return theme

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def tenant_theme(
        self,
        info,
        tenant_id: strawberry.ID,
        color_scheme: ColorSchemeEnum = ColorSchemeEnum.DARK,
    ) -> TenantThemeType | None:
        """
        Authenticated query to fetch a tenant theme by tenant ID and color scheme.
        Ensures the requesting user belongs to the tenant.
        """
        user = info.context.request.user

        has_access = await sync_to_async(
            lambda: TenantedUser.objects.filter(
                user=user, tenant_id=tenant_id, is_active=True
            ).exists()
        )()
        if not has_access:
            raise GraphQLError(
                "You do not have permission to view this tenant theme."
            )

        try:
            tenant = await sync_to_async(Tenant.objects.get)(pk=tenant_id)
        except Tenant.DoesNotExist:
            return None

        theme = await sync_to_async(
            lambda: TenantTheme.objects.filter(
                tenant=tenant, color_scheme=color_scheme.value
            ).first()
        )()
        return theme


# Spark Schema
@strawberry.type()
class QuerySpark(GoogleCalendarQueries, TenantThemingQuery):
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
            role_slug = requester.role.slug if requester.role else None
            is_spark_admin = role_slug == Role.SPARK_ADMIN_SLUG
            is_client = role_slug == Role.CLIENT_SLUG
        except Exception as exc:
            raise GraphQLError(f"Error checking permissions: {exc}") from exc

        if not (is_spark_admin or is_client):
            raise GraphQLError(
                "You do not have permission to perform this action.")

        if not id and not uuid:
            raise GraphQLError("Provide id or uuid to fetch a user.")

        try:
            if id:
                target_user = await User.objects.select_related("role").aget(pk=id)
            else:
                target_user = await User.objects.select_related("role").aget(uuid=uuid)
        except User.DoesNotExist as exc:
            raise GraphQLError("User not found.") from exc

        if is_client and not is_spark_admin:
            has_shared_tenant = await sync_to_async(
                TenantedUser.objects.filter(
                    user=target_user,
                    is_active=True,
                    tenant__tenanted_users__user=requester,
                    tenant__tenanted_users__is_active=True,
                ).exists
            )()
            if not has_shared_tenant:
                raise GraphQLError(
                    "You do not have permission to view this user.")

        return target_user

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
            role_slug = user.role.slug if user.role else None
            is_spark_admin = role_slug == Role.SPARK_ADMIN_SLUG
            is_client = role_slug == Role.CLIENT_SLUG
        except Exception as exc:
            raise GraphQLError(f"Error checking permissions: {exc}") from exc

        if not (is_spark_admin or is_client):
            raise GraphQLError(
                "You do not have permission to perform this action.")

        queryset = User.objects.select_related("role").all()
        requester_tenant_ids: list[int] = []

        if not is_spark_admin:
            requester_tenant_ids = await sync_to_async(list)(
                user.tenanted_users.filter(is_active=True).values_list(
                    "tenant_id", flat=True
                )
            )
            queryset = queryset.filter(
                tenanted_users__is_active=True,
                tenanted_users__tenant_id__in=requester_tenant_ids,
            )

        if filters:
            if filters.tenant_id:
                try:
                    tenant_id = int(filters.tenant_id)
                except (TypeError, ValueError) as exc:
                    raise GraphQLError("Invalid tenantId.") from exc
                if not is_spark_admin and tenant_id not in requester_tenant_ids:
                    raise GraphQLError(
                        "You do not have permission to view users for this tenant."
                    )
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
    TenantThemeMutations,
):
    verify_token = mutations.VerifyToken.field
    token_auth = mutations.ObtainJSONWebToken.field
    refresh_token = mutations.RefreshToken.field
    verify_account = mutations.VerifyAccount.field


# Ambassadors Schema
# @strawberry.django.type(model=get_user_model())
@strawberry_django.type(User)
class QueryAmbassadors(GoogleCalendarQueries, TenantThemingQuery):
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
class QueryClients(GoogleCalendarQueries, TenantThemingQuery):
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
            role_slug = requester.role.slug if requester.role else None
            is_spark_admin = role_slug == Role.SPARK_ADMIN_SLUG
            is_client = role_slug == Role.CLIENT_SLUG
        except Exception as exc:
            raise GraphQLError(f"Error checking permissions: {exc}") from exc

        if not (is_spark_admin or is_client):
            raise GraphQLError(
                "You do not have permission to perform this action.")

        if not id and not uuid:
            raise GraphQLError("Provide id or uuid to fetch a user.")

        try:
            if id:
                target_user = await User.objects.select_related("role").aget(pk=id)
            else:
                target_user = await User.objects.select_related("role").aget(uuid=uuid)
        except User.DoesNotExist as exc:
            raise GraphQLError("User not found.") from exc

        if is_client and not is_spark_admin:
            has_shared_tenant = await sync_to_async(
                TenantedUser.objects.filter(
                    user=target_user,
                    is_active=True,
                    tenant__tenanted_users__user=requester,
                    tenant__tenanted_users__is_active=True,
                ).exists
            )()
            if not has_shared_tenant:
                raise GraphQLError(
                    "You do not have permission to view this user.")

        return target_user

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
            role_slug = user.role.slug if user.role else None
            is_spark_admin = role_slug == Role.SPARK_ADMIN_SLUG
            is_client = role_slug == Role.CLIENT_SLUG
        except Exception as exc:
            raise GraphQLError(f"Error checking permissions: {exc}") from exc

        if not (is_spark_admin or is_client):
            raise GraphQLError(
                "You do not have permission to perform this action.")

        queryset = User.objects.select_related("role").all()
        requester_tenant_ids: list[int] = []

        if not is_spark_admin:
            requester_tenant_ids = await sync_to_async(list)(
                user.tenanted_users.filter(is_active=True).values_list(
                    "tenant_id", flat=True
                )
            )
            queryset = queryset.filter(
                tenanted_users__is_active=True,
                tenanted_users__tenant_id__in=requester_tenant_ids,
            )

        if filters:
            if filters.tenant_id:
                try:
                    tenant_id = int(filters.tenant_id)
                except (TypeError, ValueError) as exc:
                    raise GraphQLError("Invalid tenantId.") from exc
                if not is_spark_admin and tenant_id not in requester_tenant_ids:
                    raise GraphQLError(
                        "You do not have permission to view users for this tenant."
                    )
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
class MutationClients(ClientsCustomRegister, SparkUserMutations, GoogleCalendarMutations):
    verify_token = mutations.VerifyToken.field
    token_auth = mutations.ObtainJSONWebToken.field
    refresh_token = mutations.RefreshToken.field
    verify_account = mutations.VerifyAccount.field


# Mobile Schemas
# @strawberry.django.type(model=get_user_model())
@strawberry_django.type(User)
class QueryMobile(TenantThemingQuery):
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
