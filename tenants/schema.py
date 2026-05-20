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
from utils.gcs import extract_blob_name_from_url, public_url
from utils.graphql.permissions import StrictIsAuthenticated
from strawberry.relay import Node

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
    AmbassadorUserMutations,
)
from .calendar import GoogleCalendarMutations, GoogleCalendarQueries
from .dashboard.schema import DashboardQueries
from utils.graphql.relay import (
    CountableConnection,
    connection_from_queryset_async,
)
from utils.graphql.mixins import resolve_id_to_int

User = get_user_model()


# @strawberry.django.type(model=get_user_model(), name="CustomUserType")
@strawberry_django.type(User)
class CustomUserType(Node):
    uuid: strawberry.auto
    username: strawberry.auto
    email: strawberry.auto
    first_name: strawberry.auto
    last_name: strawberry.auto
    role: RoleType

    @strawberry.field(name="image")
    def image_url(self) -> str | None:
        """Return the public URL for the user image if any. Aliased
        via name= so the resolver doesn't shadow self.image.

        __dict__-only — never getattr, which triggers FieldFile lazy
        load (sync SQL) and crashes async resolvers.
        """
        field_file = self.__dict__.get("image")
        if not field_file:
            return None
        try:
            blob = field_file.name
        except Exception:
            blob = str(field_file)
        blob_name = extract_blob_name_from_url(blob)
        return public_url(blob_name)


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

        # Spark admins can view any tenant theme; others must belong to the tenant.
        role_slug = getattr(user.role, "slug", None)
        is_spark_admin = role_slug == Role.SPARK_ADMIN_SLUG
        try:
            resolved_tenant_id = resolve_id_to_int(tenant_id)
        except (TypeError, ValueError, GraphQLError):
            raise GraphQLError("Invalid tenant ID.")
        if not is_spark_admin:
            has_access = await sync_to_async(
                lambda: TenantedUser.objects.filter(
                    user=user, tenant_id=resolved_tenant_id, is_active=True
                ).exists()
            )()
            if not has_access:
                raise GraphQLError(
                    "You do not have permission to view this tenant theme."
                )

        try:
            tenant = await sync_to_async(Tenant.objects.get)(pk=resolved_tenant_id)
        except Tenant.DoesNotExist:
            return None

        theme = await sync_to_async(
            lambda: TenantTheme.objects.filter(
                tenant=tenant, color_scheme=color_scheme.value
            ).first()
        )()
        return theme


@strawberry.type
class ServerInfoType:
    """Lightweight build/runtime snapshot — what's running right now.

    Used to verify a deploy without tailing Cloud Run logs. Exposed
    unauthenticated (matches `healthcheck`); contains no secrets.
    """

    # ISO-8601 timestamp of the server's now() at request time.
    server_time: str
    # Cloud Build / git SHA of the running revision. Comes from the
    # ``GIT_SHA`` env var if set; falls back to "dev".
    git_sha: str
    # Cloud Run revision tag (e.g. "spark-api-new-00035-nmp"). Falls
    # back to "local" outside Cloud Run.
    revision: str
    # True when the default database connection responds to SELECT 1.
    database_ok: bool


def _check_database_ok_sync() -> bool:
    """Run a SELECT 1 round-trip on the default connection.

    Pulled out so it can be wrapped by ``sync_to_async`` — Django's
    ``connection.cursor()`` does sync I/O, and Strawberry's mobile/spark
    schemas execute resolvers on the asyncio loop. Calling sync I/O
    directly there raises ``SynchronousOnlyOperation``, which the old
    catch-all blanket-converted into ``database_ok=False`` — making the
    probe always lie. Now the sync work runs on a worker thread.
    """
    from django.db import connection

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            return bool(cursor.fetchone())
    except Exception:
        return False


async def _build_server_info() -> ServerInfoType:
    """Snapshot the running container — never raises.

    Async so the DB probe can be awaited via ``sync_to_async``. All
    callers (Spark, Client, Ambassador, Mobile schemas) await this.
    """
    import os
    from datetime import datetime, timezone as _tz

    db_ok = await sync_to_async(_check_database_ok_sync, thread_sensitive=False)()

    return ServerInfoType(
        server_time=datetime.now(_tz.utc).isoformat(),
        git_sha=os.environ.get("GIT_SHA", "dev"),
        revision=os.environ.get("K_REVISION", "local"),
        database_ok=db_ok,
    )


# Spark Schema
@strawberry.type()
class QuerySpark(GoogleCalendarQueries, TenantThemingQuery):
    @strawberry.field
    def healthcheck(self) -> str:
        return "ok"

    @strawberry.field
    async def server_info(self) -> ServerInfoType:
        return await _build_server_info()

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
            raise GraphQLError("You do not have permission to perform this action.")

        if not id and not uuid:
            raise GraphQLError("Provide id or uuid to fetch a user.")

        try:
            if id:
                resolved_id = resolve_id_to_int(id)
                target_user = await User.objects.select_related("role").aget(
                    pk=resolved_id
                )
            else:
                target_user = await User.objects.select_related("role").aget(uuid=uuid)
        except User.DoesNotExist as exc:
            raise GraphQLError("User not found.") from exc
        except (TypeError, ValueError, GraphQLError) as exc:
            raise GraphQLError("Invalid user ID.") from exc

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
                raise GraphQLError("You do not have permission to view this user.")

        return target_user

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def tenant(
        self,
        info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> TenantType:
        if not id and not uuid:
            raise GraphQLError("Provide id or uuid to fetch a tenant.")

        try:
            if id:
                resolved_id = resolve_id_to_int(id)
                tenant = await Tenant.objects.aget(pk=resolved_id)
            else:
                tenant = await Tenant.objects.aget(uuid=uuid)
        except Tenant.DoesNotExist as exc:
            raise GraphQLError("Tenant not found.") from exc
        except (TypeError, ValueError, GraphQLError) as exc:
            raise GraphQLError("Invalid tenant ID.") from exc

        return tenant

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
            raise GraphQLError("You do not have permission to perform this action.")

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
                    tenant_id = resolve_id_to_int(filters.tenant_id)
                except (TypeError, ValueError, GraphQLError) as exc:
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
    async def server_info(self) -> ServerInfoType:
        return await _build_server_info()

    @strawberry.field
    def me(self, info) -> CustomUserType:
        return info.context.request.user


@strawberry.type
class MutationAmbassadors(
    AmbassadorsCustomRegister,
    AmbassadorUserMutations,
    GoogleCalendarMutations,
):
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

    @strawberry.field
    async def server_info(self) -> ServerInfoType:
        return await _build_server_info()

    @strawberry.field
    async def tenant_public(
        self,
        info,
        request_url_name: str,
    ) -> TenantType | None:
        try:
            return await sync_to_async(Tenant.objects.get)(
                request_url_name=request_url_name
            )
        except Tenant.DoesNotExist:
            return None

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
            raise GraphQLError("You do not have permission to perform this action.")

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
                raise GraphQLError("You do not have permission to view this user.")

        return target_user

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def tenant(
        self,
        info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> TenantType:
        if not id and not uuid:
            raise GraphQLError("Provide id or uuid to fetch a tenant.")

        user = info.context.request.user
        queryset = Tenant.objects.filter(
            tenanted_users__is_active=True,
            tenanted_users__user=user,
        )

        try:
            if id:
                resolved_id = resolve_id_to_int(id)
                queryset = queryset.filter(pk=resolved_id)
            else:
                queryset = queryset.filter(uuid=uuid)
            tenant = await queryset.aget()
        except Tenant.DoesNotExist as exc:
            raise GraphQLError("Tenant not found.") from exc
        except (TypeError, ValueError, GraphQLError) as exc:
            raise GraphQLError("Invalid tenant ID.") from exc

        return tenant

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
            raise GraphQLError("You do not have permission to perform this action.")

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
                    tenant_id = resolve_id_to_int(filters.tenant_id)
                except (TypeError, ValueError, GraphQLError) as exc:
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
class MutationClients(
    ClientsCustomRegister,
    SparkUserMutations,
    GoogleCalendarMutations,
    TenantThemeMutations,
):
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
    async def server_info(self) -> ServerInfoType:
        return await _build_server_info()

    @strawberry.field
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


class AppointmentSlot:
    pass


class Reservation:
    pass


class Customer:
    pass


@strawberry.type
class MutationMobile(
    AmbassadorsCustomRegister,
    AmbassadorUserMutations,
    SparkUserMutations,
):
    verify_token = mutations.VerifyToken.field
    token_auth = mutations.ObtainJSONWebToken.field
    refresh_token = mutations.RefreshToken.field
    verify_account = mutations.VerifyAccount.field
