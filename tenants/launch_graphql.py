"""GraphQL surface for the admin tenant launch wizard.

Admin-only. Both mutations delegate to tenants.launch (the shared service
the launch_tenant management command also uses), so the wizard and the
command write identical rows. previewTenantLaunch writes nothing;
launchTenant is idempotent — re-running with the same exact slug resumes
that tenant, reuses its template/catalog, and never remints its code.
"""

from __future__ import annotations

import strawberry
from asgiref.sync import sync_to_async
from strawberry import relay

from recaps.models import CustomRecapTemplate
from tenants.launch import (
    LaunchError,
    build_preview,
    launch_tenant,
    parse_catalog_input,
    parse_label_swaps,
    resolve_tenant_by_exact_slug,
    validate_slug,
)
from tenants.models import Tenant
from tenants.types import TenantType
from utils.graphql.inputs import SparkGraphQLInput
from utils.graphql.mixins import resolve_id_to_int
from utils.graphql.permissions import _is_admin_access, resolve_request_user_access


@strawberry.input
class CatalogRowInput:
    name: str
    sku: str | None = None
    image_url: str | None = None
    product_type: str | None = None


@strawberry.input
class TenantLaunchInput(SparkGraphQLInput):
    name: str
    slug: str
    brand_color: str | None = None
    logo_url: str | None = None
    donor_template_id: strawberry.ID | None = None
    new_template_name: str | None = None
    label_swaps: list[str] | None = None
    catalog_csv: str | None = None
    shopify_json: str | None = None
    catalog_rows: list[CatalogRowInput] | None = None
    code_prefix: str | None = None
    download_images: bool = False


@strawberry.type
class LaunchFieldPreview:
    name: str
    type: str
    required: bool
    options: list[str]


@strawberry.type
class LaunchSectionPreview:
    name: str
    order: int
    fields: list[LaunchFieldPreview]


@strawberry.type
class LaunchBucketPreview:
    name: str
    helper: str | None = None
    min: int | None = None


@strawberry.type
class LaunchCatalogPreview:
    name: str
    sku: str
    image_url: str


@strawberry.type
class LaunchExistingTenant:
    id: strawberry.ID
    name: str
    slug: str | None


@strawberry.type
class LaunchDonorInfo:
    tenant_name: str
    tenant_slug: str | None
    template_id: int
    template_name: str
    new_template_name: str
    event_type: str


@strawberry.type
class TenantLaunchPreview:
    name: str
    slug: str
    existing_tenant: LaunchExistingTenant | None
    existing_checkin_code: str | None
    checkin_url: str
    spark_form_url: str
    donor: LaunchDonorInfo | None
    sections: list[LaunchSectionPreview]
    photo_buckets: list[LaunchBucketPreview]
    location_mode: str
    catalog: list[LaunchCatalogPreview]
    catalog_count: int
    errors: list[str]


@strawberry.type
class TenantLaunchResponse:
    success: bool
    message: str
    tenant: TenantType | None = None
    tenant_created: bool = False
    template_id: int | None = None
    template_name: str | None = None
    checkin_code: str | None = None
    code_minted: bool = False
    checkin_url: str | None = None
    spark_form_url: str | None = None
    catalog_created: int = 0
    catalog_reused: int = 0
    catalog_total: int = 0
    warnings: list[str] = strawberry.field(default_factory=list)
    preview: TenantLaunchPreview | None = None
    client_mutation_id: strawberry.ID | None = None


@strawberry.type
class DonorTemplateOption:
    template_id: int
    template_name: str
    tenant_id: strawberry.ID
    tenant_name: str
    tenant_slug: str | None
    event_type: str
    field_count: int


def _preview_type(data: dict) -> TenantLaunchPreview:
    donor = data.get("donor")
    existing = data.get("existing_tenant")
    return TenantLaunchPreview(
        name=data["name"],
        slug=data["slug"],
        existing_tenant=(
            LaunchExistingTenant(
                id=strawberry.ID(str(existing["id"])),
                name=existing["name"],
                slug=existing["slug"],
            )
            if existing
            else None
        ),
        existing_checkin_code=data.get("existing_checkin_code"),
        checkin_url=data["checkin_url"],
        spark_form_url=data["spark_form_url"],
        donor=(
            LaunchDonorInfo(
                tenant_name=donor["tenant_name"],
                tenant_slug=donor["tenant_slug"],
                template_id=donor["template_id"],
                template_name=donor["template_name"],
                new_template_name=donor["new_template_name"],
                event_type=donor["event_type"],
            )
            if donor
            else None
        ),
        sections=[
            LaunchSectionPreview(
                name=s["name"],
                order=s["order"],
                fields=[
                    LaunchFieldPreview(
                        name=f["name"],
                        type=f["type"],
                        required=f["required"],
                        options=list(f["options"]),
                    )
                    for f in s["fields"]
                ],
            )
            for s in data["sections"]
        ],
        photo_buckets=[
            LaunchBucketPreview(
                name=b["name"], helper=b.get("helper"), min=b.get("min")
            )
            for b in data["photo_buckets"]
        ],
        location_mode=data["location_mode"] or "",
        catalog=[
            LaunchCatalogPreview(
                name=r["name"], sku=r["sku"], image_url=r["image_url"]
            )
            for r in data["catalog"]
        ],
        catalog_count=data["catalog_count"],
        errors=[],
    )


def _catalog_rows(input: TenantLaunchInput) -> tuple[list[dict], list[str]]:
    rows, errors = parse_catalog_input(
        csv_text=input.catalog_csv, shopify_json=input.shopify_json
    )
    for row in input.catalog_rows or []:
        rows.append(
            {
                "name": (row.name or "").strip(),
                "sku": (row.sku or "").strip(),
                "image_url": (row.image_url or "").strip(),
                "product_type": (row.product_type or "").strip(),
            }
        )
    return rows, errors


async def _require_admin(info) -> tuple[bool, object | None]:
    request = getattr(info.context, "request", None)
    user = getattr(request, "user", None) or getattr(info.context, "user", None)
    if not user or not getattr(user, "is_authenticated", False):
        return False, None
    role_slug, is_staff, is_super, email = await resolve_request_user_access(user)
    if not _is_admin_access(role_slug, is_staff, is_super, email):
        return False, None
    return True, user


@strawberry.type
class TenantLaunchQueries:
    @strawberry.field
    async def tenant_launch_donors(self, info) -> list[DonorTemplateOption]:
        """Every tenant's recap templates, for the wizard's donor picker."""
        allowed, _user = await _require_admin(info)
        if not allowed:
            return []

        def _load() -> list[DonorTemplateOption]:
            from recaps.models import CustomField

            out: list[DonorTemplateOption] = []
            templates = (
                CustomRecapTemplate.objects.select_related("tenant", "event_type")
                .exclude(tenant__name__istartswith="[ARCHIVED]")
                .order_by("tenant__name", "id")
            )
            for tpl in templates:
                out.append(
                    DonorTemplateOption(
                        template_id=tpl.id,
                        template_name=tpl.name,
                        tenant_id=strawberry.ID(str(tpl.tenant_id)),
                        tenant_name=tpl.tenant.name,
                        tenant_slug=tpl.tenant.slug,
                        event_type=tpl.event_type.name,
                        field_count=CustomField.objects.filter(
                            custom_recap_template_id=tpl.id
                        ).count(),
                    )
                )
            return out

        return await sync_to_async(_load)()


@strawberry.type
class TenantLaunchMutations:
    @relay.mutation
    async def preview_tenant_launch(
        self, info, input: TenantLaunchInput
    ) -> TenantLaunchResponse:
        allowed, _user = await _require_admin(info)
        if not allowed:
            return TenantLaunchResponse(
                success=False,
                message="Only Spark admins can launch a tenant.",
                client_mutation_id=input.client_mutation_id,
            )
        errors: list[str] = []
        slug = ""
        try:
            slug = validate_slug(input.slug)
        except LaunchError as exc:
            errors.append(str(exc))
        rows, catalog_errors = _catalog_rows(input)
        errors.extend(catalog_errors)
        try:
            swaps = parse_label_swaps(input.label_swaps)
        except LaunchError as exc:
            errors.append(str(exc))
            swaps = []

        donor = None
        if input.donor_template_id:
            try:
                donor_pk = resolve_id_to_int(input.donor_template_id)
            except Exception:
                donor_pk = None
                errors.append("Donor template id is not valid.")
            if donor_pk:
                donor = await sync_to_async(
                    lambda: CustomRecapTemplate.objects.select_related(
                        "tenant", "event_type"
                    )
                    .filter(id=donor_pk)
                    .first()
                )()
                if donor is None:
                    errors.append(f"No donor recap template with id {donor_pk}.")
        if slug and not errors:
            # Exact-slug collision with a *different* tenant name is a
            # resume, not an error — the preview says so explicitly.
            pass

        def _build() -> TenantLaunchResponse:
            data = build_preview(
                name=(input.name or "").strip(),
                slug=slug or (input.slug or "").strip().lower(),
                donor_template=donor,
                catalog_rows=rows,
                code_prefix=input.code_prefix,
                swaps=swaps,
                new_template_name=input.new_template_name,
            )
            preview = _preview_type(data)
            preview.errors = errors
            return TenantLaunchResponse(
                success=not errors,
                message=(
                    "Preview only — nothing was written."
                    if not errors
                    else "; ".join(errors)
                ),
                preview=preview,
                warnings=errors,
                client_mutation_id=input.client_mutation_id,
            )

        return await sync_to_async(_build)()

    @relay.mutation
    async def launch_tenant(
        self, info, input: TenantLaunchInput
    ) -> TenantLaunchResponse:
        allowed, user = await _require_admin(info)
        if not allowed or user is None:
            return TenantLaunchResponse(
                success=False,
                message="Only Spark admins can launch a tenant.",
                client_mutation_id=input.client_mutation_id,
            )
        rows, catalog_errors = _catalog_rows(input)
        if catalog_errors:
            return TenantLaunchResponse(
                success=False,
                message="; ".join(catalog_errors),
                warnings=catalog_errors,
                client_mutation_id=input.client_mutation_id,
            )
        try:
            swaps = parse_label_swaps(input.label_swaps)
        except LaunchError as exc:
            return TenantLaunchResponse(
                success=False,
                message=str(exc),
                client_mutation_id=input.client_mutation_id,
            )
        donor_pk: int | None = None
        if input.donor_template_id:
            try:
                donor_pk = resolve_id_to_int(input.donor_template_id)
            except Exception:
                return TenantLaunchResponse(
                    success=False,
                    message="Donor template id is not valid.",
                    client_mutation_id=input.client_mutation_id,
                )

        def _run() -> TenantLaunchResponse:
            try:
                result = launch_tenant(
                    creator=user,
                    name=input.name,
                    slug=input.slug,
                    brand_color=input.brand_color,
                    logo_url=input.logo_url,
                    donor_template_id=donor_pk,
                    new_template_name=input.new_template_name,
                    label_swaps=swaps,
                    catalog_rows=rows,
                    code_prefix=input.code_prefix,
                    download_images=bool(input.download_images),
                )
            except LaunchError as exc:
                return TenantLaunchResponse(
                    success=False,
                    message=str(exc),
                    client_mutation_id=input.client_mutation_id,
                )
            tenant: Tenant = result["tenant"]
            template = result["template"]
            catalog = result["catalog"]
            preview = _preview_type(result["preview"])
            return TenantLaunchResponse(
                success=True,
                message=(
                    f"{tenant.name} is live. "
                    f"Check-in: {result['checkin_url']} · "
                    f"Requests: {result['spark_form_url']}"
                ),
                tenant=tenant,
                tenant_created=result["tenant_created"],
                template_id=template.id if template else None,
                template_name=template.name if template else None,
                checkin_code=result["checkin_code"],
                code_minted=result["code_minted"],
                checkin_url=result["checkin_url"],
                spark_form_url=result["spark_form_url"],
                catalog_created=catalog["created"],
                catalog_reused=catalog["reused"],
                catalog_total=catalog["total"],
                warnings=list(catalog["warnings"]),
                preview=preview,
                client_mutation_id=input.client_mutation_id,
            )

        return await sync_to_async(_run)()
