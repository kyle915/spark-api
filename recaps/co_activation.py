"""Multi-brand activations: one BA, one activation, one recap per brand.

A brand group (``tenants.BrandGroup``) lets a BA sample several sibling
brands at one table. The BA files ONE recap form; on submit Spark writes:

* the host brand's ``CustomRecap`` on the host event (as today), holding the
  shared sections + the host brand's own per-brand sections, and
* one SLICE per co-brand: a ``CustomRecap`` on that brand's tenant, attached
  to a co-brand copy of the event (``Event.co_activation_host``), holding the
  shared sections + only that brand's per-brand answers, SKUs and photos.

Because every slice is an ordinary recap on its own tenant and event, each
brand's Recaps list, recap document, approved-recap email and dashboard
read only that brand's numbers with no special casing — and no recap ever
carries a sibling brand's per-brand numbers. All recaps of one activation
share ``co_activation_key``; reviewing the host recap reviews every slice.

Which sections are per-brand is template data:
``CustomRecapTemplate.layout["coBrandSections"]`` lists section names. A
template without it never offers co-brands.
"""

from __future__ import annotations

import logging
import uuid

from events.models import Event, EventStatus, EventType, Product, RequestCoBrand
from recaps import models as rmodels
from recaps.mutation_parts.file_categories import (
    _resolve_explicit_file_recap_category,
    _resolve_file_recap_category,
)
from recaps import types as recap_types
from recaps.spend_amount import guard_spend_amount
from tenants.models import Tenant
from utils.gcs import extract_blob_name_from_url

logger = logging.getLogger(__name__)

CO_BRAND_SECTIONS_KEY = "coBrandSections"


def co_brand_section_names(template) -> set[str]:
    """Lower-cased names of the template's per-brand sections (may be empty)."""
    layout = getattr(template, "layout", None) or {}
    if not isinstance(layout, dict):
        return set()
    names = layout.get(CO_BRAND_SECTIONS_KEY) or []
    if not isinstance(names, list):
        return set()
    return {str(n).strip().lower() for n in names if str(n).strip()}


def group_siblings(tenant) -> list[Tenant]:
    """The other tenants in ``tenant``'s brand group, by name."""
    group_id = getattr(tenant, "brand_group_id", None)
    if not group_id:
        return []
    return list(
        Tenant.active()
        .filter(brand_group_id=group_id)
        .exclude(id=tenant.id)
        .order_by("name", "id")
    )


def sibling_template(sibling, program_name: str | None):
    """The sibling's recap template for the same program (event type name)."""
    wanted = (program_name or "").strip()
    qs = rmodels.CustomRecapTemplate.objects.filter(tenant_id=sibling.id)
    if wanted:
        match = (
            qs.filter(event_type__name__iexact=wanted).order_by("id").first()
            or qs.filter(name__icontains=wanted).order_by("id").first()
        )
        if match is not None:
            return match
    return None


def program_name_for(event, template) -> str:
    event_type = getattr(event, "event_type", None)
    if event_type is not None and getattr(event_type, "name", None):
        return event_type.name
    tpl_type = getattr(template, "event_type", None)
    return getattr(tpl_type, "name", "") or ""


def offers_co_brands(event, template) -> bool:
    return bool(
        template is not None
        and co_brand_section_names(template)
        and group_siblings(event.tenant)
    )


def requested_co_brand_ids(event) -> list[int]:
    """Co-brands named on the request this event came from, if any."""
    request_id = getattr(event, "request_id", None)
    if not request_id:
        return []
    return list(
        RequestCoBrand.objects.filter(request_id=request_id)
        .order_by("id")
        .values_list("tenant_id", flat=True)
    )


def request_co_brand_product_ids(event, sibling_id: int) -> list[int]:
    request_id = getattr(event, "request_id", None)
    if not request_id:
        return []
    row = RequestCoBrand.objects.filter(
        request_id=request_id, tenant_id=sibling_id
    ).first()
    if row is None:
        return []
    return list(row.products.order_by("name").values_list("id", flat=True))


def ensure_co_brand_event(host_event, sibling, template, actor) -> Event:
    """The sibling's copy of ``host_event`` (created on first use)."""
    program = getattr(getattr(host_event, "event_type", None), "name", "") or ""
    event_type = (
        EventType.objects.filter(tenant_id=sibling.id, name__iexact=program).first()
        if program
        else None
    ) or getattr(template, "event_type", None)
    fields = {
        "name": host_event.name,
        "date": host_event.date,
        "start_time": host_event.start_time,
        "end_time": host_event.end_time,
        "address": host_event.address or "",
        "coordinates": host_event.coordinates,
        "timezone_id": host_event.timezone_id,
        "state_id": host_event.state_id,
        "event_type": event_type,
        "custom_recap_template": template,
    }
    event = Event.objects.filter(
        co_activation_host=host_event, tenant_id=sibling.id
    ).first()
    if event is None:
        host_slug = getattr(getattr(host_event, "status", None), "slug", None)
        status = (
            EventStatus.objects.get_by_slug(slug=host_slug, tenant=sibling.id)
            if host_slug
            else None
        ) or EventStatus.objects.get_default(sibling.id)
        return Event.objects.create(
            tenant=sibling,
            co_activation_host=host_event,
            status=status,
            created_by=actor,
            updated_by=actor,
            **fields,
        )
    changed = []
    for key, value in fields.items():
        if getattr(event, key) != value:
            setattr(event, key, value)
            changed.append(key)
    if changed:
        event.updated_by = actor
        event.save()
    return event


def activation_root(recap):
    return recap.co_activation_parent if recap.co_activation_parent_id else recap


def activation_recaps(recap) -> list:
    """Every recap of ``recap``'s co-activation, host first (or just itself)."""
    root = activation_root(recap)
    slices = list(
        rmodels.CustomRecap.objects.filter(co_activation_parent=root).order_by("id")
    )
    return [root, *slices]


def co_activation_brand_names(recap) -> list[str]:
    """Names of the OTHER brands on ``recap``'s activation (names only)."""
    if not getattr(recap, "co_activation_key", None):
        return []
    return list(
        rmodels.CustomRecap.objects.filter(co_activation_key=recap.co_activation_key)
        .exclude(tenant_id=recap.tenant_id)
        .order_by("tenant__name")
        .values_list("tenant__name", flat=True)
        .distinct()
    )


def _section_key(field) -> str:
    section = getattr(field, "recap_section", None)
    return (getattr(section, "name", "") or "").strip().lower()


def _field_key(field) -> tuple[str, str]:
    return _section_key(field), (field.name or "").strip().lower()


def _delete_slice(slice_recap) -> None:
    rmodels.CustomFieldValue.objects.filter(custom_recap=slice_recap).delete()
    rmodels.CustomRecapProductSample.objects.filter(custom_recap=slice_recap).delete()
    rmodels.CustomRecapFile.objects.filter(custom_recap=slice_recap).delete()
    event_id = slice_recap.event_id
    slice_recap.delete()
    event = Event.objects.filter(id=event_id, co_activation_host__isnull=False).first()
    if event is not None and not rmodels.CustomRecap.objects.filter(event=event).exists():
        event.delete()


def _sibling_category(sibling_id: int, name: str | None, cache: dict):
    key = (name or "").strip().lower()
    if key not in cache:
        found = None
        if key:
            found = (
                rmodels.FileRecapCategory.objects.filter(
                    tenant_id=sibling_id, name__iexact=name.strip()
                )
                .order_by("id")
                .first()
            )
        cache[key] = found or _resolve_file_recap_category("1", tenant_id=sibling_id)
    return cache[key]


def write_co_brand_slices(
    *,
    host_recap,
    host_event,
    ambassador,
    actor,
    co_brands: list[dict] | None,
    allowed_blob_prefix: str,
) -> list:
    """Create/refresh one slice per requested co-brand; drop the rest.

    ``co_brands`` entries: ``{"tenantId", "fieldValues": [{customFieldId,
    value}], "productSamples": [{productId, quantity}], "files": [{blobName,
    category?}]}``. Unknown tenants (not in the host's group), tenants
    without a matching template, and ids outside a sibling's own template /
    catalog / upload prefix are ignored. Call inside the host recap's write
    transaction, after the host recap's values and files are written.
    """
    host_template = host_recap.custom_recap_template
    siblings = {t.id: t for t in group_siblings(host_event.tenant)}
    program = program_name_for(host_event, host_template)
    host_per_brand = co_brand_section_names(host_template)

    wanted: dict[int, dict] = {}
    if host_per_brand:
        for entry in co_brands or []:
            if not isinstance(entry, dict):
                continue
            try:
                tenant_id = int(str(entry.get("tenantId") or entry.get("tenant_id")))
            except (TypeError, ValueError):
                continue
            if tenant_id in siblings and tenant_id not in wanted:
                wanted[tenant_id] = entry

    existing = {
        s.tenant_id: s
        for s in rmodels.CustomRecap.objects.filter(co_activation_parent=host_recap)
    }
    for tenant_id, stale in existing.items():
        if tenant_id not in wanted:
            _delete_slice(stale)

    shared: dict[tuple[str, str], str] = {}
    for cfv in rmodels.CustomFieldValue.objects.filter(
        custom_recap=host_recap
    ).select_related("custom_field__recap_section"):
        key = _field_key(cfv.custom_field)
        if key[0] not in host_per_brand:
            shared[key] = cfv.value
    host_files = list(
        rmodels.CustomRecapFile.objects.filter(custom_recap=host_recap)
        .select_related("file_recap_category", "file_type")
        .order_by("id")
    )

    key = host_recap.co_activation_key or uuid.uuid4()
    slices = []
    for tenant_id, entry in wanted.items():
        sibling = siblings[tenant_id]
        template = sibling_template(sibling, program)
        if template is None:
            logger.warning(
                "co-activation: %s has no %r template; skipping slice",
                sibling.slug,
                program,
            )
            continue
        slice_recap = _write_slice(
            host_recap=host_recap,
            host_event=host_event,
            sibling=sibling,
            template=template,
            entry=entry,
            shared=shared,
            host_files=host_files,
            ambassador=ambassador,
            actor=actor,
            key=key,
            existing=existing.get(tenant_id),
            allowed_blob_prefix=allowed_blob_prefix,
        )
        slices.append(slice_recap)

    new_key = key if slices else None
    if host_recap.co_activation_key != new_key:
        host_recap.co_activation_key = new_key
        host_recap.save(update_fields=["co_activation_key", "updated_at"])
    return slices


# Host-recap facts every slice repeats (who/where/when, 3rd-party flags).
_MIRRORED_FIELDS = (
    "name",
    "submitted_at",
    "timezone",
    "state",
    "used_corpo_card",
    "filling_for_ambassador",
    "late",
    "incomplete",
    "external_ba_name",
    "is_third_party",
    "exclude_from_aggregates",
    "source_label",
    "typed_store_name",
    "typed_store_address",
)


def _write_slice(
    *,
    host_recap,
    host_event,
    sibling,
    template,
    entry: dict,
    shared: dict,
    host_files: list,
    ambassador,
    actor,
    key,
    existing,
    allowed_blob_prefix: str,
):
    event = ensure_co_brand_event(host_event, sibling, template, actor)
    mirrored = {name: getattr(host_recap, name) for name in _MIRRORED_FIELDS}
    if existing is None:
        slice_recap = rmodels.CustomRecap.objects.create(
            event=event,
            ambassador=ambassador,
            tenant=sibling,
            custom_recap_template=template,
            created_by=actor,
            co_activation_key=key,
            co_activation_parent=host_recap,
            **mirrored,
        )
    else:
        slice_recap = existing
        for name, value in mirrored.items():
            setattr(slice_recap, name, value)
        slice_recap.event = event
        slice_recap.custom_recap_template = template
        slice_recap.co_activation_key = key
        slice_recap.updated_by = actor
        slice_recap.save()
        rmodels.CustomFieldValue.objects.filter(custom_recap=slice_recap).delete()
        rmodels.CustomRecapProductSample.objects.filter(
            custom_recap=slice_recap
        ).delete()

    per_brand = co_brand_section_names(template)
    fields = list(
        rmodels.CustomField.objects.filter(custom_recap_template=template)
        .select_related("recap_section", "custom_field_type")
    )
    by_id = {f.id: f for f in fields}
    given: dict[int, str] = {}
    for fv in entry.get("fieldValues") or entry.get("field_values") or []:
        if not isinstance(fv, dict):
            continue
        try:
            field_id = int(str(fv.get("customFieldId") or fv.get("custom_field_id")))
        except (TypeError, ValueError):
            continue
        field = by_id.get(field_id)
        value = fv.get("value")
        if field is None or value is None or _section_key(field) not in per_brand:
            continue
        given[field_id] = str(
            guard_spend_amount(
                field.name,
                getattr(field.custom_field_type, "name", "") or "",
                value,
            )
        )

    pairs = []
    for field in fields:
        if _section_key(field) in per_brand:
            value = given.get(field.id)
        else:
            value = shared.get(_field_key(field))
        if value is None:
            continue
        rmodels.CustomFieldValue.objects.create(
            custom_recap=slice_recap,
            custom_field=field,
            value=value,
            created_by=actor,
        )
        pairs.append((field.name, value))

    sampled = recap_types._consumers_sampled_from_fields(pairs)
    if slice_recap.total_engagements != sampled:
        slice_recap.total_engagements = sampled
        slice_recap.save(update_fields=["total_engagements", "updated_at"])

    catalog = set(
        Product.objects.filter(tenant_id=sibling.id).values_list("id", flat=True)
    )
    for sample in entry.get("productSamples") or entry.get("product_samples") or []:
        if not isinstance(sample, dict):
            continue
        try:
            product_id = int(str(sample.get("productId") or sample.get("product_id")))
            qty = int(sample.get("quantity"))
        except (TypeError, ValueError):
            continue
        if qty <= 0 or product_id not in catalog:
            continue
        rmodels.CustomRecapProductSample.objects.create(
            custom_recap=slice_recap,
            product_id=product_id,
            quantity=qty,
            created_by=actor,
        )

    have = set(
        rmodels.CustomRecapFile.objects.filter(custom_recap=slice_recap).values_list(
            "url", flat=True
        )
    )
    category_cache: dict = {}
    for host_file in host_files:
        blob = host_file.url.name if host_file.url else ""
        if not blob or blob in have:
            continue
        have.add(blob)
        rmodels.CustomRecapFile.objects.create(
            name=host_file.name,
            url=blob,
            file_type=host_file.file_type,
            file_recap_category=_sibling_category(
                sibling.id,
                getattr(host_file.file_recap_category, "name", None),
                category_cache,
            ),
            custom_recap=slice_recap,
            approved=False,
            created_by=actor,
        )

    default_file_type = host_files[0].file_type if host_files else None
    if default_file_type is None:
        default_file_type = rmodels.FileType.objects.first()
    for file_input in entry.get("files") or []:
        if not isinstance(file_input, dict) or default_file_type is None:
            continue
        raw = file_input.get("blobName") or file_input.get("blob_name")
        blob = extract_blob_name_from_url(raw)
        if not blob or not blob.startswith(allowed_blob_prefix) or blob in have:
            continue
        have.add(blob)
        category = None
        raw_category = str(file_input.get("category") or "").strip()
        if raw_category:
            category = _resolve_explicit_file_recap_category(
                raw_category, tenant_id=sibling.id
            )
        rmodels.CustomRecapFile.objects.create(
            name=f"{sibling.name} photo for {host_recap.name}",
            url=blob,
            file_type=default_file_type,
            file_recap_category=category
            or _resolve_file_recap_category("1", tenant_id=sibling.id),
            custom_recap=slice_recap,
            approved=False,
            created_by=actor,
        )
    return slice_recap

