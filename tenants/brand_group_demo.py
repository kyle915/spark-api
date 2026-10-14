"""Seed / purge the FAMUS · Alcohol Armor · Wook Water demo activations.

Kyle walks his team through the brand group with these. Each scenario in
``brand_group_demo_data`` becomes a real, linked activation built through the
production write paths:

  request (+ SKUs, + co-brand rows on multi-brand activations)
  → ``Event.objects.from_request``
  → ``checkin_web.submit_checkin_recap`` (co-activations split per brand via
    the real ``co_brands`` logic)
  → approved with the same stamp ``approveCustomRecap`` writes.

Guarantees:
  * Nothing leaves Spark — the whole run sits inside ``suppress_outbound()``:
    no email, push, Cloud Task, calendar sync, Sheets mirror, recap export or
    job-board post, and the approval skips the recap-approved notify kick.
  * Everything is labeled: request / event / recap / store names start with
    "DEMO · ", recaps carry a DEMO source label, BAs are "Demo BA · <name>".
  * Everything is tagged: every row is created by a demo user under
    ``DEMO_DOMAIN`` (placeholder domain the mailer never delivers to), so
    ``purge`` finds exactly these rows and nothing else.
  * Idempotent: a scenario whose demo request already exists is skipped.
"""

from __future__ import annotations

import io
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.db import transaction
from PIL import Image, ImageDraw, ImageFont

from ambassadors import checkin_web
from ambassadors.models import Ambassador
from events.co_brands import replace_request_co_brands
from events.event_confirmations import (
    PRODUCT_CATEGORY_SEPARATOR,
    catalog_product_options,
)
from events.market import resolve_geo_for_address
from events.models import (
    Event,
    EventType,
    Product,
    Request,
    RequestProduct,
    RequestStatus,
    RequestType,
    Retailer,
    SchedulingStatus,
    TimeZone,
)
from recaps import co_activation
from recaps.models import (
    CustomField,
    CustomFieldValue,
    CustomRecap,
    CustomRecapFile,
    CustomRecapProductSample,
    CustomRecapSalePerformance,
    CustomRecapTemplate,
    FileType,
)
from recaps.mutations import _stamp_recap_approval
from recaps.products_sampled import PRODUCTS_SAMPLED_FIELD
from recaps.recap_tokens import make_recap_token
from tenants.brand_group_demo_data import SCENARIOS, BrandResult, Scenario, BRAND_SLUGS
from tenants.models import Role, Tenant
from utils import gcs
from utils.outbound import suppress_outbound
from utils.tz import resolve_zoneinfo
from utils.utils import ROLE_ID

logger = logging.getLogger(__name__)
User = get_user_model()

DEMO_DOMAIN = "brand-group-demo.walkup.spark"
SEEDER_EMAIL = f"seeder@{DEMO_DOMAIN}"
PREFIX = "DEMO · "
SOURCE_LABEL = "DEMO data · not a real activation"
BLOB_STEM = "demo-"
REQUEST_NOTE = (
    "DEMO data for the FAMUS · Alcohol Armor · Wook Water walkthrough — not a real "
    "activation. Remove with the 'Seed brand group demo' workflow (mode=purge)."
)
ADMIN_BASE = "https://admin.igniteproductions.co"
CLIENT_BASE = "https://client.igniteproductions.co"
SUBMIT_DELAY = timedelta(minutes=25)
APPROVE_DELAY = timedelta(hours=2)


class DemoSetupError(Exception):
    """The brand group isn't in the shape the demo needs (nothing written)."""


@dataclass
class BrandPlan:
    tenant: Tenant
    template: CustomRecapTemplate
    field_values: list[dict]
    product_samples: list[dict]
    products: list[Product]


# ── lookups ──────────────────────────────────────────────────────────────


def _tenants() -> dict[str, Tenant]:
    found = {t.slug: t for t in Tenant.objects.filter(slug__in=BRAND_SLUGS)}
    missing = [s for s in BRAND_SLUGS if s not in found]
    if missing:
        raise DemoSetupError(f"tenant slug(s) not found: {', '.join(missing)}")
    groups = {t.brand_group_id for t in found.values()}
    if len(groups) != 1 or None in groups:
        raise DemoSetupError("the three brands are not in one brand group")
    return found


def _field(template: CustomRecapTemplate, label: str) -> CustomField:
    fields = list(CustomField.objects.filter(custom_recap_template=template))
    exact = [f for f in fields if f.name == label]
    if exact:
        return exact[0]
    hits = [f for f in fields if label.lower() in (f.name or "").lower()]
    if len(hits) != 1:
        raise DemoSetupError(
            f"{template.name}: field {label!r} matched {[f.name for f in hits] or 'nothing'}"
        )
    return hits[0]


def _template(tenant: Tenant, program: str) -> CustomRecapTemplate:
    template = co_activation.sibling_template(tenant, program)
    if template is None:
        raise DemoSetupError(f"{tenant.slug}: no {program!r} recap template")
    return template


def _brand_plan(
    tenant: Tenant, scenario: Scenario, brand: BrandResult, *, with_shared: bool
) -> BrandPlan:
    template = _template(tenant, scenario.program)
    values: list[dict] = []
    labels = {
        **(scenario.shared if with_shared else {}),
        **brand.results,
        **brand.feedback,
    }
    for label, value in labels.items():
        values.append(
            {"customFieldId": str(_field(template, label).id), "value": value}
        )

    options = catalog_product_options(tenant)
    products: list[Product] = []
    sampled_labels: list[str] = []
    samples: list[dict] = []
    for sku, qty in brand.skus:
        product = Product.objects.filter(tenant=tenant, name=sku).order_by("id").first()
        option = next(
            (
                o
                for o in options
                if o == sku or o.endswith(f"{PRODUCT_CATEGORY_SEPARATOR}{sku}")
            ),
            None,
        )
        if product is None or option is None:
            raise DemoSetupError(f"{tenant.slug}: SKU {sku!r} is not in the catalog")
        products.append(product)
        sampled_labels.append(option)
        samples.append({"productId": str(product.id), "quantity": qty})
    values.append(
        {
            "customFieldId": str(_field(template, PRODUCTS_SAMPLED_FIELD).id),
            "value": json.dumps(sampled_labels),
        }
    )
    return BrandPlan(tenant, template, values, samples, products)


def _timezone_row(iana: str) -> TimeZone | None:
    for row in TimeZone.objects.order_by("id"):
        zone = resolve_zoneinfo(row)
        if zone is not None and getattr(zone, "key", "") == iana:
            return row
    return None


def _window(scenario: Scenario) -> tuple[datetime, datetime]:
    zone = ZoneInfo(scenario.iana)
    start = datetime.combine(scenario.day, scenario.hours[0], tzinfo=zone)
    end = datetime.combine(scenario.day, scenario.hours[1], tzinfo=zone)
    return start, end


def _request_name(scenario: Scenario) -> str:
    return f"{PREFIX}{scenario.title}"


def demo_users():
    return User.objects.filter(email__iendswith=f"@{DEMO_DOMAIN}")


# ── demo accounts ────────────────────────────────────────────────────────


def _ensure_seeder() -> User:
    user = User.objects.filter(email__iexact=SEEDER_EMAIL).first()
    if user is not None:
        return user
    user = User(
        username=SEEDER_EMAIL,
        email=SEEDER_EMAIL,
        first_name="Spark",
        last_name="Demo data",
        role=Role.objects.get(id=ROLE_ID.Ambassadors),
        is_active=False,
    )
    user.set_unusable_password()
    user.save()
    return user


def _ensure_ba(first_name: str, seeder: User) -> Ambassador:
    email = f"ba.{first_name.lower()}@{DEMO_DOMAIN}"
    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        user = User(
            username=email,
            email=email,
            first_name="Demo BA ·",
            last_name=first_name,
            role=Role.objects.get(id=ROLE_ID.Ambassadors),
            is_active=False,
            created_by=seeder,
        )
        user.set_unusable_password()
        user.save()
    ambassador = Ambassador.objects.filter(user=user).first()
    if ambassador is None:
        ambassador = Ambassador.objects.create(
            user=user,
            coordinates=[],
            is_active=False,
            created_by=seeder,
            updated_by=seeder,
        )
    return ambassador


# ── photos ───────────────────────────────────────────────────────────────

_FONT_PATHS = (
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
)


def _font(size: int):
    for path in _FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _accent(tenant: Tenant) -> tuple[int, int, int]:
    theme = tenant.themes.first()
    raw = (
        (getattr(theme, "css_variables", None) or {}).get("--color-primary")
        or "#c5f546"
    ).lstrip("#")
    try:
        return tuple(int(raw[i : i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return (197, 245, 70)


def _product_image(product: Product, cache: dict) -> Image.Image | None:
    if product.id not in cache:
        image = None
        blob = (
            gcs.extract_blob_name_from_url(product.image.name)
            if product.image
            else None
        )
        data = gcs.download_blob_bytes(blob) if blob else None
        if data:
            try:
                image = Image.open(io.BytesIO(data)).convert("RGBA")
            except Exception:  # noqa: BLE001 — a bad catalog image just drops out
                image = None
        cache[product.id] = image
    return cache[product.id]


def _photo(label: str, idx: int, shots: list[Image.Image], accent) -> bytes:
    w, h = 1200, 800
    img = Image.new("RGB", (w, h), (24, 26, 30))
    draw = ImageDraw.Draw(img)
    for y in range(h):
        shade = 24 + int(22 * y / h)
        draw.line([(0, y), (w, y)], fill=(shade, shade + 2, shade + 6))
    draw.rectangle([30, 30, w - 30, h - 30], outline=accent, width=4)
    if shots and "receipt" not in label.lower():
        box_h = 470
        scaled = [
            s.resize((max(1, int(s.width * box_h / s.height)), box_h)) for s in shots
        ]
        total_w = sum(s.width for s in scaled) + 30 * (len(scaled) - 1)
        if total_w > w - 120:
            r = (w - 120) / total_w
            scaled = [
                s.resize((max(1, int(s.width * r)), max(1, int(s.height * r))))
                for s in scaled
            ]
            total_w = sum(s.width for s in scaled) + 30 * (len(scaled) - 1)
        x = (w - total_w) // 2
        for s in scaled:
            img.paste(s, (x, 150 + (box_h - s.height) // 2), s)
            x += s.width + 30
    else:
        draw.rectangle([w / 2 - 170, 170, w / 2 + 170, 620], fill=(238, 236, 228))
        for i, yy in enumerate(range(210, 600, 34)):
            draw.line(
                [(w / 2 - 130, yy), (w / 2 + (130 if i % 3 else 40), yy)],
                fill=(150, 150, 140),
                width=6,
            )
    draw.text(
        (w / 2, 85),
        f"DEMO · {label}",
        fill=(240, 240, 236),
        font=_font(46),
        anchor="mm",
    )
    draw.text(
        (w / 2, h - 75),
        f"Demo photo {idx} · not a real activation",
        fill=(170, 174, 164),
        font=_font(28),
        anchor="mm",
    )
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82)
    return buf.getvalue()


def _upload_photos(
    event: Event, scenario: Scenario, plans: list[BrandPlan], uploaded: list[str]
) -> list[dict]:
    buckets = {b["name"]: b["id"] for b in checkin_web.serialize_photo_buckets(event)}
    accent = _accent(plans[0].tenant)
    cache: dict = {}
    shots = [
        img
        for plan in plans
        for p in plan.products
        if (img := _product_image(p, cache)) is not None
    ]
    files = []
    for category, count in scenario.photos:
        slug = re.sub(r"[^a-z]+", "-", category.lower()).strip("-")
        for i in range(1, count + 1):
            blob = f"recap_files/checkin/{event.uuid}/{BLOB_STEM}{slug}-{i}.jpg"
            gcs.upload_bytes(
                blob, _photo(category, i, shots, accent), content_type="image/jpeg"
            )
            uploaded.append(blob)
            files.append({"blobName": blob, "category": str(buckets.get(category, ""))})
    return files


# ── seed ─────────────────────────────────────────────────────────────────


def _plans(scenario: Scenario, tenants: dict[str, Tenant]) -> list[BrandPlan]:
    if scenario.brands[0].brand != scenario.host:
        raise DemoSetupError(f"{scenario.key}: first brand must be the host")
    return [
        _brand_plan(tenants[b.brand], scenario, b, with_shared=(i == 0))
        for i, b in enumerate(scenario.brands)
    ]


def _existing_request(scenario: Scenario, tenants: dict[str, Tenant]) -> Request | None:
    return (
        Request.objects.filter(
            tenant=tenants[scenario.host],
            name=_request_name(scenario),
            created_by__email__iendswith=f"@{DEMO_DOMAIN}",
            deleted_at__isnull=True,
        )
        .order_by("id")
        .first()
    )


def _create_activation(
    scenario: Scenario, tenants: dict[str, Tenant], plans: list[BrandPlan], seeder: User
) -> CustomRecap:
    host = tenants[scenario.host]
    start, end = _window(scenario)
    event_type = EventType.objects.filter(
        tenant=host, name__iexact=scenario.program
    ).first()
    request_type = RequestType.objects.filter(
        tenant=host, name__iexact=scenario.program
    ).first()
    status = RequestStatus.objects.get_by_slug("approved", tenant=host.id)
    if event_type is None or request_type is None or status is None:
        raise DemoSetupError(
            f"{host.slug}: missing {scenario.program!r} event/request type or approved status"
        )
    if not FileType.objects.exists():
        raise DemoSetupError("no FileType rows — recap photos can't be filed")

    retailer = None
    if scenario.store:
        geo = resolve_geo_for_address(scenario.address)
        retailer = Retailer.objects.create(
            name=_request_name(scenario),
            address=scenario.address[:100],
            store_contact="",
            location_id=getattr(geo, "location_id", None),
            tenant=host,
            created_by=seeder,
            updated_by=seeder,
        )

    request = Request.objects.create(
        name=_request_name(scenario),
        date=start,
        start_time=start,
        end_time=end,
        address=scenario.address,
        coordinates=list(scenario.coordinates),
        notes=REQUEST_NOTE,
        reviewed=True,
        scheduling_status=SchedulingStatus.ALREADY_SCHEDULED,
        store_number=scenario.store[1] if scenario.store else None,
        timezone=_timezone_row(scenario.iana),
        retailer=retailer,
        request_type=request_type,
        status=status,
        tenant=host,
        created_by=seeder,
        updated_by=seeder,
        approved_by=seeder,
    )
    for product in plans[0].products:
        RequestProduct.objects.create(
            request=request,
            product=product,
            tenant=host,
            created_by=seeder,
            updated_by=seeder,
        )
    if scenario.is_co_activation:
        replace_request_co_brands(
            request,
            [
                {"tenant_id": p.tenant.id, "product_ids": [x.id for x in p.products]}
                for p in plans[1:]
            ],
        )

    event = async_to_sync(Event.objects.from_request)(
        request, created_by=seeder, event_type=event_type
    )
    ambassador = _ensure_ba(scenario.ba_first_name, seeder)

    uploaded: list[str] = []
    try:
        files = _upload_photos(event, scenario, plans, uploaded)
        store_name, store_number = scenario.store or ("", "")
        recap = checkin_web.submit_checkin_recap(
            event=event,
            ambassador=ambassador,
            template=plans[0].template,
            field_values=plans[0].field_values,
            files=files,
            total_engagements=None,
            product_samples=plans[0].product_samples,
            store_name=f"{PREFIX}{store_name}" if store_name else "",
            store_number=store_number,
            source_label=SOURCE_LABEL,
            co_brands=[
                {
                    "tenantId": str(p.tenant.id),
                    "fieldValues": p.field_values,
                    "productSamples": p.product_samples,
                    "files": [],
                }
                for p in plans[1:]
            ]
            if scenario.is_co_activation
            else None,
        )
    except Exception:
        for blob in uploaded:
            try:
                gcs.delete_blob(blob)
            except Exception:  # noqa: BLE001 — cleanup is best-effort
                logger.warning("demo seed: could not delete %s", blob)
        raise

    submitted = end + SUBMIT_DELAY
    for row in co_activation.activation_recaps(recap):
        _stamp_recap_approval(row, approved=True, actor=seeder)
        row.submitted_at = submitted
        row.approved_at = submitted + APPROVE_DELAY
        row.save()
    return recap


def recap_links(recap: CustomRecap) -> dict[str, str]:
    return {
        "admin": f"{ADMIN_BASE}/recap/view-custom/{recap.uuid}",
        "public": f"{CLIENT_BASE}/r/{make_recap_token('custom', int(recap.id))}",
    }


def _report_activation(out, request: Request) -> None:
    out.write(f"    request  {ADMIN_BASE}/request/view/{request.uuid}")
    host_event = Event.objects.filter(request=request).order_by("id").first()
    recaps = []
    if host_event is not None:
        host_recap = CustomRecap.objects.filter(event=host_event).order_by("id").first()
        if host_recap is not None:
            recaps = co_activation.activation_recaps(host_recap)
    for recap in recaps:
        samples = sum(
            s.quantity
            for s in CustomRecapProductSample.objects.filter(custom_recap=recap)
        )
        links = recap_links(recap)
        out.write(
            f"    recap    [{recap.tenant.slug}] {recap.name!r} sampled={recap.total_engagements} "
            f"skus={samples} approved={recap.approved} photos={recap.custom_recap_files.count()}"
        )
        out.write(f"             admin  {links['admin']}")
        out.write(f"             client {links['public']}")


def seed(*, apply: bool, out) -> dict:
    """Create every missing demo activation (dry-run unless ``apply``)."""
    tenants = _tenants()
    summary = {
        "created": [],
        "existing": [],
        "would_create": [],
        "blocked_outbound": [],
    }
    with suppress_outbound() as blocked:
        seeder = (
            _ensure_seeder()
            if apply
            else User.objects.filter(email__iexact=SEEDER_EMAIL).first()
        )
        for scenario in SCENARIOS:
            label = f"{scenario.key}: {_request_name(scenario)} ({scenario.program}, {scenario.day:%Y-%m-%d})"
            plans = _plans(scenario, tenants)
            existing = _existing_request(scenario, tenants)
            if existing is not None:
                out.write(f"= exists  {label}")
                _report_activation(out, existing)
                summary["existing"].append(scenario.key)
                continue
            brands = " + ".join(f"{p.tenant.name} ({p.template.name})" for p in plans)
            if not apply:
                out.write(f"+ would create {label} — {brands}")
                summary["would_create"].append(scenario.key)
                continue
            with transaction.atomic():
                _create_activation(scenario, tenants, plans, seeder)
            out.write(f"+ created {label} — {brands}")
            _report_activation(out, _existing_request(scenario, tenants))
            summary["created"].append(scenario.key)
        summary["blocked_outbound"] = list(blocked)
    kinds: dict[str, int] = {}
    for entry in summary["blocked_outbound"]:
        kind = entry.split(":", 1)[0]
        kinds[kind] = kinds.get(kind, 0) + 1
    out.write(f"outbound suppressed: {kinds or 'none'}")
    out.write(
        f"emails attempted: {kinds.get('email', 0)} (all suppressed; nothing sent)"
    )
    return summary


# ── purge ────────────────────────────────────────────────────────────────


def purge(*, apply: bool, out) -> dict:
    """Delete everything ``seed`` created (dry-run unless ``apply``)."""
    tenants = list(Tenant.objects.filter(slug__in=BRAND_SLUGS))
    users = demo_users()
    ambassadors = Ambassador.objects.filter(user__in=users)
    recaps = CustomRecap.objects.filter(ambassador__in=ambassadors)
    events = Event.objects.filter(created_by__in=users)
    requests = Request.objects.filter(created_by__in=users)
    retailers = Retailer.objects.filter(created_by__in=users)

    problems = []
    if (
        recaps.exclude(tenant__in=tenants).exists()
        or events.exclude(tenant__in=tenants).exists()
    ):
        problems.append("demo rows found outside the three brand tenants")
    if CustomRecap.objects.filter(event__in=events).exclude(id__in=recaps).exists():
        problems.append("a non-demo recap is filed on a demo event")
    if Event.objects.filter(request__in=requests).exclude(id__in=events).exists():
        problems.append("a non-demo event hangs off a demo request")
    if problems:
        raise DemoSetupError("refusing to purge: " + "; ".join(problems))

    event_uuids = {str(u) for u in events.values_list("uuid", flat=True)}
    blobs = set()
    for name in CustomRecapFile.objects.filter(custom_recap__in=recaps).values_list(
        "url", flat=True
    ):
        parts = (name or "").split("/")
        if (
            len(parts) == 4
            and parts[:2] == ["recap_files", "checkin"]
            and parts[2] in event_uuids
            and parts[3].startswith(BLOB_STEM)
        ):
            blobs.add(name)

    counts = {
        "recaps": recaps.count(),
        "events": events.count(),
        "requests": requests.count(),
        "retailers": retailers.count(),
        "ambassadors": ambassadors.count(),
        "users": users.count(),
        "photo_rows": CustomRecapFile.objects.filter(custom_recap__in=recaps).count(),
        "photo_blobs": len(blobs),
    }
    out.write(f"{'purging' if apply else 'would purge'}: {counts}")
    for request in requests.order_by("id"):
        out.write(f"  - [{request.tenant.slug}] {request.name}")
    if not apply:
        return counts

    with transaction.atomic():
        recap_ids = list(recaps.values_list("id", flat=True))
        for model in (
            CustomFieldValue,
            CustomRecapProductSample,
            CustomRecapSalePerformance,
            CustomRecapFile,
        ):
            model.objects.filter(custom_recap_id__in=recap_ids).delete()
        CustomRecap.objects.filter(
            id__in=recap_ids, co_activation_parent__isnull=False
        ).delete()
        CustomRecap.objects.filter(id__in=recap_ids).delete()
        Event.objects.filter(id__in=list(events.values_list("id", flat=True))).delete()
        request_ids = list(requests.values_list("id", flat=True))
        RequestProduct.objects.filter(request_id__in=request_ids).delete()
        Request.objects.filter(id__in=request_ids).delete()
        Retailer.objects.filter(
            id__in=list(retailers.values_list("id", flat=True))
        ).delete()
        ambassadors.delete()
        users.exclude(email__iexact=SEEDER_EMAIL).delete()
        users.delete()
    for blob in sorted(blobs):
        try:
            gcs.delete_blob(blob)
        except Exception:  # noqa: BLE001 — rows are gone; a stray blob is harmless
            logger.warning("demo purge: could not delete blob %s", blob)
    out.write("purged.")
    return counts
