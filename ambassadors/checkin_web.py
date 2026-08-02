"""Public web check-in — the service layer behind ``/api/public/checkin/*``.

A BA opens a shareable per-event link (``/checkin/<walkup_code>``), identifies
themselves (name + phone, email optional), clocks in, works, clocks out, and
files the event's full custom-template recap — all in the browser, no login and
no app. It's the web twin of the mobile walk-up flow (``ambassadors/walkup.py``
+ ``spark-mobile`` ``WalkupCodeScreen``/``RecapSubmitScreen``) and reuses the
same primitives:

* the event's ``walkup_code`` is the link (an admin generates it — walk-ups must
  be enabled for the event);
* a self-identified BA becomes an inactive Ambassador + a ``source=walkup``
  ``AmbassadorEvent`` that stays ``is_approved=False`` until an admin confirms it
  in the Walk-ups queue, so nothing counts in KPI/payroll until reviewed;
* clock in/out are plain ``Attendance`` rows;
* the recap is a normal ``CustomRecap`` (created_by = the BA's own user), so it
  lands in the recap list / dashboards exactly like an app-filed one.

Everything here is pure-sync so the public sync views (``events/checkin_views``)
can call it directly. The only async work — the post-submit data-quality guard
and the admin "recap ready" notification — is offloaded to a fresh thread with
its own event loop (never ``asyncio.run()`` on the caller's thread), matching
``ambassadors/push.py::_send_push_to_user_sync`` so a nested thread-sensitive DB
write can't deadlock the ASGI worker.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import re
import secrets

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone as dj_tz

logger = logging.getLogger(__name__)
User = get_user_model()


# --------------------------------------------------------------------------
# Event + code resolution
# --------------------------------------------------------------------------
def resolve_event_by_code(code: str):
    """Resolve a walk-up code to its Event (or ``None``). Enforces the code's
    expiry, matching the mobile ``resolve_walkup_code`` contract."""
    from events.models import Event

    clean = (code or "").strip().upper()
    if not clean:
        return None
    event = (
        Event.objects.select_related("tenant", "request", "retailer", "location", "state", "timezone")
        .filter(walkup_code__iexact=clean)
        .first()
    )
    if not event:
        return None
    exp = getattr(event, "walkup_code_expires_at", None)
    if exp and exp < dj_tz.now():
        return None
    return event


# --------------------------------------------------------------------------
# Template resolution + serialization
# --------------------------------------------------------------------------
def resolve_template_for_event(event):
    """The recap template for an event — mirrors the resolution order in
    ``events/types.py::custom_recap_template`` (direct FK → template of a recap
    already filed for this event → tenant+event_type match → tenant's sole
    template) so the web check-in renders the SAME template as app + desktop."""
    from recaps.models import CustomRecap, CustomRecapTemplate

    if getattr(event, "custom_recap_template_id", None):
        return CustomRecapTemplate.objects.filter(
            id=event.custom_recap_template_id
        ).first()
    if not event.tenant_id:
        return None
    existing_tpl_id = (
        CustomRecap.objects.filter(event_id=event.id)
        .order_by("-id")
        .values_list("custom_recap_template_id", flat=True)
        .first()
    )
    if existing_tpl_id:
        tpl = CustomRecapTemplate.objects.filter(id=existing_tpl_id).first()
        if tpl:
            return tpl
    tenant_qs = CustomRecapTemplate.objects.filter(tenant_id=event.tenant_id)
    if getattr(event, "event_type_id", None):
        match = tenant_qs.filter(event_type_id=event.event_type_id).order_by("id").first()
        if match:
            return match
    if tenant_qs.count() == 1:
        return tenant_qs.first()
    return None


def _event_products(event):
    """Per-SKU sampling list for the event (from its Request's products),
    reusing the same source as ``shiftContext``. Empty when the event has no
    request/products — the FE then hides the PRODUCTS SAMPLED section."""
    from events.models import RequestProduct
    from utils.gcs import extract_blob_name_from_url, public_url

    request = getattr(event, "request", None)
    if request is None:
        return []
    out = []
    rp_qs = (
        RequestProduct.objects.select_related("product")
        .filter(request=request)
        .order_by("id")
    )
    for rp in rp_qs:
        product = getattr(rp, "product", None)
        if product is None:
            continue
        name = getattr(product, "name", None)
        if not name:
            continue
        image_url = None
        field_file = getattr(product, "image", None)
        if field_file:
            try:
                blob = field_file.name
            except Exception:  # noqa: BLE001
                blob = str(field_file)
            try:
                image_url = public_url(extract_blob_name_from_url(blob))
            except Exception:  # noqa: BLE001
                image_url = None
        out.append({"id": str(product.id), "name": name, "imageUrl": image_url})
    return out


def serialize_template(event) -> dict | None:
    """Shape the event's custom recap template for the public page: sections in
    order, each with its fields (type / options / required) in order. Field ids
    are plain integers (not Relay global ids) — the submit endpoint looks them
    up by id scoped to the template. ``None`` when the event has no template."""
    tpl = resolve_template_for_event(event)
    if tpl is None:
        return None

    from recaps.models import CustomField

    fields = list(
        CustomField.objects.filter(custom_recap_template_id=tpl.id)
        .select_related("custom_field_type", "recap_section")
        .order_by("recap_section__order", "recap_section__id", "order", "id")
    )
    # Group into sections preserving the queryset's (section-ordered) order.
    sections: list[dict] = []
    by_section: dict[int, dict] = {}
    for f in fields:
        sec = f.recap_section
        sec_id = sec.id if sec else 0
        if sec_id not in by_section:
            entry = {
                "id": str(sec_id),
                "name": (sec.name if sec else "Details"),
                "fields": [],
            }
            by_section[sec_id] = entry
            sections.append(entry)
        by_section[sec_id]["fields"].append(
            {
                "id": str(f.id),
                "name": f.name,
                "required": bool(f.required),
                "type": (getattr(f.custom_field_type, "name", "") or "text").lower(),
                "options": list(f.options or []),
            }
        )

    return {
        "id": str(tpl.id),
        "name": tpl.name,
        "productSamples": bool(tpl.product_samples),
        "sections": sections,
        "products": _event_products(event) if tpl.product_samples else [],
    }


# --------------------------------------------------------------------------
# Identity — get-or-create a lightweight (pending) walk-up BA
# --------------------------------------------------------------------------
def _normalize_phone(phone: str | None) -> str:
    return re.sub(r"\D", "", phone or "")


def _synth_email(phone_digits: str) -> str:
    """A stable pseudo-email so a returning BA (same phone) reuses their account
    instead of spawning a duplicate. Never used to send mail — the account has
    an unusable password and stays pending until an admin confirms it."""
    token = phone_digits or secrets.token_hex(5)
    return f"checkin-{token}@walkup.spark"


def get_or_create_checkin_ambassador(
    *, first_name: str, last_name: str, phone: str, email: str | None
):
    """Get-or-create the (inactive/pending) Ambassador for a self-identified
    walk-up BA. Returns ``(ambassador, created)``. Pure-sync.

    SECURITY: identity is keyed ONLY on a phone-derived pseudo-email in an
    isolated ``@walkup.spark`` namespace — never on the typed email and never a
    lookup against real Spark accounts. This is a PUBLIC link: matching a typed
    email/phone to an existing real user would let anyone attribute a walk-up
    (and a recap) to, say, an admin's account by typing their address. The stub
    dedups a *returning* walk-up (same phone → same stub) and stays pending
    until an admin confirms it in the Walk-ups queue, exactly like the mobile
    walk-up sign-up (which likewise never reuses an existing account). The typed
    email is accepted for future contact but is deliberately not used for
    identity. An already-onboarded BA's walk-up is reconciled at confirm time."""
    from ambassadors.models import Ambassador
    from tenants.models import Role

    phone_digits = _normalize_phone(phone)
    # Always the isolated stub namespace — never the typed email, never a real
    # account. _synth_email falls back to a random token if the phone is blank
    # (the view already requires a phone, so that's just belt-and-suspenders).
    lookup_email = _synth_email(phone_digits)

    from django.db import IntegrityError

    with transaction.atomic():
        user = User.objects.filter(email__iexact=lookup_email).first()
        created = False
        if user is None:
            try:
                role = Role.objects.get(slug=Role.AMBASSADOR_SLUG)
            except Role.DoesNotExist:
                role = None
            try:
                with transaction.atomic():
                    user = User.objects.create(
                        first_name=(first_name or "").strip(),
                        last_name=(last_name or "").strip(),
                        username=lookup_email,
                        email=lookup_email,
                        role=role,
                        is_active=True,
                    )
                    user.set_unusable_password()
                    user.save()
                created = True
            except IntegrityError:
                # Two first-time check-ins for the same phone raced; the other
                # won. Reuse the row it created (savepoint rollback keeps the
                # outer transaction usable).
                user = User.objects.filter(email__iexact=lookup_email).first()
                if user is None:
                    raise
        else:
            # Keep the name fresh if they typed a fuller one this time.
            dirty = []
            if first_name and not (user.first_name or "").strip():
                user.first_name = first_name.strip()
                dirty.append("first_name")
            if last_name and not (user.last_name or "").strip():
                user.last_name = last_name.strip()
                dirty.append("last_name")
            if dirty:
                user.save(update_fields=dirty)

        ambassador = Ambassador.objects.filter(user=user).first()
        if ambassador is None:
            ambassador = Ambassador.objects.create(
                user=user,
                phone=(phone or "").strip() or None,
                is_active=False,  # pending admin confirmation, like a walk-up
                coordinates=[],
                created_by=user,
                updated_by=user,
            )
        elif phone and not (getattr(ambassador, "phone", None) or "").strip():
            ambassador.phone = phone.strip()
            ambassador.save(update_fields=["phone"])

    return ambassador, created


# --------------------------------------------------------------------------
# Booking + attendance
# --------------------------------------------------------------------------
def ensure_walkup_booking(event, ambassador, actor):
    """Get-or-create this BA's ``source=walkup`` booking for the event.

    Always PENDING (``is_approved=False``). Unlike the in-app walk-up — where an
    already-active BA is auto-approved because they authenticated as themselves —
    a public web check-in has no authenticated identity (the account is an
    isolated phone-keyed stub, see ``get_or_create_checkin_ambassador``), so
    every web check-in must be confirmed by an admin in the Walk-ups queue before
    its hours/recap count. This keeps the code's "possession starts a *pending*
    check-in" guarantee intact even if the stub namespace ever resolved to an
    active account."""
    from ambassadors.models import AmbassadorEvent

    amb_event, created = AmbassadorEvent.objects.get_or_create(
        ambassador=ambassador,
        event=event,
        defaults=dict(
            tenant=event.tenant,
            is_approved=False,
            source=AmbassadorEvent.SOURCE_WALKUP,
            created_by=actor,
            updated_by=actor,
        ),
    )
    return amb_event, created


def _ensure_source(name: str):
    from ambassadors.models import Source

    source, _ = Source.objects.get_or_create(name=name)
    return source


def record_attendance(*, amb_event, kind: str, coordinates, actor):
    """Insert one clock ``Attendance`` row (kind = ``"clock_in"``/``"clock_out"``).
    Mirrors ``ambassadors/mutations._record_attendance``."""
    from ambassadors.models import Attendance

    return Attendance.objects.create(
        clock_time=dj_tz.now(),
        coordinates=coordinates,
        ambassador=amb_event.ambassador,
        job=None,
        event=amb_event.event,
        source=_ensure_source(kind),
    )


def walkin_event_name(*, store_name: str, address: str, on_date) -> str:
    """Name a walk-in event "M/D/YYYY - <address>".

    This string is the title on the recap, in pickers and in exports, so it has
    to say WHICH activation at a glance. The old behaviour used whatever the BA
    typed in the optional store-name box, which in practice was the brand —
    every Total Wireless recap came through titled "Total wireless", telling
    you nothing about which one. Date + address is the pair that actually
    identifies a stop, and both are already required at check-in.

    A store name, when given and not just the brand echoed back, is appended in
    parentheses rather than dropped — "8/1/2026 - 123 Main St (Kiosk 4)".

    Note this is display only. Find-or-create keys on
    (tenant, normalized address, date), NOT on the name, so changing the format
    can never fork or merge events.
    """
    addr = (address or "").strip()
    store = (store_name or "").strip()
    try:
        stamp = f"{on_date.month}/{on_date.day}/{on_date.year}"
    except AttributeError:
        stamp = ""

    base = " - ".join(p for p in (stamp, addr) if p) or store or "Walk-in event"
    # Skip a store name that adds nothing: blank, or already inside the address.
    if store and normalize_place(store) not in normalize_place(base):
        base = f"{base} ({store})"
    return base[:255]


def record_location_ping(*, ambassador, event, coordinates, source: str):
    """Write one ``LocationPing`` for a web check-in, best-effort.

    Clock coordinates were already stored on ``Attendance.coordinates``, but
    the admin surfaces that actually PLOT a BA — the "Today, on the ground"
    map and the per-event GPS trail — read ``LocationPing``. Web check-ins
    were therefore invisible on both while the mobile app showed up fine.
    This closes that gap: browser BAs land on the same map with no new admin
    UI.

    Returns the ping or ``None``. NEVER raises — a BA has to be able to clock
    in from a stockroom with one bar even if we can't record where they are.
    """
    from ambassadors.models import LocationPing

    if not coordinates or len(coordinates) < 2:
        return None
    try:
        lat = float(coordinates[0])
        lng = float(coordinates[1])
    except (TypeError, ValueError):
        return None
    # Null island means "no fix", not "off the coast of Ghana".
    if lat == 0.0 and lng == 0.0:
        return None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
        return None

    valid = {c[0] for c in LocationPing.SOURCE_CHOICES}
    try:
        return LocationPing.objects.create(
            ambassador=ambassador,
            event=event,
            lat=lat,
            lng=lng,
            recorded_at=dj_tz.now(),
            source=source if source in valid else "foreground",
        )
    except Exception:  # noqa: BLE001 — presence data is never worth a 500
        logger.exception(
            "location ping failed ambassador=%s event=%s",
            getattr(ambassador, "id", None),
            getattr(event, "id", None),
        )
        return None


def clock_state(*, ambassador_id: int, event_id: int) -> dict:
    """Current clock state for (BA, event): ``state`` is one of ``not_started``
    / ``clocked_in`` / ``clocked_out`` (the latest punch wins), plus first-in /
    last-out timestamps."""
    from ambassadors.models import Attendance

    atts = list(
        Attendance.objects.filter(ambassador_id=ambassador_id, event_id=event_id)
        .select_related("source")
        .order_by("clock_time")
    )
    first_in = next(
        (a for a in atts if getattr(a.source, "name", "") == "clock_in"), None
    )
    last_out = next(
        (a for a in reversed(atts) if getattr(a.source, "name", "") == "clock_out"),
        None,
    )
    latest = atts[-1] if atts else None
    latest_kind = getattr(latest.source, "name", "") if latest else ""
    if latest_kind == "clock_in":
        state = "clocked_in"
    elif latest_kind == "clock_out":
        state = "clocked_out"
    else:
        state = "not_started"
    return {
        "state": state,
        "clockInAt": first_in.clock_time.isoformat() if first_in else None,
        "clockOutAt": last_out.clock_time.isoformat() if last_out else None,
    }


def has_recap(*, ambassador_id: int, event_id: int) -> bool:
    from recaps.models import CustomRecap, Recap

    return (
        CustomRecap.objects.filter(
            event_id=event_id, ambassador_id=ambassador_id
        ).exists()
        or Recap.objects.filter(
            event_id=event_id, ambassador_id=ambassador_id
        ).exists()
    )


# --------------------------------------------------------------------------
# Admin alert — "a web check-in just landed"
# --------------------------------------------------------------------------
def notify_checkin_landed_if_first(event, ambassador) -> None:
    """Email the Ignite admins the FIRST time a web check-in BA clocks in for an
    event, so a pending walk-up never sits unseen in the queue. Fires once per
    (BA, event) — gated on it being the first ``clock_in``. Best-effort: email
    is reliable inline (see project_push_email_delivery) and a failure here
    never blocks the clock."""
    from ambassadors.models import Attendance

    try:
        n = Attendance.objects.filter(
            ambassador=ambassador, event=event, source__name="clock_in"
        ).count()
        if n != 1:
            return
        _email_admins_checkin_landed(event, ambassador)
    except Exception:  # noqa: BLE001
        logger.exception(
            "checkin: landed-alert failed event=%s", getattr(event, "id", None)
        )


def _email_admins_checkin_landed(event, ambassador) -> None:
    from django.conf import settings
    from django.utils.html import escape

    from events.mutations import _get_spark_admin_emails
    from utils.mailer import Envelope, Mailer

    admins = _get_spark_admin_emails()
    if not admins:
        return
    user = getattr(ambassador, "user", None)
    name = ""
    if user:
        name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    name = name or "A field rep"
    phone = getattr(ambassador, "phone", None) or ""
    brand = event.tenant.name if getattr(event, "tenant_id", None) else ""
    venue = event.name or "an event"
    base = (getattr(settings, "ADMIN_FRONTEND_URL", "") or "").rstrip("/")
    link_html = (
        f"<div style='margin:16px 0 4px'><a href='{base}/walkups' "
        "style='display:inline-block;background:#c5f546;color:#0a0d09;"
        "padding:10px 18px;border-radius:10px;text-decoration:none;"
        "font-weight:700'>Review in Walk-ups</a></div>"
        if base and base != "http://localhost:3000"
        else ""
    )
    phone_html = (
        f"<p style='color:#555;margin:4px 0 0'>Phone: {escape(phone)}</p>"
        if phone
        else ""
    )
    html = (
        "<div style='font-family:system-ui,sans-serif;color:#14181a'>"
        f"<p style='font-size:15px;margin:0'><strong>{escape(name)}</strong> just "
        f"checked in via the web link for <strong>{escape(venue)}</strong>"
        f"{(' — ' + escape(brand)) if brand else ''}.</p>"
        f"{phone_html}"
        "<p style='color:#555'>They're clocked in and can file a recap. Confirm "
        "the walk-up so their hours count.</p>"
        f"{link_html}</div>"
    )

    class _CheckinMailer(Mailer):
        def envelope(self) -> "Envelope":
            return Envelope(
                subject=f"New web check-in — {name} @ {venue}",
                html=html,
                to_emails=admins,
            )

    _CheckinMailer().send_now()


# --------------------------------------------------------------------------
# Recap submission
# --------------------------------------------------------------------------
def submit_checkin_recap(
    *,
    event,
    ambassador,
    template,
    field_values: list[dict],
    files: list[dict],
    total_engagements: int | None,
    product_samples: list[dict] | None = None,
):
    """Create a ``CustomRecap`` (+ field values, photos, product samples) for a
    walk-up BA, attributed to their own user. Replicates the write path in
    ``recaps/mutations.create_custom_recap`` (retailer/location/state/timezone
    derived from the event) so the recap is indistinguishable from an app-filed
    one, then runs the data-quality guard + admin notification off-thread.
    Returns the created recap."""
    from recaps import heic_conversion
    from recaps import models as rmodels
    from recaps.mutations import _resolve_file_recap_category
    from utils.gcs import extract_blob_name_from_url

    actor = ambassador.user
    name = (event.name or "Recap").strip() or "Recap"

    retailer = getattr(event, "retailer", None)
    location = getattr(event, "location", None) or (
        getattr(retailer, "location", None) if retailer else None
    )
    state = getattr(event, "state", None) or (
        getattr(location, "state", None) if location else None
    )
    timezone = getattr(event, "timezone", None)

    # Security scoping for caller-supplied ids:
    #  - photos must live under THIS session's own check-in prefix (never an
    #    arbitrary/foreign bucket path);
    #  - product samples must reference one of the event's own SKUs.
    expected_blob_prefix = f"recap_files/checkin/{event.uuid}/"
    allowed_product_ids = {str(p["id"]) for p in _event_products(event)}

    with transaction.atomic():
        # Idempotent: a returning/edited check-in updates its existing recap for
        # this (event, BA) rather than filing a duplicate that would inflate KPIs
        # (the page offers "Edit recap"; a flaky-network double-submit hits this
        # too). Field values + product samples are replaced; photos are additive.
        recap = (
            rmodels.CustomRecap.objects.filter(event=event, ambassador=ambassador)
            .order_by("-id")
            .first()
        )
        if recap is None:
            recap = rmodels.CustomRecap.objects.create(
                name=name,
                submitted_at=dj_tz.now(),
                event=event,
                timezone=timezone,
                total_engagements=total_engagements,
                job=None,
                retailer=retailer,
                ambassador=ambassador,
                location=location,
                state=state,
                tenant_id=event.tenant_id,
                custom_recap_template=template,
                created_by=actor,
            )
        else:
            recap.submitted_at = dj_tz.now()
            recap.total_engagements = total_engagements
            recap.custom_recap_template = template
            recap.updated_by = actor
            recap.save(
                update_fields=[
                    "submitted_at",
                    "total_engagements",
                    "custom_recap_template",
                    "updated_by",
                    "updated_at",
                ]
            )
            rmodels.CustomFieldValue.objects.filter(custom_recap=recap).delete()
            rmodels.CustomRecapProductSample.objects.filter(
                custom_recap=recap
            ).delete()

        for fv in field_values or []:
            raw_id = fv.get("customFieldId") or fv.get("custom_field_id")
            try:
                field_id = int(str(raw_id))
            except (TypeError, ValueError):
                continue
            custom_field = rmodels.CustomField.objects.filter(
                id=field_id, custom_recap_template_id=template.id
            ).first()
            if not custom_field:
                continue
            value = fv.get("value")
            if value is None:
                continue
            rmodels.CustomFieldValue.objects.create(
                custom_recap=recap,
                custom_field=custom_field,
                value=str(value),
                created_by=actor,
            )

        for sample in product_samples or []:
            raw_pid = sample.get("productId") or sample.get("product_id")
            qty = sample.get("quantity")
            try:
                product_id = int(str(raw_pid))
                qty_int = int(qty)
            except (TypeError, ValueError):
                continue
            if qty_int <= 0:
                continue
            # Never reference a product outside this event's own SKU list — the
            # FE only offers these; a forged id would pull in another tenant's
            # product. (Empty allow-set ⇒ event has no products ⇒ skip all.)
            if str(product_id) not in allowed_product_ids:
                logger.warning(
                    "checkin recap: rejected out-of-scope product %s", product_id
                )
                continue
            rmodels.CustomRecapProductSample.objects.create(
                custom_recap=recap,
                created_by=actor,
                product_id=product_id,
                quantity=qty_int,
            )

        existing_blobs = set(
            rmodels.CustomRecapFile.objects.filter(custom_recap=recap).values_list(
                "url", flat=True
            )
        )
        default_file_type = None
        for file_input in files or []:
            raw = file_input.get("blobName") or file_input.get("blob_name") or file_input.get("file")
            blob_name = extract_blob_name_from_url(raw)
            if not blob_name:
                continue
            # Only accept a blob this session actually uploaded (its own
            # check-in prefix) — reject any arbitrary/foreign bucket path a
            # forged request might supply, and skip re-submitted duplicates.
            if not blob_name.startswith(expected_blob_prefix):
                logger.warning(
                    "checkin recap: rejected out-of-scope blob %s", blob_name
                )
                continue
            if blob_name in existing_blobs:
                continue
            existing_blobs.add(blob_name)
            if default_file_type is None:
                default_file_type = rmodels.FileType.objects.first()
            if default_file_type is None:
                # No file types configured at all — skip photos rather than 500.
                logger.warning("checkin recap: no FileType available; skipping photo")
                break
            # Every check-in upload is a photo (the upload-URL endpoint only
            # signs image content types), so file it under the tenant's "photos"
            # category via the positional sentinel "1" — same bucket the app/web
            # recap forms use, so the gallery groups them correctly.
            file_recap_category = _resolve_file_recap_category(
                "1", tenant_id=getattr(event, "tenant_id", None)
            )
            rmodels.CustomRecapFile.objects.create(
                name=f"Web check-in photo for {name}",
                url=blob_name,
                file_type=default_file_type,
                file_recap_category=file_recap_category,
                custom_recap=recap,
                approved=False,
                created_by=actor,
            )
            if heic_conversion.is_heic_blob(blob_name):
                try:
                    heic_conversion.ensure_jpg_sibling_blob(blob_name)
                except Exception:  # noqa: BLE001 — display convenience only
                    logger.exception("checkin recap: HEIC sibling failed %s", blob_name)

    _finalize_recap_offthread(recap.id)
    return recap


def _finalize_recap_offthread(recap_id: int) -> None:
    """Run the async data-quality guard + admin "recap ready" notification on a
    fresh thread (its own loop, no asgiref thread-local) so a nested
    thread-sensitive DB write can't deadlock the calling ASGI worker. Both are
    best-effort — a failure here never fails the submitted recap."""

    async def _run():
        from asgiref.sync import sync_to_async

        from recaps import models as rmodels
        from recaps.mutations import (
            _guard_recap_data_quality,
            _notify_recap_ready_for_review_to_admins,
        )

        recap = await sync_to_async(
            rmodels.CustomRecap.objects.select_related("created_by", "event", "tenant").get
        )(id=recap_id)
        created_by = await sync_to_async(lambda: recap.created_by)()
        try:
            await _guard_recap_data_quality(recap)
        except Exception:  # noqa: BLE001
            logger.exception("checkin recap: data-quality guard failed id=%s", recap_id)
        try:
            await _notify_recap_ready_for_review_to_admins(recap, created_by)
        except Exception:  # noqa: BLE001
            logger.exception("checkin recap: notify-admins failed id=%s", recap_id)
        # Field-ops crew for the check-in link specifically — nobody is
        # watching a queue for these, so the submission has to reach a person.
        try:
            await sync_to_async(notify_checkin_recap_submitted)(recap)
        except Exception:  # noqa: BLE001
            logger.exception("checkin recap: crew notify failed id=%s", recap_id)

    def _worker():
        asyncio.run(_run())

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            ex.submit(_worker).result(timeout=90)
    except Exception:  # noqa: BLE001 — never fail the recap on finalize trouble
        logger.exception("checkin recap: finalize thread failed id=%s", recap_id)


# --------------------------------------------------------------------------
# Public context payload
# --------------------------------------------------------------------------
def _brand_primary_color(tenant) -> str | None:
    try:
        theme = tenant.themes.first()
        cssv = getattr(theme, "css_variables", None) or {}
        if isinstance(cssv, dict):
            return cssv.get("--p") or cssv.get("primary") or cssv.get("--color-primary")
    except Exception:  # noqa: BLE001
        return None
    return None


def build_public_context(event, ambassador=None) -> dict:
    """The JSON the public page renders: event + brand + template, and — when a
    session already exists (ambassador given) — that BA's current clock/recap
    state so a returning link resumes where they left off."""
    tenant = getattr(event, "tenant", None)
    # Keep the SCHEDULED START and the CALENDAR DATE apart. They used to be
    # coalesced into one `startTime`, which was fine for a pre-booked event but
    # wrong for a walk-in: those have no start_time, so the page fell back to
    # `date` — stored at noon UTC — and rendered it as a clock time. A BA in
    # Nevada checking in for Aug 1 saw "Sat, Aug 1 · 5 AM" for a shift that has
    # no start time at all. Sending them separately lets the page show a time
    # only when there genuinely is one.
    start = getattr(event, "start_time", None)
    end = getattr(event, "end_time", None)
    day = getattr(event, "date", None)
    payload = {
        "event": {
            "uuid": str(event.uuid),
            "name": event.name,
            "address": getattr(event, "address", None),
            "startTime": start.isoformat() if start else None,
            "endTime": end.isoformat() if end else None,
            "date": day.isoformat() if day else None,
        },
        "brand": {
            "name": tenant.name if tenant else "",
            "primaryColor": _brand_primary_color(tenant) if tenant else None,
        },
        "template": serialize_template(event),
    }
    if ambassador is not None:
        payload["session"] = {
            "ambassadorName": (
                f"{ambassador.user.first_name or ''}".strip() or "You"
                if getattr(ambassador, "user", None)
                else "You"
            ),
            "clock": clock_state(ambassador_id=ambassador.id, event_id=event.id),
            "hasRecap": has_recap(ambassador_id=ambassador.id, event_id=event.id),
            "pendingReview": not bool(getattr(ambassador, "is_active", False)),
        }
    return payload


# ---------------------------------------------------------------------------
# Tenant-wide standing check-in
# ---------------------------------------------------------------------------
#
# The per-event link above needs an activation to exist first. A tenant's
# `checkin_code` is the standing twin: ONE durable link, pinned on the client's
# page, that any BA can open. They supply the store + date and Spark
# finds-or-creates the event, so nobody has to pre-build the activation.
#
# The find-or-create key is (tenant, normalized address, calendar date). That is
# deliberately the "same place, same day" identity rather than one-event-per-BA,
# because several BAs commonly work a single location together: the first to
# check in creates the event, everyone after joins it, and each gets their own
# booking, their own hours and their own recap on that shared event.


def resolve_checkin_target(code: str):
    """Resolve a check-in code to what it points at.

    Returns ``("event", event)``, ``("tenant", tenant)`` or ``(None, None)``.
    Event codes are tried FIRST so every link already in circulation keeps its
    exact current behaviour; the tenant code is a fallback, never an override.
    """
    event = resolve_event_by_code(code)
    if event is not None:
        return "event", event

    from tenants.models import Tenant

    clean = (code or "").strip()
    if not clean:
        return None, None
    tenant = Tenant.objects.filter(checkin_code__iexact=clean).first()
    if tenant is not None:
        return "tenant", tenant
    return None, None


def normalize_place(value: str) -> str:
    """The 'is this the same store?' key: case-, punctuation- and
    spacing-insensitive, so `1155 E. State St.` and `1155 e state st` are one
    place and two BAs there don't fork into two events."""
    v = (value or "").strip().lower()
    v = re.sub(r"[^\w\s]", " ", v)
    return re.sub(r"\s+", " ", v).strip()


def recent_checkin_locations(tenant, limit: int = 30) -> list:
    """Distinct recent store names + addresses for this tenant, newest first.

    Feeds the autocomplete on the store step. Its real job is data hygiene: a BA
    picking a known store re-uses its exact spelling, so the normalized key
    matches and they join the existing event instead of creating a near-dupe.
    """
    from events.models import Event

    seen: set = set()
    out: list = []
    rows = (
        Event.objects.filter(tenant=tenant)
        .exclude(address="")
        .order_by("-id")
        .values("name", "address")[: limit * 4]
    )
    for row in rows:
        key = normalize_place(row.get("address"))
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({"name": row.get("name") or "", "address": row.get("address") or ""})
        if len(out) >= limit:
            break
    return out


def _event_date_utc(on_date):
    """A calendar date stored as NOON UTC.

    Midnight would be the obvious choice and is wrong here: read back in any US
    zone (UTC-4 … UTC-10) midnight UTC lands on the PREVIOUS evening, so the
    event would report a day early everywhere the work actually happened. Noon
    UTC reads as 2am–8am on the intended date across every US zone.
    """
    from datetime import datetime, time, timezone as _tz

    return datetime.combine(on_date, time(12, 0), tzinfo=_tz.utc)


def _default_event_type(tenant):
    """The tenant's standard sampling event type, if it has one."""
    from events.models import EventType

    try:
        return (
            EventType.objects.filter(tenant=tenant)
            .order_by("id")
            .first()
        )
    except Exception:  # noqa: BLE001 — event type is optional on Event
        return None


def find_or_create_walkin_event(
    *, tenant, store_name: str, address: str, on_date, actor
):
    """The event for (tenant, address, date) — found if it exists, else created.

    Returns ``(event, created)``. Wrapped in a transaction with a re-check so two
    BAs tapping "start" at the same second still land on one event.

    ``actor`` is REQUIRED and must be a real user: ``Event.created_by`` is NOT
    NULL, and there is no system-user fallback in app code, so a default of
    ``None`` here would raise IntegrityError on the very first check-in. The
    caller identifies the BA first and passes their user, which also gives the
    Walk-ups queue honest attribution for who opened the event.
    """
    if actor is None:
        raise ValueError("find_or_create_walkin_event requires an actor.")
    from django.db import transaction

    from events.models import Event

    key = normalize_place(address)
    if not key:
        raise ValueError("A store address is required to start a check-in.")

    day_start = _event_date_utc(on_date)
    lo = day_start.replace(hour=0, minute=0)
    hi = day_start.replace(hour=23, minute=59)

    def _match():
        # Small set (one tenant, one day), so normalize in Python rather than
        # trying to express the same collapsing in SQL.
        for ev in Event.objects.filter(tenant=tenant, date__gte=lo, date__lte=hi):
            if normalize_place(getattr(ev, "address", "")) == key:
                return ev
        return None

    with transaction.atomic():
        existing = _match()
        if existing is not None:
            return existing, False

        name = walkin_event_name(
            store_name=store_name, address=address, on_date=on_date
        )
        event = Event.objects.create(
            tenant=tenant,
            name=name[:255],
            address=(address or "").strip(),
            date=day_start,
            event_type=_default_event_type(tenant),
            created_by=actor,
            updated_by=actor,
        )

    # Stamp the state from the address so the row shows a Market in the tracker
    # and counts in the geo breakdown. Best-effort: a geo miss must never block
    # a BA from checking in.
    try:
        from events.routing import extract_state_code
        from tenants.models import State

        code = extract_state_code(event.address or "")
        if code:
            st = State.objects.filter(code__iexact=code).first()
            if st is not None:
                event.state = st
                event.save(update_fields=["state"])
    except Exception:  # noqa: BLE001
        logger.warning("checkin: state stamp failed for event=%s", event.id)

    return event, True


def build_tenant_context(tenant) -> dict:
    """Payload for a standing tenant link before any event exists.

    ``needsEventDetails`` tells the page to ask for store + date first; the rest
    of the flow is identical to the per-event link once identify resolves one.
    """
    return {
        "mode": "tenant",
        "needsEventDetails": True,
        "brand": {
            "name": getattr(tenant, "name", "") or "",
            "primaryColor": _brand_primary_color(tenant),
        },
        "recentLocations": recent_checkin_locations(tenant),
    }


def notify_checkin_recap_submitted(recap) -> None:
    """Email the field-ops crew the moment a recap lands from the check-in link.

    Separate from the existing "ready for review" alert, which goes to
    RECAP_REVIEW_COPY_EMAILS — a different list for a different job. This one
    is scoped to the standing/web check-in flow, where nobody is watching a
    queue and the whole point is that the submission reaches a person.

    Best-effort: runs on the finalize thread and never fails the recap.
    """
    from django.conf import settings
    from django.utils.html import escape

    from utils.mailer import Envelope, Mailer

    to = [e.strip() for e in getattr(settings, "CHECKIN_NOTIFY_EMAILS", []) if (e or "").strip()]
    if not to:
        return

    event = getattr(recap, "event", None)
    tenant = getattr(recap, "tenant", None) or getattr(event, "tenant", None)
    brand = getattr(tenant, "name", "") or ""
    where = getattr(event, "name", "") or "an event"
    address = getattr(event, "address", "") or ""

    amb = getattr(recap, "ambassador", None)
    user = getattr(amb, "user", None) or getattr(recap, "created_by", None)
    who = ""
    if user:
        who = f"{getattr(user, 'first_name', '') or ''} {getattr(user, 'last_name', '') or ''}".strip()
    who = who or "A field rep"
    phone = getattr(amb, "phone", None) or ""

    base = (getattr(settings, "ADMIN_FRONTEND_URL", "") or "").rstrip("/")
    link = (
        f"<div style='margin:16px 0 4px'><a href='{base}/recaps' "
        "style='display:inline-block;background:#c5f546;color:#0a0d09;"
        "padding:10px 18px;border-radius:10px;text-decoration:none;"
        "font-weight:700'>Open recaps</a></div>"
        if base and base != "http://localhost:3000"
        else ""
    )
    rows = "".join(
        f"<p style='color:#555;margin:2px 0'>{escape(label)}: {escape(str(value))}</p>"
        for label, value in (
            ("Brand", brand),
            ("Where", address or where),
            ("Phone", phone),
        )
        if value
    )
    html = (
        "<div style='font-family:system-ui,sans-serif;color:#14181a'>"
        f"<p style='font-size:15px;margin:0'><strong>{escape(who)}</strong> just "
        f"submitted a recap for <strong>{escape(where)}</strong>.</p>"
        f"{rows}"
        "<p style='color:#555;margin-top:12px'>Approving it logs their hours.</p>"
        f"{link}</div>"
    )

    class _RecapSubmittedMailer(Mailer):
        def envelope(self) -> "Envelope":
            return Envelope(
                subject=f"Recap submitted — {who} @ {where}",
                html=html,
                to_emails=to,
            )

    _RecapSubmittedMailer().send_now()


# Longest a shift can stay open and still be resumable. Beyond this an open
# punch is almost certainly a missed clock-out from a previous day, and
# resuming it would attach today's work to yesterday's event.
OPEN_SHIFT_RESUME_HOURS = 18


def open_shift_event_for(*, ambassador, tenant):
    """The event this BA is currently CLOCKED IN on for ``tenant``, or None.

    Reported from the field: a BA clocked in at 3:55, lost her session (a
    cleared browser, a different tab, a tapped "start over"), re-identified,
    and the standing link put her on a NEW event because she typed the store
    address slightly differently the second time. Her words: "it's not letting
    me go to where I clocked in before." She was stuck on the clock with no
    way out, and her hours were sitting on an event she couldn't reach.

    Find-or-create keys on the address the BA types, which is the right key for
    "which activation is this" but the WRONG one for "where am I already
    working". Someone with an open punch is, by definition, at the place they
    clocked in — so an open shift wins over anything they type.

    Newest first, and only within OPEN_SHIFT_RESUME_HOURS so a stale missed
    clock-out from days ago can't hijack today's check-in.
    """
    from ambassadors.models import Attendance
    from events.models import Event

    if ambassador is None or tenant is None:
        return None
    try:
        cutoff = dj_tz.now() - timedelta(hours=OPEN_SHIFT_RESUME_HOURS)
        recent_ins = (
            Attendance.objects.filter(
                ambassador=ambassador,
                event__tenant=tenant,
                source__name="clock_in",
                clock_time__gte=cutoff,
            )
            .order_by("-clock_time")
            .values_list("event_id", "clock_time")[:20]
        )
        for event_id, punched_in in recent_ins:
            closed = Attendance.objects.filter(
                ambassador=ambassador,
                event_id=event_id,
                source__name="clock_out",
                clock_time__gte=punched_in,
            ).exists()
            if not closed:
                return (
                    Event.objects.select_related(
                        "tenant", "request", "retailer", "location", "state", "timezone"
                    )
                    .filter(id=event_id)
                    .first()
                )
    except Exception:  # noqa: BLE001 — never block a check-in over this
        logger.exception(
            "open-shift lookup failed ambassador=%s tenant=%s",
            getattr(ambassador, "id", None), getattr(tenant, "id", None),
        )
    return None
