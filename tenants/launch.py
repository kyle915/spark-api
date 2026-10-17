"""Shared tenant-launch service: the codified Drink Lyft pattern.

One place that creates a tenant the way the per-brand setup commands do —
seed defaults, clone a donor recap template, import the Product catalog,
mint the standing walk-up code once — so the admin launch wizard, the
``launch_tenant`` management command, and future setup commands all write
through the same models. No parallel store.

Invariants (see AGENTS.md):
- Slug resolution is EXACT (slug__iexact / request_url_name__iexact),
  never a loose substring match.
- An existing standing code is never reminted or rotated.
- Cloning reads the donor; it never writes to the donor tenant.
- The tenant Product catalog is the single source of truth for SKU
  selectors; catalog imports are idempotent by SKU, then by name.
- Recaps stay human-reviewed: nothing here touches recap approval —
  walk-up submits keep leaving ``approved=False``.
"""

from __future__ import annotations

import csv
import io
import json
import re
import secrets

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils.text import slugify

from urllib.parse import urlparse

import httpx
from ambassadors.models import AttendanceStatus, Skill
from events.models import (
    EventStatus,
    EventType,
    Product,
    ProductType,
    RequestStatus,
    RequestType,
)
from jobs.models import RateType
from jobs.models import Status as JobStatus
from recaps.models import (
    CustomField,
    CustomRecapFieldType,
    CustomRecapTemplate,
    FileRecapCategory,
    RecapSection,
    TypeOfGood,
)
from recaps.products_sampled import products_sampled_options_for_tenant
from tenants.models import Tenant, TenantTheme
from tenants.mutations import (
    DEFAULT_ATTENDANCE_STATUS_TEMPLATES,
    DEFAULT_EVENT_TYPES,
    DEFAULT_FILE_RECAP_CATEGORIES,
    DEFAULT_JOB_STATUS_TEMPLATES,
    DEFAULT_RATE_TYPES,
    DEFAULT_REQUEST_TYPES,
    DEFAULT_SKILLS,
    DEFAULT_STATUS_TEMPLATES,
    DEFAULT_TYPES_OF_GOOD,
)
from utils.utils import default_tenant_theme

CLIENT_BASE_URL = "https://client.igniteproductions.co"
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_SKU_DESCRIPTION = re.compile(r"^(?:Shopify )?SKU\s+(.+)$", re.IGNORECASE)


class LaunchError(ValueError):
    """A validation problem the wizard can show to ops verbatim."""


def normalize_slug(raw: str | None) -> str:
    return (raw or "").strip().lower()


def validate_slug(raw: str | None) -> str:
    slug = normalize_slug(raw)
    if not slug:
        raise LaunchError("Slug is required.")
    if len(slug) > 50:
        raise LaunchError("Slug must be 50 characters or fewer.")
    if not SLUG_PATTERN.match(slug):
        raise LaunchError(
            "Slug must be lowercase letters, numbers, and single hyphens "
            "(e.g. drink-lyft) — no spaces, underscores, or punctuation."
        )
    return slug


def resolve_tenant_by_exact_slug(slug: str) -> Tenant | None:
    """Exact slug, then exact request_url_name. Never a substring."""
    slug = normalize_slug(slug)
    rows = list(Tenant.objects.filter(slug__iexact=slug).order_by("id"))
    if not rows:
        rows = list(
            Tenant.objects.filter(request_url_name__iexact=slug).order_by("id")
        )
    if not rows:
        return None
    if len(rows) > 1:
        raise LaunchError(
            f"{len(rows)} tenants match slug {slug!r} exactly. "
            "Resolve the duplicate before launching."
        )
    return rows[0]


def slug_is_taken(slug: str, *, exclude_tenant_id: int | None = None) -> bool:
    slug = normalize_slug(slug)
    qs = Tenant.objects.filter(slug__iexact=slug)
    if exclude_tenant_id:
        qs = qs.exclude(id=exclude_tenant_id)
    if qs.exists():
        return True
    qs = Tenant.objects.filter(request_url_name__iexact=slug)
    if exclude_tenant_id:
        qs = qs.exclude(id=exclude_tenant_id)
    return qs.exists()


def derive_code_prefix(name: str, slug: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", slug or "") or re.findall(
        r"[A-Za-z0-9]+", name or ""
    )
    if not words:
        return "SP"
    if len(words) == 1:
        return words[0][:2].upper()
    return "".join(w[0] for w in words[:3]).upper()


def clean_code_prefix(raw: str | None, *, name: str = "", slug: str = "") -> str:
    cleaned = "".join(ch for ch in (raw or "").strip().upper() if ch.isalnum())
    if not cleaned:
        return derive_code_prefix(name, slug)
    if not 1 <= len(cleaned) <= 4:
        raise LaunchError("Check-in code prefix must be 1–4 letters/digits.")
    return cleaned


def mint_checkin_code(prefix: str) -> str:
    for _ in range(50):
        body = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        candidate = f"{prefix}-{body}"
        taken = Tenant.objects.filter(checkin_code__iexact=candidate).exists() or (
            Tenant.objects.filter(checkin_recap_code__iexact=candidate).exists()
        ) or Tenant.objects.filter(checkin_team_code__iexact=candidate).exists()
        if not taken:
            return candidate
    raise LaunchError("Could not mint a unique check-in code — try again.")


def seed_tenant_defaults(tenant: Tenant, creator) -> None:
    """The createTenant seed set, shared by the wizard and the command."""

    def _statuses(model_cls, templates, include_default: bool):
        for status in templates:
            payload = {
                "name": status["name"],
                "slug": status.get("slug") or slugify(status["name"]),
                "tenant": tenant,
                "created_by": creator,
            }
            if include_default:
                payload["is_default"] = status["is_default"]
            model_cls.objects.create(**payload)

    _statuses(RequestStatus, DEFAULT_STATUS_TEMPLATES, True)
    _statuses(EventStatus, DEFAULT_STATUS_TEMPLATES, True)
    _statuses(JobStatus, DEFAULT_JOB_STATUS_TEMPLATES, False)
    _statuses(AttendanceStatus, DEFAULT_ATTENDANCE_STATUS_TEMPLATES, False)
    for event_type in DEFAULT_EVENT_TYPES:
        EventType.objects.create(
            name=event_type["name"],
            slug=event_type.get("slug") or slugify(event_type["name"]),
            tenant=tenant,
            created_by=creator,
            is_default=event_type["is_default"],
        )
    for request_type in DEFAULT_REQUEST_TYPES:
        RequestType.objects.create(name=request_type, tenant=tenant, created_by=creator)
    for rate_type in DEFAULT_RATE_TYPES:
        RateType.objects.create(name=rate_type, tenant=tenant, created_by=creator)
    for category in DEFAULT_FILE_RECAP_CATEGORIES:
        FileRecapCategory.objects.create(
            name=category, tenant_id=tenant.id, created_by=creator
        )
    for type_of_good in DEFAULT_TYPES_OF_GOOD:
        TypeOfGood.objects.create(name=type_of_good, tenant=tenant, created_by=creator)
    for skill in DEFAULT_SKILLS:
        if not Skill.objects.filter(name__iexact=skill).exists():
            Skill.objects.create(name=skill, created_by=creator)


def create_tenant_with_defaults(
    *, name: str, slug: str, creator, logo_url: str | None = None
) -> Tenant:
    tenant = Tenant.objects.create(
        name=name.strip(),
        slug=slug,
        request_url_name=slug,
        image=(logo_url or "").strip() or None,
        created_by=creator,
    )
    seed_tenant_defaults(tenant, creator)
    return tenant


def apply_brand_color(tenant: Tenant, brand_color: str | None, creator) -> bool:
    color = (brand_color or "").strip()
    if not color:
        return False
    if not _HEX_COLOR.match(color):
        raise LaunchError("Brand color must be a hex color like #c5f546.")
    variables = default_tenant_theme()
    variables["--color-primary"] = color
    TenantTheme.objects.update_or_create(
        tenant=tenant,
        color_scheme="dark",
        defaults={
            "name": "default",
            "css_variables": variables,
            "created_by": creator,
            "updated_by": creator,
        },
    )
    return True


# ---------------------------------------------------------------------------
# Recap template cloning (ported from clone_recap_template; donor untouched)
# ---------------------------------------------------------------------------


def _swap_text(text: str, swaps: list[tuple[str, str]]) -> str:
    out = text or ""
    for old, new in swaps:
        if old:
            out = out.replace(old, new)
    return out


def parse_label_swaps(raw: list[str] | None) -> list[tuple[str, str]]:
    swaps: list[tuple[str, str]] = []
    for item in raw or []:
        if "=" not in item:
            raise LaunchError(f"Label swap expects OLD=NEW, got {item!r}.")
        old, new = item.split("=", 1)
        if not old:
            raise LaunchError("Label swap OLD side cannot be empty.")
        swaps.append((old, new))
    return swaps


def donor_template_structure(template: CustomRecapTemplate) -> list[dict]:
    fields = list(
        CustomField.objects.filter(custom_recap_template_id=template.id)
        .select_related("recap_section", "custom_field_type")
        .order_by("recap_section__order", "recap_section_id", "order", "id")
    )
    sections: list[dict] = []
    by_id: dict[int, dict] = {}
    for field in fields:
        section = field.recap_section
        bucket = by_id.get(section.id)
        if bucket is None:
            bucket = {"name": section.name, "order": section.order, "fields": []}
            by_id[section.id] = bucket
            sections.append(bucket)
        bucket["fields"].append(
            {
                "name": field.name,
                "type": field.custom_field_type.name,
                "field_type_id": field.custom_field_type_id,
                "required": bool(field.required),
                "order": field.order,
                "options": list(field.options or []),
            }
        )
    sections.sort(key=lambda s: (s["order"], s["name"]))
    return sections


def resolve_donor_photo_buckets(
    donor_tenant: Tenant, donor_template: CustomRecapTemplate
) -> list[dict]:
    raw = getattr(donor_tenant, "checkin_photo_buckets", None)
    if isinstance(raw, list):
        entries = raw
    elif isinstance(raw, dict):
        wanted = (donor_template.event_type.name or "").strip().lower()
        entries = None
        for key, value in raw.items():
            if (key or "").strip().lower() == wanted and isinstance(value, list):
                entries = value
                break
        if entries is None and isinstance(raw.get("default"), list):
            entries = raw["default"]
        if entries is None:
            entries = next(
                (v for v in raw.values() if isinstance(v, list)), []
            )
    else:
        entries = []
    out: list[dict] = []
    for entry in entries or []:
        if isinstance(entry, str):
            name = entry.strip()
            item = {"name": name} if name else None
        elif isinstance(entry, dict):
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            item = {"name": name}
            if entry.get("helper"):
                item["helper"] = str(entry["helper"])
            if entry.get("min"):
                item["min"] = int(entry["min"])
        else:
            continue
        if item:
            out.append(item)
    return out


def clone_recap_template(
    *,
    source: CustomRecapTemplate,
    target: Tenant,
    creator,
    new_name: str | None = None,
    swaps: list[tuple[str, str]] | None = None,
    products_from_target: bool = True,
) -> dict:
    """Clone donor sections/fields onto target. Never writes to the donor."""
    swaps = swaps or []
    target_options: list[str] | None = None
    if products_from_target:
        target_options = products_sampled_options_for_tenant(target)

    event_type_name = source.event_type.name
    event_type = EventType.objects.filter(
        tenant_id=target.id, name__iexact=event_type_name
    ).first()
    if event_type is None:
        event_type = EventType.objects.create(
            name=event_type_name,
            slug=slugify(event_type_name),
            tenant=target,
            created_by=creator,
            is_default=False,
        )

    template_name = (new_name or "").strip() or _swap_text(source.name, swaps)
    template, created = CustomRecapTemplate.objects.get_or_create(
        tenant_id=target.id,
        name=template_name,
        defaults={
            "event_type": event_type,
            "product_samples": source.product_samples,
            "sales_performance": source.sales_performance,
            "layout": dict(source.layout or {}),
            "created_by": creator,
        },
    )
    if template.event_type_id != event_type.id:
        template.event_type = event_type
        template.save(update_fields=["event_type", "updated_at"])

    sections_created = sections_reused = fields_created = fields_reused = 0
    for section_data in donor_template_structure(source):
        section, s_created = RecapSection.objects.get_or_create(
            tenant_id=target.id,
            name=section_data["name"],
            defaults={"order": section_data["order"], "created_by": creator},
        )
        sections_created += int(s_created)
        sections_reused += int(not s_created)
        for field_data in section_data["fields"]:
            field_name = _swap_text(field_data["name"], swaps)
            options = list(field_data["options"])
            if (
                target_options
                and field_name.strip().lower() == "products sampled"
            ):
                options = list(target_options)
            # Field types are global — reuse the donor's exact row.
            field_type = CustomRecapFieldType.objects.get(
                pk=field_data["field_type_id"]
            )
            field, f_created = CustomField.objects.get_or_create(
                custom_recap_template_id=template.id,
                recap_section_id=section.id,
                name=field_name,
                defaults={
                    "custom_field_type": field_type,
                    "required": field_data["required"],
                    "order": field_data["order"],
                    "options": options,
                    "created_by": creator,
                },
            )
            fields_created += int(f_created)
            fields_reused += int(not f_created)
            if not f_created and options and list(field.options or []) != options:
                field.options = options
                field.updated_by = creator
                field.save(update_fields=["options", "updated_by", "updated_at"])

    # Photo buckets + location mode ride along with the template.
    buckets = resolve_donor_photo_buckets(source.tenant, source)
    for bucket in buckets:
        _ensure_category(target, bucket["name"], creator)
    changed_fields: list[str] = []
    if buckets:
        target.checkin_photo_buckets = {event_type.name: buckets}
        changed_fields.append("checkin_photo_buckets")
    donor_mode = getattr(source.tenant, "checkin_location_mode", None)
    if donor_mode and target.checkin_location_mode != donor_mode:
        target.checkin_location_mode = donor_mode
        changed_fields.append("checkin_location_mode")
    donor_markets = getattr(source.tenant, "checkin_markets", None)
    if donor_markets and not target.checkin_markets:
        target.checkin_markets = donor_markets
        changed_fields.append("checkin_markets")
    if changed_fields:
        target.save(update_fields=changed_fields + ["updated_at"])

    return {
        "template": template,
        "template_created": created,
        "event_type": event_type,
        "sections_created": sections_created,
        "sections_reused": sections_reused,
        "fields_created": fields_created,
        "fields_reused": fields_reused,
        "photo_buckets": buckets,
        "location_mode": target.checkin_location_mode,
    }


def _ensure_category(tenant: Tenant, name: str, creator):
    existing = FileRecapCategory.objects.filter(
        tenant_id=tenant.id, name__iexact=name
    ).first()
    if existing:
        return existing
    return FileRecapCategory.objects.create(
        name=name, tenant_id=tenant.id, created_by=creator
    )


# ---------------------------------------------------------------------------
# Product catalog import (CSV paste/upload + Shopify collection JSON)
# ---------------------------------------------------------------------------

_CSV_ALIASES = {
    "name": "name",
    "product name": "name",
    "product": "name",
    "title": "name",
    "sku": "sku",
    "sku code": "sku",
    "image url": "image_url",
    "image_url": "image_url",
    "image": "image_url",
    "url": "image_url",
    "product type": "product_type",
    "product_type": "product_type",
    "type": "product_type",
    "category": "product_type",
}


def parse_catalog_csv(text: str) -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    errors: list[str] = []
    stripped = (text or "").strip()
    if not stripped:
        return rows, errors
    reader = csv.DictReader(io.StringIO(stripped))
    if not reader.fieldnames:
        return rows, ["Catalog CSV needs a header row: name, sku, image_url."]
    header_map: dict[str, str] = {}
    for raw_header in reader.fieldnames:
        key = (raw_header or "").strip().lower()
        if key in _CSV_ALIASES:
            header_map[raw_header] = _CSV_ALIASES[key]
    if "name" not in header_map.values():
        return rows, ["Catalog CSV needs a 'name' column (name, sku, image_url)."]
    for line_no, raw_row in enumerate(reader, start=2):
        row = {
            target: (raw_row.get(source) or "").strip()
            for source, target in header_map.items()
        }
        if not any(row.values()):
            continue
        if not row.get("name"):
            errors.append(f"Line {line_no}: missing product name — skipped.")
            continue
        rows.append(
            {
                "name": row["name"],
                "sku": row.get("sku", ""),
                "image_url": row.get("image_url", ""),
                "product_type": row.get("product_type", ""),
            }
        )
    return rows, errors


def parse_shopify_collection_json(text: str) -> tuple[list[dict], list[str]]:
    """Shopify /collections/<handle>/products.json or a bare product list.

    Only parses JSON the ops user pastes/uploads — never fetches a URL.
    """
    stripped = (text or "").strip()
    if not stripped:
        return [], []
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return [], [f"Shopify JSON did not parse: {exc}"]
    products = payload.get("products") if isinstance(payload, dict) else payload
    if not isinstance(products, list):
        return [], ["Shopify JSON needs a 'products' list."]
    rows: list[dict] = []
    errors: list[str] = []
    for idx, product in enumerate(products, start=1):
        if not isinstance(product, dict):
            errors.append(f"Product {idx}: not an object — skipped.")
            continue
        name = str(product.get("title") or product.get("name") or "").strip()
        if not name:
            errors.append(f"Product {idx}: missing title — skipped.")
            continue
        sku = ""
        variants = product.get("variants")
        if isinstance(variants, list):
            for variant in variants:
                if isinstance(variant, dict) and str(variant.get("sku") or "").strip():
                    sku = str(variant["sku"]).strip()
                    break
        if not sku:
            sku = str(product.get("sku") or "").strip()
        image_url = ""
        image = product.get("image")
        if isinstance(image, dict):
            image_url = str(image.get("src") or "").strip()
        images = product.get("images")
        if not image_url and isinstance(images, list) and images:
            first = images[0]
            if isinstance(first, dict):
                image_url = str(first.get("src") or "").strip()
        rows.append(
            {
                "name": name,
                "sku": sku,
                "image_url": image_url,
                "product_type": str(
                    product.get("product_type") or product.get("type") or ""
                ).strip(),
            }
        )
    return rows, errors


def parse_catalog_input(
    *, csv_text: str | None = None, shopify_json: str | None = None
) -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    errors: list[str] = []
    csv_rows, csv_errors = parse_catalog_csv(csv_text or "")
    rows.extend(csv_rows)
    errors.extend(csv_errors)
    json_rows, json_errors = parse_shopify_collection_json(shopify_json or "")
    rows.extend(json_rows)
    errors.extend(json_errors)
    return rows, errors


def _extract_sku(description: str | None) -> str:
    first_line = (description or "").splitlines()[0] if description else ""
    match = _SKU_DESCRIPTION.match(first_line.strip())
    return match.group(1).strip() if match else ""


def _download_image(url: str) -> tuple[str, bytes]:
    response = httpx.get(
        url,
        timeout=30.0,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; Spark-Onboarder/1.0)"},
    )
    response.raise_for_status()
    filename = urlparse(url).path.rsplit("/", 1)[-1] or "product.jpg"
    if not response.content:
        raise LaunchError(f"Product image {url} was empty.")
    return filename, response.content


def import_products(
    *,
    tenant: Tenant,
    creator,
    rows: list[dict],
    download_images: bool = False,
    image_downloader=None,
) -> dict:
    """Idempotent catalog import. Re-running the same rows creates nothing."""
    created = reused = updated = 0
    warnings: list[str] = []
    downloader = image_downloader or _download_image
    existing = list(Product.objects.filter(tenant_id=tenant.id).select_related("product_type"))
    by_sku: dict[str, Product] = {}
    by_name: dict[str, Product] = {}
    for product in existing:
        sku = _extract_sku(product.description)
        if sku:
            by_sku.setdefault(sku.lower(), product)
        by_name.setdefault((product.name or "").strip().lower(), product)

    type_cache: dict[str, ProductType] = {}
    for row in rows:
        name = (row.get("name") or "").strip()
        sku = (row.get("sku") or "").strip()
        image_url = (row.get("image_url") or "").strip()
        type_name = (row.get("product_type") or "").strip() or "Products"
        if not name:
            warnings.append("A catalog row had no name — skipped.")
            continue
        if len(name) > 50:
            warnings.append(f"{name!r} is over 50 characters — skipped, not truncated.")
            continue
        product = None
        if sku:
            product = by_sku.get(sku.lower())
        if product is None:
            product = by_name.get(name.lower())
        if product is not None:
            reused += 1
            changed: list[str] = []
            if sku and not _extract_sku(product.description):
                product.description = f"SKU {sku}"
                changed.append("description")
            if changed:
                product.updated_by = creator
                product.save(update_fields=changed + ["updated_by", "updated_at"])
                updated += 1
            if sku:
                by_sku[sku.lower()] = product
            by_name[name.lower()] = product
        else:
            ptype = type_cache.get(type_name.lower())
            if ptype is None:
                ptype = ProductType.objects.filter(
                    tenant_id=tenant.id, name__iexact=type_name
                ).first()
                if ptype is None:
                    ptype = ProductType.objects.create(
                        name=type_name[:50], tenant=tenant, created_by=creator
                    )
                type_cache[type_name.lower()] = ptype
            description = f"SKU {sku}" if sku else ""
            if image_url and not download_images:
                description = (
                    f"{description}\nImage: {image_url}" if description else f"Image: {image_url}"
                )
            product = Product.objects.create(
                name=name,
                description=description or None,
                product_type=ptype,
                tenant=tenant,
                created_by=creator,
            )
            created += 1
            if sku:
                by_sku[sku.lower()] = product
            by_name[name.lower()] = product
        if image_url and download_images and not product.image:
            try:
                filename, data = downloader(image_url)
                product.image.save(filename, ContentFile(data), save=True)
            except Exception as exc:  # image is best-effort; catalog row stands
                warnings.append(f"Image for {name!r} could not be downloaded: {exc}")
    return {
        "created": created,
        "reused": reused,
        "updated": updated,
        "warnings": warnings,
        "total": Product.objects.filter(tenant_id=tenant.id).count(),
    }


# ---------------------------------------------------------------------------
# Standing code + launch orchestration
# ---------------------------------------------------------------------------


def ensure_checkin_code(
    tenant: Tenant, *, prefix: str | None = None, name: str = "", slug: str = ""
) -> tuple[str, bool]:
    """Mint once. An existing code is returned untouched, forever."""
    existing = (tenant.checkin_code or "").strip()
    if existing:
        return existing, False
    cleaned = clean_code_prefix(prefix, name=name or tenant.name, slug=slug or tenant.slug or "")
    code = mint_checkin_code(cleaned)
    tenant.checkin_code = code
    tenant.save(update_fields=["checkin_code", "updated_at"])
    return code, True


def checkin_url(code: str | None) -> str | None:
    return f"{CLIENT_BASE_URL}/checkin/{code}" if code else None


def spark_form_url(slug: str) -> str:
    return f"{CLIENT_BASE_URL}/spark-form/{slug}"


def build_preview(
    *,
    name: str,
    slug: str,
    donor_template: CustomRecapTemplate | None,
    catalog_rows: list[dict],
    code_prefix: str | None = None,
    swaps: list[tuple[str, str]] | None = None,
    new_template_name: str | None = None,
) -> dict:
    slug = normalize_slug(slug)
    existing = None
    try:
        validate_slug(slug)
        existing = resolve_tenant_by_exact_slug(slug)
    except LaunchError:
        existing = None
    sections: list[dict] = []
    buckets: list[dict] = []
    location_mode = ""
    donor_info = None
    if donor_template is not None:
        swaps = swaps or []
        for section in donor_template_structure(donor_template):
            sections.append(
                {
                    "name": section["name"],
                    "order": section["order"],
                    "fields": [
                        {
                            "name": _swap_text(f["name"], swaps),
                            "type": f["type"],
                            "required": f["required"],
                            "options": list(f["options"]),
                        }
                        for f in section["fields"]
                    ],
                }
            )
        buckets = resolve_donor_photo_buckets(donor_template.tenant, donor_template)
        location_mode = donor_template.tenant.checkin_location_mode or ""
        donor_info = {
            "tenant_name": donor_template.tenant.name,
            "tenant_slug": donor_template.tenant.slug,
            "template_id": donor_template.id,
            "template_name": donor_template.name,
            "new_template_name": (new_template_name or "").strip()
            or _swap_text(donor_template.name, swaps),
            "event_type": donor_template.event_type.name,
        }
    prefix = clean_code_prefix(code_prefix, name=name, slug=slug) if slug else "SP"
    return {
        "name": name,
        "slug": slug,
        "existing_tenant": (
            {"id": existing.id, "name": existing.name, "slug": existing.slug}
            if existing
            else None
        ),
        "existing_checkin_code": (existing.checkin_code if existing else None),
        "checkin_url": (
            checkin_url(existing.checkin_code)
            if existing and existing.checkin_code
            else f"{CLIENT_BASE_URL}/checkin/{prefix}-XXXXXX (minted on launch)"
        ),
        "spark_form_url": spark_form_url(slug) if slug else "",
        "donor": donor_info,
        "sections": sections,
        "photo_buckets": buckets,
        "location_mode": location_mode,
        "catalog": [
            {"name": r.get("name", ""), "sku": r.get("sku", ""), "image_url": r.get("image_url", "")}
            for r in catalog_rows
        ],
        "catalog_count": len(catalog_rows),
    }


@transaction.atomic
def launch_tenant(
    *,
    creator,
    name: str,
    slug: str,
    brand_color: str | None = None,
    logo_url: str | None = None,
    donor_template_id: int | None = None,
    new_template_name: str | None = None,
    label_swaps: list[tuple[str, str]] | None = None,
    catalog_rows: list[dict] | None = None,
    code_prefix: str | None = None,
    download_images: bool = False,
) -> dict:
    name = (name or "").strip()
    if not name:
        raise LaunchError("Brand name is required.")
    slug = validate_slug(slug)
    tenant = resolve_tenant_by_exact_slug(slug)
    tenant_created = False
    if tenant is None:
        if slug_is_taken(slug):
            raise LaunchError(f"Slug {slug!r} is already in use.")
        tenant = create_tenant_with_defaults(
            name=name, slug=slug, creator=creator, logo_url=logo_url
        )
        tenant_created = True
    else:
        # Resume path: same slug = same tenant, never a duplicate.
        if not (tenant.request_url_name or "").strip():
            tenant.request_url_name = slug
            tenant.save(update_fields=["request_url_name", "updated_at"])
        if logo_url and not tenant.image:
            tenant.image = logo_url.strip()
            tenant.save(update_fields=["image", "updated_at"])

    if brand_color:
        apply_brand_color(tenant, brand_color, creator)

    clone_result = None
    donor_template = None
    if donor_template_id:
        donor_template = (
            CustomRecapTemplate.objects.select_related("tenant", "event_type")
            .filter(id=donor_template_id)
            .first()
        )
        if donor_template is None:
            raise LaunchError(f"No donor recap template with id {donor_template_id}.")
        if donor_template.tenant_id == tenant.id and not tenant_created:
            # Cloning a tenant onto itself would duplicate its own form.
            clone_result = None
        else:
            clone_result = clone_recap_template(
                source=donor_template,
                target=tenant,
                creator=creator,
                new_name=new_template_name,
                swaps=label_swaps or [],
                products_from_target=True,
            )

    catalog_result = import_products(
        tenant=tenant,
        creator=creator,
        rows=catalog_rows or [],
        download_images=download_images,
    ) if catalog_rows else {"created": 0, "reused": 0, "updated": 0, "warnings": [], "total": Product.objects.filter(tenant_id=tenant.id).count()}

    # If the catalog arrived after the clone, refresh Products Sampled pills
    # are live from the catalog at read time — nothing else to stamp.

    code, code_minted = ensure_checkin_code(
        tenant, prefix=code_prefix, name=name, slug=slug
    )
    if clone_result is not None:
        event_type = clone_result["event_type"]
        tenant.checkin_event_type = event_type
        tenant.save(update_fields=["checkin_event_type", "updated_at"])
        tenant.checkin_event_types.set([event_type])

    preview = build_preview(
        name=tenant.name,
        slug=slug,
        donor_template=donor_template,
        catalog_rows=catalog_rows or [],
        code_prefix=code_prefix,
        swaps=label_swaps or [],
        new_template_name=new_template_name,
    )
    return {
        "tenant": tenant,
        "tenant_created": tenant_created,
        "template": clone_result["template"] if clone_result else None,
        "clone": clone_result,
        "catalog": catalog_result,
        "checkin_code": code,
        "code_minted": code_minted,
        "checkin_url": checkin_url(code),
        "spark_form_url": spark_form_url(slug),
        "preview": preview,
    }
