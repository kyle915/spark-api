"""XLSX export + signed download helpers for recap mutations."""
from asgiref.sync import sync_to_async
from django.conf import settings
from django.utils import timezone as django_timezone
from django.utils.text import slugify
from graphql import GraphQLError

from recaps import inputs
from recaps import models
from recaps.excel import build_recaps_xlsx
from recaps.queries import CustomRecapQueriesService, RecapQueriesService
from utils.gcs import (
    public_url,
    extract_blob_name_from_url,
    upload_bytes,
    generate_download_url,
    get_gcs_client,
)
from utils.graphql.mixins import resolve_id_to_int
from utils.graphql.permissions import _is_admin_access, resolve_request_user_access

# Who may export what (recap exports, file downloads, campaign report):
#   - Ignite admins (DB-backed ``_is_admin_access``): any recap, any tenant.
#   - Clients: only recaps in a tenant they're an ACTIVE member of, and only
#     approved, un-archived ones (the same rule the client recap lists use).
#   - Everyone else (brand ambassadors, a removed admin whose role was
#     cleared): refused.
# Out-of-scope ids read as "<Recap> not found." so a probe can't confirm a
# recap exists under another brand, or that a draft exists.
EXPORT_SCOPE_ADMIN = "admin"
EXPORT_SCOPE_CLIENT = "client"
EXPORT_SCOPE_REFUSED = "refused"


class RecapExportMixin:
    async def _export_scope(self) -> str:
        """Classify the caller for exports, from the DB row (never the JWT
        user's role, which doesn't hydrate inside async resolvers)."""
        user = getattr(self, "user", None)
        if user is None or not getattr(user, "is_authenticated", False):
            return EXPORT_SCOPE_REFUSED
        role_slug, is_staff, is_super, email = await resolve_request_user_access(
            user
        )
        if _is_admin_access(role_slug, is_staff, is_super, email):
            return EXPORT_SCOPE_ADMIN
        if role_slug == "client":
            return EXPORT_SCOPE_CLIENT
        return EXPORT_SCOPE_REFUSED

    async def _export_member_tenant_ids(self) -> set[int]:
        """Tenants the caller is an active member of (one query)."""
        from tenants.models import TenantedUser

        user = getattr(self, "user", None)
        if user is None or getattr(user, "pk", None) is None:
            return set()
        return await sync_to_async(
            lambda: set(
                TenantedUser.objects.filter(
                    user_id=user.pk, is_active=True
                ).values_list("tenant_id", flat=True)
            )
        )()

    @staticmethod
    def _client_visible_recap(recap) -> bool:
        """Approved and not archived: what a client may see."""
        return bool(getattr(recap, "approved", False)) and (
            getattr(recap, "archived_at", None) is None
        )

    async def _assert_recap_export_access(
        self,
        *,
        tenant_id: int | None,
        approved: bool,
        archived_at,
        record_label: str = "Recap",
    ) -> None:
        """Gate one recap (or a file on it) for export. Raises
        "<record_label> not found." when out of scope."""
        scope = await self._export_scope()
        if scope == EXPORT_SCOPE_ADMIN:
            return
        not_found = GraphQLError(f"{record_label} not found.")
        if scope != EXPORT_SCOPE_CLIENT or tenant_id is None:
            raise not_found
        if not approved or archived_at is not None:
            raise not_found
        if tenant_id not in await self._export_member_tenant_ids():
            raise not_found

    async def _resolve_export_tenant(self, resolved_tenant_id: int | None):
        """Tenant for a tenant-wide export, plus whether the caller is
        limited to client-visible recaps."""
        scope = await self._export_scope()
        if scope == EXPORT_SCOPE_ADMIN:
            if resolved_tenant_id is None:
                raise GraphQLError("Tenant ID is required.")
            tenant = await self._get_tenant_without_membership(
                tenant_id=resolved_tenant_id
            )
            return tenant, False
        if scope != EXPORT_SCOPE_CLIENT:
            raise GraphQLError("You are not authorized to export recaps.")
        tenant = await self.get_user_tenant(
            self.info,
            tenant_id=resolved_tenant_id,
            user=self.user,
        )
        return tenant, True

    async def export_recaps_xlsx(self) -> str:
        """Generate an Excel report with all recaps for a tenant and return a signed URL."""
        if not isinstance(self.input, inputs.ExportRecapsXlsxInput):
            raise GraphQLError("Invalid input type.")

        resolved_tenant_id: int | None = None
        if self.input.tenant_id not in (None, ""):
            try:
                resolved_tenant_id = resolve_id_to_int(self.input.tenant_id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid tenant ID.")

        tenant, client_only = await self._resolve_export_tenant(resolved_tenant_id)
        start_date = self.input.start_date
        end_date = self.input.end_date

        frontend_base_url = settings.ADMIN_FRONTEND_URL

        @sync_to_async
        def build_xlsx_for_tenant():
            service = RecapQueriesService()
            queryset = service.get_filtered_queryset(
                tenant_id=tenant.id,
                start_date=start_date,
                end_date=end_date,
                approved=True if client_only else None,
            )
            if client_only:
                queryset = queryset.filter(archived_at__isnull=True)
            recaps = list(
                queryset.select_related(
                    "event__request__retailer",
                    "event__request__distributor",
                    "ambassador",
                    "ambassador__user",
                )
            )
            return build_recaps_xlsx(recaps, frontend_base_url=frontend_base_url)

        xlsx_bytes = await build_xlsx_for_tenant()

        timestamp = django_timezone.now().strftime("%Y%m%d%H%M%S")
        tenant_slug = slugify(getattr(tenant, "name", "") or "tenant")
        export_prefix = f"recaps/exports/{tenant_slug}-"
        blob_name = f"{export_prefix}{timestamp}.xlsx"

        @sync_to_async
        def delete_previous_exports():
            client = get_gcs_client()
            bucket = client.bucket(settings.GS_BUCKET_NAME)
            for blob in bucket.list_blobs(prefix=export_prefix):
                if blob.name != blob_name:
                    blob.delete()

        await delete_previous_exports()
        upload_bytes(
            blob_name,
            xlsx_bytes,
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )
        return public_url(blob_name)

    async def export_recap_xlsx(self) -> str:
        """Generate an Excel report for a single recap and return a signed URL."""
        if not isinstance(self.input, inputs.ExportRecapXlsxInput):
            raise GraphQLError("Invalid input type.")

        try:
            recap_id = resolve_id_to_int(self.input.id)
        except (TypeError, ValueError, GraphQLError):
            raise GraphQLError("Invalid recap ID.")

        frontend_base_url = settings.ADMIN_FRONTEND_URL

        # Cross-tenant READ gate (follow-up to #708) — resolve the recap's
        # owning tenant up front and authorize BEFORE building/uploading the
        # export. This accessor loaded a single Recap by raw PK gated only by
        # StrictIsAuthenticated, so any authenticated user could export
        # another tenant's recap data by guessing the id.
        @sync_to_async
        def fetch_recap_scope():
            return (
                models.Recap.objects.filter(id=recap_id)
                .values("event__tenant_id", "approved", "archived_at")
                .first()
            )

        row = await fetch_recap_scope()
        if row is None:
            raise GraphQLError("Recap not found.")
        await self._assert_recap_export_access(
            tenant_id=row["event__tenant_id"],
            approved=row["approved"],
            archived_at=row["archived_at"],
        )

        @sync_to_async
        def build_xlsx_for_recap():
            try:
                recap = (
                    RecapQueriesService()
                    .get_queryset()
                    .select_related(
                        "event__request__retailer",
                        "event__request__distributor",
                        "event__tenant",
                        "ambassador",
                        "ambassador__user",
                    )
                    .get(id=recap_id)
                )
            except models.Recap.DoesNotExist:
                return None, None, None
            tenant_name = getattr(getattr(recap, "event", None), "tenant", None)
            return (
                build_recaps_xlsx([recap], frontend_base_url=frontend_base_url),
                recap.uuid,
                getattr(tenant_name, "name", None),
            )

        xlsx_bytes, recap_uuid, tenant_name = await build_xlsx_for_recap()
        if xlsx_bytes is None or recap_uuid is None:
            raise GraphQLError("Recap not found.")

        timestamp = django_timezone.now().strftime("%Y%m%d%H%M%S")
        tenant_slug = slugify(tenant_name or "tenant")
        export_prefix = f"recaps/exports/{tenant_slug}-{recap_uuid}-"
        blob_name = f"{export_prefix}{timestamp}.xlsx"

        @sync_to_async
        def delete_previous_exports():
            client = get_gcs_client()
            bucket = client.bucket(settings.GS_BUCKET_NAME)
            for blob in bucket.list_blobs(prefix=export_prefix):
                if blob.name != blob_name:
                    blob.delete()

        await delete_previous_exports()
        upload_bytes(
            blob_name,
            xlsx_bytes,
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )
        return public_url(blob_name)

    async def export_custom_recaps_xlsx(self) -> str:
        """Generate an Excel report with all custom recaps for a tenant."""
        if not isinstance(self.input, inputs.ExportCustomRecapsXlsxInput):
            raise GraphQLError("Invalid input type.")

        resolved_tenant_id: int | None = None
        if self.input.tenant_id not in (None, ""):
            try:
                resolved_tenant_id = resolve_id_to_int(self.input.tenant_id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid tenant ID.")

        resolved_template_id: int | None = None
        if self.input.custom_recap_template_id not in (None, ""):
            try:
                resolved_template_id = resolve_id_to_int(
                    self.input.custom_recap_template_id
                )
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid custom recap template ID.")

        tenant, client_only = await self._resolve_export_tenant(resolved_tenant_id)

        start_date = self.input.start_date
        end_date = self.input.end_date
        frontend_base_url = settings.ADMIN_FRONTEND_URL

        @sync_to_async
        def build_xlsx_for_tenant():
            service = CustomRecapQueriesService()
            queryset = service.get_filtered_queryset(
                tenant_id=tenant.id,
                custom_recap_template_id=resolved_template_id,
                start_date=start_date,
                end_date=end_date,
                approved=True if client_only else None,
            )
            if client_only:
                queryset = queryset.filter(archived_at__isnull=True)
            custom_recaps = list(queryset)
            return build_recaps_xlsx(
                custom_recaps,
                frontend_base_url=frontend_base_url,
            )

        xlsx_bytes = await build_xlsx_for_tenant()

        timestamp = django_timezone.now().strftime("%Y%m%d%H%M%S")
        tenant_slug = slugify(getattr(tenant, "name", "") or "tenant")
        export_prefix = f"custom-recaps/exports/{tenant_slug}-"
        blob_name = f"{export_prefix}{timestamp}.xlsx"

        @sync_to_async
        def delete_previous_exports():
            client = get_gcs_client()
            bucket = client.bucket(settings.GS_BUCKET_NAME)
            for blob in bucket.list_blobs(prefix=export_prefix):
                if blob.name != blob_name:
                    blob.delete()

        await delete_previous_exports()
        upload_bytes(
            blob_name,
            xlsx_bytes,
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )
        return public_url(blob_name)

    async def export_custom_recap_xlsx(self) -> str:
        """Generate an Excel report for a single custom recap."""
        if not isinstance(self.input, inputs.ExportCustomRecapXlsxInput):
            raise GraphQLError("Invalid input type.")

        try:
            custom_recap_id = resolve_id_to_int(self.input.id)
        except (TypeError, ValueError, GraphQLError):
            raise GraphQLError("Invalid custom recap ID.")

        frontend_base_url = settings.ADMIN_FRONTEND_URL

        # Cross-tenant READ gate (follow-up to #708) — resolve the custom
        # recap's owning tenant up front and authorize BEFORE building the
        # export. Single-recap-by-id accessor previously gated only by
        # StrictIsAuthenticated. CustomRecap carries a direct tenant FK.
        @sync_to_async
        def fetch_custom_recap_scope():
            return (
                models.CustomRecap.objects.filter(id=custom_recap_id)
                .values("tenant_id", "approved", "archived_at")
                .first()
            )

        row = await fetch_custom_recap_scope()
        if row is None:
            raise GraphQLError("Custom recap not found.")
        await self._assert_recap_export_access(
            tenant_id=row["tenant_id"],
            approved=row["approved"],
            archived_at=row["archived_at"],
            record_label="Custom recap",
        )

        @sync_to_async
        def build_xlsx_for_custom_recap():
            try:
                custom_recap = CustomRecapQueriesService().get_queryset().get(
                    id=custom_recap_id
                )
            except models.CustomRecap.DoesNotExist:
                return None, None, None

            tenant_name = getattr(custom_recap.tenant, "name", None) or getattr(
                getattr(custom_recap, "event", None), "tenant", None
            )
            return (
                build_recaps_xlsx([custom_recap], frontend_base_url=frontend_base_url),
                custom_recap.uuid,
                getattr(tenant_name, "name", None)
                if not isinstance(tenant_name, str)
                else tenant_name,
            )

        xlsx_bytes, custom_recap_uuid, tenant_name = await build_xlsx_for_custom_recap()
        if xlsx_bytes is None or custom_recap_uuid is None:
            raise GraphQLError("Custom recap not found.")

        timestamp = django_timezone.now().strftime("%Y%m%d%H%M%S")
        tenant_slug = slugify(tenant_name or "tenant")
        export_prefix = f"custom-recaps/exports/{tenant_slug}-{custom_recap_uuid}-"
        blob_name = f"{export_prefix}{timestamp}.xlsx"

        @sync_to_async
        def delete_previous_exports():
            client = get_gcs_client()
            bucket = client.bucket(settings.GS_BUCKET_NAME)
            for blob in bucket.list_blobs(prefix=export_prefix):
                if blob.name != blob_name:
                    blob.delete()

        await delete_previous_exports()
        upload_bytes(
            blob_name,
            xlsx_bytes,
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )
        return public_url(blob_name)

    async def get_recap_file_download_url(self) -> str:
        """Return a signed download URL for a recap or custom recap file.

        Cross-tenant READ gate (follow-up to #708). This accessor returns a
        download URL for the file *content* of a recap file looked up by raw
        uuid. We resolve the file's owning tenant from its parent recap and
        authorize via ``_assert_recap_export_access``: admins (resolved from
        the DB row) get any tenant; clients only approved, un-archived recaps
        in their own tenants; everyone else is refused. A file with no
        resolvable parent recap (detached blob, recap=None) has no tenant and
        is denied.
        """
        if not isinstance(self.input, inputs.RecapFileDownloadUrlInput):
            raise GraphQLError("Invalid input type.")

        recap_file_uuid = str(self.input.uuid)

        @sync_to_async
        def fetch_recap_file():
            recap_file = (
                models.RecapFile.objects.select_related(
                    "recap",
                    "recap__event",
                )
                .filter(uuid=recap_file_uuid)
                .first()
            )
            if recap_file is not None:
                return recap_file
            return (
                models.CustomRecapFile.objects.select_related(
                    "custom_recap",
                    "custom_recap__event",
                )
                .filter(uuid=recap_file_uuid)
                .first()
            )

        recap_file = await fetch_recap_file()
        if recap_file is None:
            raise GraphQLError("Recap file not found.")

        # Derive the file's owning tenant from its parent recap. RecapFile
        # scopes tenant via recap.event.tenant_id; CustomRecapFile via
        # custom_recap.tenant_id. Both parents are select_related above so the
        # reads here are async-safe. A detached file (parent is None) yields
        # tenant_id=None, which the gate treats as a denial.
        if isinstance(recap_file, models.CustomRecapFile):
            parent = recap_file.custom_recap
            file_tenant_id = getattr(parent, "tenant_id", None)
            record_label = "Custom recap"
        else:
            parent = recap_file.recap
            event = getattr(parent, "event", None) if parent is not None else None
            file_tenant_id = getattr(event, "tenant_id", None)
            record_label = "Recap"

        # Clients only get files on approved, un-archived recaps in their
        # own tenants; ambassadors are refused (see _export_scope).
        await self._assert_recap_export_access(
            tenant_id=file_tenant_id,
            approved=bool(getattr(parent, "approved", False)),
            archived_at=getattr(parent, "archived_at", None),
            record_label=record_label,
        )

        file_field = getattr(recap_file, "file", None) or getattr(recap_file, "url", None)
        blob_name = extract_blob_name_from_url(str(file_field))
        if not blob_name:
            raise GraphQLError("Recap file not found.")
        return public_url(blob_name)
