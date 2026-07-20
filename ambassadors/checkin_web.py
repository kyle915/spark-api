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
    start = getattr(event, "start_time", None) or getattr(event, "date", None)
    end = getattr(event, "end_time", None)
    payload = {
        "event": {
            "uuid": str(event.uuid),
            "name": event.name,
            "address": getattr(event, "address", None),
            "startTime": start.isoformat() if start else None,
            "endTime": end.isoformat() if end else None,
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
