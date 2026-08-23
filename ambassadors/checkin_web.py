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
import hashlib
import logging
import re
import secrets

from datetime import date as date_cls, datetime, timedelta, timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone as dj_tz
from django.utils.dateparse import parse_datetime

from tenants.models import normalize_checkin_resources

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
        Event.objects.select_related("tenant", "request", "retailer", "location", "state", "timezone", "event_type")
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


def photo_bucket_specs(tenant, event_type) -> list:
    """The configured bucket list for one PROGRAM, straight off the tenant.

    ``Tenant.checkin_photo_buckets`` holds either a flat list — one set of
    buckets for the whole brand — or a mapping keyed by event type name, for a
    brand whose programs want different shots. Liquid Death is the second kind:
    a retail demo has a table and a shelf to photograph, an activation has
    neither and does have parking to expense, so a single per-brand list can
    only ever be right for one of them.

    An unknown program falls back to an explicit ``"default"`` list if the brand
    set one, otherwise to nothing. Nothing is the right answer over "some other
    program's list" — a retail BA offered an "Expense Receipts (Parking)"
    dropzone is worse off than one offered the plain grid.
    """
    configured = getattr(tenant, "checkin_photo_buckets", None)
    if isinstance(configured, list):
        return configured
    if not isinstance(configured, dict):
        return []
    wanted = _normalize_category_name(getattr(event_type, "name", "") or "")
    if wanted:
        for key, value in configured.items():
            if _normalize_category_name(str(key)) == wanted and isinstance(value, list):
                return value
    shared = configured.get("default")
    return shared if isinstance(shared, list) else []


def serialize_photo_buckets(event) -> list[dict]:
    """The labelled photo buckets for THIS event's program, in order.

    One entry per dropzone the page should render: ``{id, name, helper, min}``,
    where ``id`` is the tenant's OWN ``FileRecapCategory`` PK — the value the
    page sends back per file so each photo is filed under the bucket the BA put
    it in, instead of everything landing in one category.

    WHICH buckets comes from the event's own event type (see
    ``photo_bucket_specs``), because that is what the BA chose on the standing
    link and what decides their recap form. The category ROWS stay tenant-wide:
    a recap belongs to one event and therefore one program, so two programs
    sharing a "Consumer Sampling Pictures" row is never ambiguous in the PDF,
    and splitting it per program would fragment the brand's photo history for
    no reader's benefit.

    Empty for every tenant that hasn't opted in (``Tenant.checkin_photo_buckets``
    unset), which is what keeps the page on its single generic grid and the
    submit path on the "photos" sentinel. A configured bucket whose category
    row is missing is SKIPPED rather than invented: a bucket the submit path
    would refuse to accept must not be offered, or the BA fills a dropzone
    whose photos quietly fall back to the generic pile.
    """
    tenant = getattr(event, "tenant", None)
    configured = photo_bucket_specs(tenant, getattr(event, "event_type", None))
    if not configured:
        return []

    from recaps.models import FileRecapCategory

    # One query, then match by name — case/spacing-insensitive so a category
    # renamed "Table setup" -> "Table Set Up" (or back) keeps its bucket.
    by_name: dict[str, object] = {}
    for cat in FileRecapCategory.objects.filter(tenant_id=tenant.id).order_by("id"):
        by_name.setdefault(_normalize_category_name(cat.name), cat)

    buckets: list[dict] = []
    for entry in configured:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        cat = by_name.get(_normalize_category_name(name))
        if cat is None:
            logger.warning(
                "checkin photo buckets: tenant %s has no category %r; skipping",
                getattr(tenant, "slug", None),
                name,
            )
            continue
        try:
            minimum = int(entry.get("min") or 0)
        except (TypeError, ValueError):
            minimum = 0
        buckets.append(
            {
                "id": str(cat.id),
                "name": name,
                "helper": str(entry.get("helper") or "").strip(),
                "min": max(0, minimum),
            }
        )
    return buckets


def _normalize_category_name(name: str | None) -> str:
    """Fold a category label to a comparison key: case and non-alphanumerics
    dropped, so "Table Set Up", "Table setup" and "table-setup" are one bucket."""
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


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


def find_checkin_ambassador(*, phone: str):
    """The existing walk-up stub for this phone, or None.

    Lookup only — the unfiled-recaps list must not mint a stub just because
    someone opened the standing link. Identity is the same phone-derived
    ``@walkup.spark`` email ``get_or_create_checkin_ambassador`` uses.
    """
    from ambassadors.models import Ambassador

    phone_digits = _normalize_phone(phone)
    if not phone_digits:
        return None
    lookup_email = _synth_email(phone_digits)
    user = User.objects.filter(email__iexact=lookup_email).first()
    if user is None:
        return None
    return Ambassador.objects.filter(user=user).first()


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


# A BA who tapped Clock in at 3pm with no bars will flush at 5pm. Accept the
# client timestamp so payroll matches the tap, but refuse a future stamp or
# one older than a day (that's not "I lost service", that's a rewritten shift).
CLOCKED_IN_MAX_AGE = timedelta(hours=24)
CLOCKED_IN_FUTURE_SKEW = timedelta(minutes=2)


class ClientClockTimeError(ValueError):
    """``clockedInAt`` was present but not usable. ``reason`` is the JSON error code."""

    def __init__(self, reason: str, message: str):
        self.reason = reason
        super().__init__(message)


def parse_client_clock_time(raw, *, now=None):
    """Parse optional client ``clockedInAt`` (ISO).

    ``None`` / blank → ``None`` (caller uses server now). Invalid / future /
    older than 24h → ``ClientClockTimeError``.
    """
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        return None
    parsed = parse_datetime(raw.strip())
    if parsed is None:
        # JS ``toISOString()`` is fine; some phones send ``+00:00`` already.
        try:
            parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ClientClockTimeError(
                "invalid", "clockedInAt must be an ISO timestamp."
            ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    now = now or dj_tz.now()
    if parsed > now + CLOCKED_IN_FUTURE_SKEW:
        raise ClientClockTimeError(
            "future", "That clock-in time is in the future."
        )
    if parsed < now - CLOCKED_IN_MAX_AGE:
        raise ClientClockTimeError(
            "too_old", "That clock-in time is too old to use."
        )
    return parsed


def record_attendance(*, amb_event, kind: str, coordinates, actor, clock_time=None):
    """Insert one clock ``Attendance`` row (kind = ``"clock_in"``/``"clock_out"``).
    Mirrors ``ambassadors/mutations._record_attendance``.

    ``clock_time`` is the moment the BA meant — tap time on an offline queue
    flush — and falls back to server now when the client didn't send one.
    """
    from ambassadors.models import Attendance

    return Attendance.objects.create(
        clock_time=clock_time or dj_tz.now(),
        coordinates=coordinates,
        ambassador=amb_event.ambassador,
        job=None,
        event=amb_event.event,
        source=_ensure_source(kind),
    )


def walkin_event_name(
    *, store_name: str, address: str, on_date, program: str = ""
) -> str:
    """Name a walk-in event "M/D/YYYY - <address>".

    This string is the title on the recap, in pickers and in exports, so it has
    to say WHICH activation at a glance. The old behaviour used whatever the BA
    typed in the optional store-name box, which in practice was the brand —
    every Total Wireless recap came through titled "Total wireless", telling
    you nothing about which one. Date + address is the pair that actually
    identifies a stop, and both are already required at check-in.

    A store name, when given and not just the brand echoed back, is appended in
    parentheses rather than dropped — "8/1/2026 - 123 Main St (Kiosk 4)".

    ``program`` is the event type's name, passed only for a brand that runs more
    than one off the same link. Since the BA now chooses their program, one
    address on one date can legitimately hold two events; without the program in
    the title they'd be two identically-named rows in the recap list and nobody
    could tell the retail demo from the activation. Appended as
    "· Event Activation".

    Note this is display only. Find-or-create keys on
    (tenant, normalized address, date, event type), NOT on the name, so changing
    the format can never fork or merge events.
    """
    addr = (address or "").strip()
    store = (store_name or "").strip()
    try:
        stamp = f"{on_date.month}/{on_date.day}/{on_date.year}"
    except AttributeError:
        stamp = ""

    base = " - ".join(p for p in (stamp, addr) if p) or store or "Walk-in event"
    # Skip a store name that adds nothing. Two shapes:
    #
    #  * it's already inside the title (blank, or the address echoed back);
    #  * it CONTAINS the address — which means it isn't a store name at all but a
    #    previous event's whole title. `recent_checkin_locations` hands the page
    #    {name: event.name, address: event.address}, and the store autocomplete
    #    copies that `name` into the store-name box; event names are titles
    #    ("8/2/2026 - 1155 E State St"), never store names. That produced
    #    "8/2/2026 - 1155 E State St (8/2/2026 - 1155 E State St · Retail
    #    Sampling)" the moment a second program made the two strings differ.
    #    The page no longer copies such a name, and this refuses it regardless of
    #    caller.
    if (
        store
        and normalize_place(store) not in normalize_place(base)
        and not (addr and normalize_place(addr) in normalize_place(store))
    ):
        base = f"{base} ({store})"
    label = (program or "").strip()
    if label and normalize_place(label) not in normalize_place(base):
        base = f"{base} · {label}"
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


def abandon_open_clock(*, ambassador, event) -> dict:
    """Clock out a leftover open punch without filing or deleting a recap.

    Standing-link localStorage keeps a 90-day session keyed only by the
    walk-up code. Reopening FF-* on Wednesday restores Saturday's event,
    and Clock in punches that leftover day. Closing the punch (not the
    recap) lets identify mint today's market/store event.

    Already-closed shifts are a no-op. Empty clock-out Recap stubs are
    not filed — we never delete a recap here.
    """
    empty = {
        "state": "not_started",
        "clockInAt": None,
        "clockOutAt": None,
    }
    if ambassador is None or event is None:
        return {"cleared": False, "clockedOut": False, "clock": empty}
    state = clock_state(ambassador_id=ambassador.id, event_id=event.id)
    if state.get("state") != "clocked_in":
        return {"cleared": True, "clockedOut": False, "clock": state}

    amb_event, _created = ensure_walkup_booking(
        event, ambassador, actor=ambassador.user
    )
    record_attendance(
        amb_event=amb_event,
        kind="clock_out",
        coordinates=None,
        actor=ambassador.user,
    )
    try:
        from ambassadors.models import MileageSession

        session = active_mileage_session(ambassador=ambassador, event=event)
        if session is not None:
            session.status = MileageSession.STATUS_CANCELED
            session.ended_at = dj_tz.now()
            session.save(update_fields=["status", "ended_at", "updated_at"])
    except Exception:  # noqa: BLE001 — mileage must never block a clear
        logger.exception(
            "clear-clock mileage close failed ambassador=%s event=%s",
            getattr(ambassador, "id", None),
            getattr(event, "id", None),
        )
    state = clock_state(ambassador_id=ambassador.id, event_id=event.id)
    return {"cleared": True, "clockedOut": True, "clock": state}


def has_recap(*, ambassador_id: int, event_id: int) -> bool:
    """True when this BA has *filed* a recap for the event.

    Clock-out inserts an empty Recap/CustomRecap stub. Existence-only
    was wrong — the check-in page said "you're all set" on a blank draft.
    Filed means submitted content (photos, metrics, or submitted_at).
    """
    from recaps.filed import has_filed_recap

    return has_filed_recap(ambassador_id=ambassador_id, event_id=event_id)


def existing_shift_event_for(*, ambassador, tenant, on_date, address: str = ""):
    """Event this BA already clocked on for ``tenant`` on ``on_date``.

    Standing-check-in recaps belong to a real shift. A BA filing Friday's
    recap on Sunday must land on Friday's event — not a new one invented
    from today's form defaults. Prefer an address/market match when they
    typed one; otherwise the newest clock-in that day.
    """
    from django.db.models import Q

    from ambassadors.models import Attendance

    if ambassador is None or tenant is None or on_date is None:
        return None

    day_start = _event_date_utc(on_date)
    lo = day_start.replace(hour=0, minute=0, second=0, microsecond=0)
    hi = day_start.replace(hour=23, minute=59, second=59, microsecond=999999)
    # clock_time is stored in UTC; a US evening punch can sit on either
    # side of the noon-UTC event.date. A 14-hour pad covers every US zone.
    clock_lo = day_start - timedelta(hours=14)
    clock_hi = day_start + timedelta(hours=14)

    rows = (
        Attendance.objects.filter(
            ambassador=ambassador,
            event__tenant=tenant,
            source__name="clock_in",
        )
        .filter(
            Q(event__date__gte=lo, event__date__lte=hi)
            | Q(clock_time__gte=clock_lo, clock_time__lte=clock_hi)
        )
        .select_related(
            "event",
            "event__tenant",
            "event__request",
            "event__retailer",
            "event__location",
            "event__state",
            "event__timezone",
            "event__event_type",
        )
        .order_by("-clock_time")
    )

    events = []
    seen: set[int] = set()
    for att in rows:
        ev = att.event
        if ev is None or ev.id in seen:
            continue
        seen.add(ev.id)
        events.append(ev)
    # clock_time's 14-hour pad can surface a leftover punch on a DIFFERENT
    # calendar day's event (Alicia clocked into Sunday Miami on Wednesday
    # morning via a persisted session). Never attach today's identify to
    # that event — find-or-create should mint the day they picked.
    events = [ev for ev in events if event_calendar_date(ev) == on_date]
    if not events:
        return None

    if address:
        key = normalize_place(address)
        core = address_core_key(address)
        for ev in events:
            ev_addr = ev.address or ""
            if key and normalize_place(ev_addr) == key:
                return ev
            if core and address_core_key(ev_addr) == core:
                return ev
        # Typed a different store on the same day — mint/join that activation,
        # don't hijack the first clock-in (LD: Walmart then 7-Eleven).
        return None
    return events[0]


def unfiled_shifts_for(*, ambassador, tenant, limit: int = 8) -> list[dict]:
    """Recent clocked shifts that still need a filed recap.

    The standing identify step lists these so a BA who forgot Friday can
    tap that shift instead of re-typing today's date and getting stuck.
    """
    from ambassadors.models import Attendance
    from recaps.filed import has_filed_recap

    if ambassador is None or tenant is None:
        return []

    cutoff = dj_tz.now() - timedelta(days=90)
    rows = (
        Attendance.objects.filter(
            ambassador=ambassador,
            event__tenant=tenant,
            source__name="clock_in",
            clock_time__gte=cutoff,
        )
        .select_related("event")
        .order_by("-clock_time")
    )

    out: list[dict] = []
    seen: set[int] = set()
    for att in rows:
        ev = att.event
        if ev is None or ev.id in seen:
            continue
        seen.add(ev.id)
        if has_filed_recap(ambassador_id=ambassador.id, event_id=ev.id):
            continue
        clock = clock_state(ambassador_id=ambassador.id, event_id=ev.id)
        cal = event_calendar_date(ev)
        out.append(
            {
                "eventDate": cal.isoformat() if cal else None,
                "name": ev.name or "",
                "address": ev.address or "",
                "clockInAt": clock.get("clockInAt"),
                "clockOutAt": clock.get("clockOutAt"),
                "clockState": clock.get("state"),
            }
        )
        if len(out) >= limit:
            break
    return out


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

    from utils.mailer import Envelope, Mailer

    # The shared events@ inbox, NOT every Spark admin. This used to fan out via
    # _get_spark_admin_emails(), so one BA clocking in pinged seven people
    # individually — the fastest way to train a team to ignore an alert. Clock
    # traffic rides the HOURS list with the nightly digest; the recap alert is
    # a separate, people-facing list.
    admins = [
        e.strip()
        for e in getattr(settings, "CHECKIN_HOURS_NOTIFY_EMAILS", [])
        if (e or "").strip()
    ]
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
_FEEL_FREE_SLUGS = frozenset(
    {"feel-free", "feelfree", "bl00-feel-free", "bl00feelfree"}
)


def is_feel_free_tenant(tenant) -> bool:
    """True for the Feel Free brand only (standing link FF-YMMK3Q).

    Torch / Liquid Death / KKC / Girl Beer must not match — those stay
    human-reviewed (or, for Torch requests, the separate spark-form
    auto-approve). Name/slug/url-name all accepted because live rows
    have used each.
    """
    if tenant is None:
        return False
    slug = (getattr(tenant, "slug", None) or "").strip().lower()
    name = (getattr(tenant, "name", None) or "").strip().lower()
    url = (getattr(tenant, "request_url_name", None) or "").strip().lower()
    if slug in _FEEL_FREE_SLUGS or slug.endswith("-feel-free"):
        return True
    if url in _FEEL_FREE_SLUGS or url.endswith("-feel-free"):
        return True
    return name == "feel free"


class RecapNeedsAPhoto(ValueError):
    """A submission would leave a recap with no photo on it at all.

    Distinct from the generic failure the view reports as a 500, because this
    one is the BA's to fix and the page can say so. Raised inside the write
    transaction, so a refused submission rolls all of itself back — including a
    refused EDIT, whose field values are deleted and rewritten in that same
    block and must not be left half-replaced.
    """


def submit_checkin_recap(
    *,
    event,
    ambassador,
    template,
    field_values: list[dict],
    files: list[dict],
    total_engagements: int | None,
    product_samples: list[dict] | None = None,
    force_new: bool = False,
    third_party: bool = False,
):
    """Create a ``CustomRecap`` (+ field values, photos, product samples) for a
    walk-up BA, attributed to their own user. Replicates the write path in
    ``recaps/mutations.create_custom_recap`` (retailer/location/state/timezone
    derived from the event) so the recap is indistinguishable from an app-filed
    one, then runs the data-quality guard + admin notification off-thread.
    Returns the created recap."""
    from recaps import heic_conversion
    from recaps import models as rmodels
    from recaps.mutations import (
        _resolve_explicit_file_recap_category,
        _resolve_file_recap_category,
    )
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
    #  - a per-file category must be one of the buckets THIS EVENT'S PROGRAM
    #    actually offers. Anything else (absent, stale, forged) falls back to the
    #    "photos" sentinel below, so a tenant with no buckets configured —
    #    Total Wireless, Feel Free — behaves exactly as it did before buckets
    #    existed, and a forged id can at worst pick a different bucket of the
    #    brand's own rather than reach another tenant's category.
    allowed_category_ids = {b["id"] for b in serialize_photo_buckets(event)}

    with transaction.atomic():
        # Default is idempotent: a returning/edited check-in updates its
        # existing recap for this (event, BA) rather than filing a duplicate
        # (the page offers "Edit recap"; a flaky-network double-submit hits
        # this too). Field values + product samples are replaced; photos are
        # additive.
        #
        # Standing tenant links (Feel Free) are the exception: a BA can work
        # more than one shift in the same market on the same calendar day, and
        # those share ONE event. `force_new` files another recap instead of
        # overwriting the first. An empty clock-out stub is still reused so
        # we don't leave a blank row next to a real one. Per-event codes
        # never pass force_new — Liquid Death activations stay one-per-event.
        recap = (
            rmodels.CustomRecap.objects.filter(event=event, ambassador=ambassador)
            .order_by("-id")
            .first()
        )
        if recap is not None and force_new:
            from recaps.filed import custom_filed_q

            if (
                rmodels.CustomRecap.objects.filter(id=recap.id)
                .filter(custom_filed_q())
                .exists()
            ):
                recap = None
        typed_name = _store_display_name(
            getattr(event, "name", "") or "", getattr(event, "address", "") or ""
        )
        typed_addr = (getattr(event, "address", "") or "").strip()
        auto_approve = is_feel_free_tenant(getattr(event, "tenant", None))
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
                is_third_party=third_party,
                typed_store_name=typed_name[:255] if third_party else "",
                typed_store_address=typed_addr if third_party else "",
                store_mapping_status="unmatched" if third_party else "",
                store_suggestions=(
                    suggest_store_matches(
                        getattr(event, "tenant", None), typed_name, typed_addr
                    )
                    if third_party
                    else []
                ),
                approved=auto_approve,
            )
        else:
            recap.submitted_at = dj_tz.now()
            recap.total_engagements = total_engagements
            recap.custom_recap_template = template
            recap.updated_by = actor
            if third_party:
                recap.is_third_party = True
            if auto_approve:
                recap.approved = True
            recap.save(
                update_fields=[
                    "submitted_at",
                    "total_engagements",
                    "custom_recap_template",
                    "updated_by",
                    "updated_at",
                    *(["is_third_party"] if third_party else []),
                    *(["approved"] if auto_approve else []),
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

        # Torch (and most retail templates) already ask "total number of
        # consumers sampled". That IS Spark's engagements metric — copy it
        # onto CustomRecap.total_engagements so the BA isn't asked twice
        # and the recap PDF / KPI still get a number.
        from recaps.types import _consumers_sampled_from_fields

        sampled = _consumers_sampled_from_fields(
            [
                (cfv.custom_field.name, cfv.value)
                for cfv in rmodels.CustomFieldValue.objects.filter(
                    custom_recap=recap
                ).select_related("custom_field")
            ]
        )
        if sampled is not None:
            recap.total_engagements = sampled
            recap.save(update_fields=["total_engagements", "updated_at"])

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
        # Resolve each distinct category once, not once per photo — a BA files
        # 20 shots across 4 buckets and the resolver hits the DB every call.
        category_cache: dict[str, object] = {}
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
            # Which bucket the BA dropped this photo into.
            #
            # A validated bucket id is a REAL PK and must NOT go through
            # _resolve_file_recap_category: that helper reads "1"/"2" as
            # positional SENTINELS (photos / receipts) before it ever looks at
            # PKs, and a tenant's own category can legitimately have PK 1 or 2.
            # LD's "Table Set Up" is PK 2, so routing it through the resolver
            # filed every table shot under "Receipts" — the same mis-file this
            # feature exists to end. `_resolve_explicit_file_recap_category` is
            # the PK-only counterpart: tenant-scoped, no sentinel reading.
            #
            # Everything else — no category (every brand without buckets), a
            # stale id from a page loaded before the buckets changed, a forged
            # one — falls back to the "1" sentinel and lands in the tenant's
            # "photos" category, the same bucket the app/web recap forms use.
            #
            # A stale id also covers a BA who switched PROGRAMS on a page they
            # left open: the other program's bucket isn't in this event's
            # allow-set, so the photo lands in the generic pile rather than in a
            # bucket this program never offered.
            raw_category = (
                file_input.get("category") or file_input.get("categoryId") or ""
            )
            raw_category = str(raw_category).strip()
            if raw_category not in allowed_category_ids:
                raw_category = ""
            if raw_category not in category_cache:
                resolved = None
                if raw_category:
                    # Always tenant-scoped, so this can never reach another
                    # brand's category even if the id were somehow forged past
                    # the allow-set above.
                    resolved = _resolve_explicit_file_recap_category(
                        raw_category, tenant_id=event.tenant_id
                    )
                if resolved is None:
                    resolved = _resolve_file_recap_category(
                        "1", tenant_id=getattr(event, "tenant_id", None)
                    )
                category_cache[raw_category] = resolved
            file_recap_category = category_cache[raw_category]
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
                # After commit, via Cloud Tasks (same path as other recap
                # uploads). Never convert inline — 8 iPhone HEICs on LTE
                # would block File recap for the whole request.
                heic_conversion.schedule_jpg_sibling_blob(blob_name)

        # A recap with no photo on it is not a filed shift, it's an empty row in
        # the client's report. The page has always refused to submit one; this
        # closes the same door on the API, which until now would accept a
        # hand-made request with `files: []` and file exactly that.
        #
        # Deliberately a check on what the recap ENDS UP with, not on what this
        # request carried:
        #  - a request whose blobs were ALL rejected above (out-of-scope prefix,
        #    forged path) leaves no photo behind, and is the adversarial case
        #    this is for — counting the request's `files` would wave it through;
        #  - an EDIT of a recap that already has photos is a shift WITH photos,
        #    so re-submitting it carrying none is legitimate. The page happens to
        #    block that today (`photos` state starts empty, so a BA must re-add
        #    one to edit), but that is a page-side quirk worth fixing, and this
        #    must not be the thing standing in the way when it is.
        if not rmodels.CustomRecapFile.objects.filter(custom_recap=recap).exists():
            raise RecapNeedsAPhoto(
                f"recap for event {event.uuid} would have no photo; refusing"
            )

    if recap.approved:
        from ambassadors.walkup import approve_booking_for_recap

        try:
            approve_booking_for_recap(
                ambassador_id=ambassador.id,
                event_id=event.id,
                actor=actor,
            )
        except Exception:  # noqa: BLE001 — never fail a filed recap on hours
            logger.exception(
                "checkin recap: auto-approve booking failed recap=%s", recap.id
            )

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
            _kick_recap_approved_notify,
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
        if recap.approved:
            # Feel Free auto-approve: skip NEEDS REVIEW, send the same
            # client mail a human approve would (Girl Beer still no-ops).
            try:
                await _kick_recap_approved_notify(recap, "custom")
            except Exception:  # noqa: BLE001
                logger.exception(
                    "checkin recap: auto-approve notify failed id=%s", recap_id
                )
        else:
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


def _brand_image_url(tenant) -> str | None:
    """Same public URL tenantPublic.image returns, so /checkin and
    /spark-form feed TenantLogo the same mark."""
    if tenant is None:
        return None
    field_file = getattr(tenant, "image", None)
    if not field_file:
        return None
    try:
        blob = field_file.name
    except Exception:
        blob = str(field_file)
    if not blob:
        return None
    from utils.gcs import extract_blob_name_from_url, public_url

    return public_url(extract_blob_name_from_url(blob))


def _brand_payload(tenant) -> dict:
    image = _brand_image_url(tenant)
    slug = ""
    if tenant is not None:
        slug = (
            getattr(tenant, "request_url_name", None)
            or getattr(tenant, "slug", None)
            or ""
        )
    return {
        "name": (getattr(tenant, "name", None) or "") if tenant else "",
        "primaryColor": _brand_primary_color(tenant) if tenant else None,
        "image": image,
        "logoUrl": image,
        "requestUrlName": slug or None,
    }


def build_checkin_resources(tenant) -> list[dict]:
    """The BA-facing resource buttons for this tenant's check-in page.

    Prefers the `checkin_resources` list and falls back to synthesising one
    entry from the legacy single `checkin_training_url`. The fallback is what
    makes this safe to deploy ahead of any seeding: a tenant whose only config
    is the old field still gets its card, whether or not migration 0036's data
    step has run against this database.
    """
    if tenant is None:
        return []

    resources = normalize_checkin_resources(getattr(tenant, "checkin_resources", None))
    if resources:
        return _public_resource_urls(resources)

    legacy = (getattr(tenant, "checkin_training_url", "") or "").strip()
    if not legacy:
        return []
    return _public_resource_urls(
        normalize_checkin_resources(
            [
                {
                    "label": "BA reference & training",
                    "kind": "link",
                    "url": legacy,
                    "note": "Field guide, video, product sheets",
                }
            ]
        )
    )


def _public_resource_urls(resources: list[dict]) -> list[dict]:
    """Rewrite stored admin/spark hosts onto the BA-facing client origin.

    Feel Free PDFs were seeded with admin.igniteproductions.co; field
    phones have failed DNS on that host. ``absolute_public_url`` is the
    same rewrite event-confirmation emails already use.
    """
    from events.event_confirmations import absolute_public_url

    out: list[dict] = []
    for row in resources:
        url = absolute_public_url(row.get("url") or "")
        if not url:
            continue
        out.append({**row, "url": url})
    return out


def _public_training_url(tenant) -> str:
    if tenant is None:
        return ""
    from events.event_confirmations import absolute_public_url

    return absolute_public_url(
        getattr(tenant, "checkin_training_url", "") or ""
    )


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
    req = getattr(event, "request", None)
    payload = {
        "event": {
            "uuid": str(event.uuid),
            "name": event.name,
            "address": getattr(event, "address", None),
            "startTime": start.isoformat() if start else None,
            "endTime": end.isoformat() if end else None,
            "date": day.isoformat() if day else None,
            "storeManagerName": (
                (getattr(req, "store_manager_name", None) or "").strip() or None
            ),
            "storeManagerPhone": (
                (getattr(req, "store_manager_phone", None) or "").strip() or None
            ),
        },
        "brand": _brand_payload(tenant),
        # Same BA-facing references the tenant landing screen shows, repeated
        # here so they stay reachable mid-shift once the BA is clocked in.
        "resources": build_checkin_resources(tenant),
        # Legacy single-URL twin of `resources`. Still sent because the API
        # deploys BEFORE the front-end: dropping it would blank Liquid Death's
        # card on the live page for however long that gap lasts.
        "trainingUrl": _public_training_url(tenant),
        "template": serialize_template(event),
        # Labelled photo dropzones for the recap step, for THIS event's program.
        # Empty for every brand that hasn't opted in, which is the page's signal
        # to keep its single generic "Photos" grid.
        "photoBuckets": serialize_photo_buckets(event),
        # Same key the standing-link landing payload sends. Without it the
        # clocked-in page cannot tell market (Feel Free) from store (Torch)
        # and hides Log this stop.
        "locationMode": tenant_location_mode(tenant),
    }
    # Which program this event is — so a BA who picked one can see the page
    # agreed with them before they start filling in a 15-field form.
    etype = getattr(event, "event_type", None)
    if etype is not None:
        payload["event"]["eventType"] = {
            "id": str(etype.id),
            "name": etype.name or "",
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
            # Only present when the gig has track_mileage on, so the control
            # never appears for brands that don't reimburse driving.
            "mileage": mileage_state(ambassador=ambassador, event=event),
            "stops": sampling_stops(ambassador=ambassador, event=event),
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
    tenant = Tenant.objects.filter(checkin_recap_code__iexact=clean).first()
    if tenant is not None:
        return "tenant", tenant
    return None, None


def is_recap_only_code(code: str, tenant) -> bool:
    """True when ``code`` is this tenant's 3rd-party recap-only URL.

    The BA clock link (`checkin_code`) is tried first in resolve, so a
    mistaken duplicate of the two codes would still clock. This helper is
    the page/API switch for skipping punch.
    """
    recap = (getattr(tenant, "checkin_recap_code", None) or "").strip()
    return bool(recap) and recap.lower() == (code or "").strip().lower()


def recap_only_identity_phone(
    *, first_name: str, last_name: str, email: str | None
) -> str:
    """Stable fake phone so a 3rd-party filer without a number reuses one stub.

    Walk-up identity is phone-keyed (``checkin-{digits}@walkup.spark``). Agency
    recaps only require a name; hashing name+email keeps the same person on
    the same ambassador without colliding with a real NANP number.
    """
    raw = (
        f"{(first_name or '').strip().lower()}|"
        f"{(last_name or '').strip().lower()}|"
        f"{(email or '').strip().lower()}"
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    n = int(digest[:12], 16) % (10**10)
    return f"000{n:010d}"


def normalize_place(value: str) -> str:
    """The 'is this the same store?' key: case-, punctuation- and
    spacing-insensitive, so `1155 E. State St.` and `1155 e state st` are one
    place and two BAs there don't fork into two events."""
    v = (value or "").strip().lower()
    v = re.sub(r"[^\w\s]", " ", v)
    return re.sub(r"\s+", " ", v).strip()


# Street-type suffix words we drop when deciding "same store" for a walk-in.
# A scheduled event carries an admin-TYPED address ("1201 Avocado Ave"); a
# walk-in's address is REVERSE-GEOCODED from the BA's phone GPS ("1201 Avocado
# Boulevard, El Cajon, CA 92020"). Those name one place, but normalize_place()
# keeps them distinct (ave≠boulevard, and one carries a ZIP), so the walk-in
# forks a duplicate event and the scheduled row is left reading DUE. The core
# key below collapses the two by dropping the suffix + any ZIP.
_STREET_SUFFIXES = {
    "ave", "avenue", "blvd", "boulevard", "st", "street", "rd", "road",
    "dr", "drive", "ln", "lane", "ct", "court", "cir", "circle", "pl",
    "place", "way", "ter", "terrace", "hwy", "highway", "pkwy", "parkway",
    "sq", "square", "trl", "trail", "pike", "plaza", "row", "run", "path",
    "loop", "aly", "alley", "expy", "expressway", "fwy", "freeway", "byp",
    "bypass", "xing", "crossing", "commons", "center", "ctr", "mall",
}

# Country tokens that a reverse-geocoder tacks on (", USA") but an admin
# rarely types — dropped so "…El Cajon, CA 92020, USA" and "…El Cajon, CA
# 92020" collapse to the same place.
_ADDR_COUNTRY_TOKENS = {"usa", "us", "united", "states"}

# Directional words → a single canonical form. A reverse-geocoder spells them
# out ("North Clybourn Avenue") while an admin abbreviates ("N Clybourn Ave"),
# so we fold both to the abbreviation before comparing.
_DIRECTIONALS = {
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northeast": "ne", "northwest": "nw", "southeast": "se", "southwest": "sw",
}


def address_core_key(value: str) -> str:
    """A looser "same store?" key than :func:`normalize_place`: on top of the
    case/punctuation/space flattening it drops street-type suffix words
    (ave/avenue/blvd/…), country tokens (usa) and any trailing 5-digit ZIP, and
    folds directionals (north→n), so an admin-typed address and its
    reverse-geocoded twin collapse to one place.

    Only trusted when the address begins with a STREET NUMBER — that keeps the
    key tight (number + street name + city/state), so it collapses suffix/ZIP
    noise without merging genuinely different addresses. Returns "" when there's
    no leading number; callers then fall back to the strict normalize_place key.
    The (tenant, date, event-type) scope around the match adds further safety.
    """
    base = normalize_place(value)
    if not base:
        return ""
    toks = base.split()
    # Require a leading street number; without one this loose key isn't safe.
    if not toks[0].isdigit():
        return ""
    core = []
    for i, t in enumerate(toks):
        if t in _STREET_SUFFIXES or t in _ADDR_COUNTRY_TOKENS:
            continue
        # Drop a 5-digit ZIP, but never the leading street number (i == 0).
        if i > 0 and re.fullmatch(r"\d{5}", t):
            continue
        core.append(_DIRECTIONALS.get(t, t))
    return " ".join(core).strip()


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


def _store_display_name(name: str, address: str) -> str:
    """A picker label that names the STORE, not the walk-in event title.

    Walk-in events are titled "8/17/2026 - 1648 NW Chipman Road… (Torch
    Sampling - Total Wine & More (Lee's Summit))" — dumping that into a
    <select> is unreadable. Prefer a parenthetical store, then strip a
    "Torch Sampling - " prefix, then the address itself.
    """
    n = (name or "").strip()
    a = (address or "").strip()
    if not n:
        return a
    addr_key = normalize_place(a)[:24] if a else ""
    if addr_key and addr_key in normalize_place(n):
        if "(" in n and n.endswith(")"):
            inner = n[n.rfind("(") + 1 : -1].strip()
            if inner:
                return inner
        return a
    if re.match(r"^\d{1,2}/\d{1,2}/\d{4}", n):
        if "(" in n and n.endswith(")"):
            inner = n[n.rfind("(") + 1 : -1].strip()
            if inner:
                return inner
        return a
    stripped = re.sub(r"^(torch sampling\s*-\s*)", "", n, flags=re.I).strip()
    return stripped or n


def checkin_store_options(tenant, limit: int = 200) -> list:
    """Unique store name + address for the recap-only picker.

    Account Map / Master Tracker venues (Request rows) first, then Retailer
    rows, then recent events. Deduped by normalize_place(address) so 202
    Total Wine tracker rows collapse to the ~32 unique stores. Sorted by
    name so a 3rd-party filer can find a location instead of free-typing.
    """
    from events.models import Event, Request, Retailer

    seen: set = set()
    out: list = []

    def _add(name: str, address: str) -> None:
        addr = (address or "").strip()
        key = normalize_place(addr)
        if not key or key in seen:
            return
        if len(out) >= limit:
            return
        seen.add(key)
        label = _store_display_name(name, addr)
        out.append({"name": label, "address": addr})

    req_rows = (
        Request.objects.filter(tenant=tenant, deleted_at__isnull=True)
        .exclude(address="")
        .values("address", "retailer_address", "retailer_name", "retailer__name", "name")
    )
    for row in req_rows:
        addr = (row.get("address") or row.get("retailer_address") or "").strip()
        name = (
            (row.get("retailer_name") or "").strip()
            or (row.get("retailer__name") or "").strip()
            or (row.get("name") or "").strip()
        )
        _add(name, addr)

    for row in Retailer.objects.filter(tenant=tenant).exclude(address__isnull=True).exclude(address="").values("name", "address"):
        _add(row.get("name") or "", row.get("address") or "")

    for row in (
        Event.objects.filter(tenant=tenant)
        .exclude(address="")
        .order_by("-id")
        .values("name", "address")[: limit * 4]
    ):
        _add(row.get("name") or "", row.get("address") or "")

    out.sort(key=lambda x: ((x.get("name") or x.get("address") or "").lower()))
    return out


def suggest_store_matches(tenant, name: str, address: str, limit: int = 8) -> list:
    """Maybe-matches for a typed 3rd-party store (never auto-applied).

    Scores tenant Retailer rows (national chain vs regional store) and
    Account Map / Master Tracker Request venues. Admin confirms one.
    """
    from events.models import Request, Retailer

    if tenant is None:
        return []

    name_n = normalize_place(name)
    addr_n = normalize_place(address)
    addr_core = address_core_key(address)
    name_tokens = set(name_n.split()) if name_n else set()
    scored: dict = {}

    def _add(retailer, score: int, reason: str, kind: str) -> None:
        if retailer is None or score < 35:
            return
        key = str(retailer.uuid)
        prev = scored.get(key)
        if prev and prev["score"] >= score:
            return
        scored[key] = {
            "uuid": key,
            "name": retailer.name or "",
            "address": retailer.address or "",
            "kind": kind,
            "isNational": bool(retailer.is_national),
            "score": int(score),
            "reason": reason,
        }

    for retailer in Retailer.objects.filter(tenant=tenant):
        rname = normalize_place(retailer.name or "")
        raddr = normalize_place(retailer.address or "")
        rcore = address_core_key(retailer.address or "")
        if addr_n and raddr and addr_n == raddr:
            _add(retailer, 95, "Maybe this store — same address", "store")
            continue
        if addr_core and rcore and addr_core == rcore:
            _add(retailer, 82, "Maybe this store — same street", "store")
        overlap = name_tokens & set(rname.split()) if rname else set()
        if len(overlap) >= 2:
            score = 50 + 10 * min(len(overlap), 4)
            if retailer.is_national:
                _add(retailer, min(score, 70), "Maybe the national account", "chain")
            elif addr_n and raddr and (set(addr_n.split()) & set(raddr.split())):
                _add(retailer, min(score + 15, 88), "Maybe this regional store", "store")
            else:
                _add(retailer, min(score, 65), "Maybe this regional chain", "chain")
        elif overlap and retailer.is_national:
            _add(retailer, 48, "Maybe the national account", "chain")

    for row in Request.objects.filter(
        tenant=tenant, deleted_at__isnull=True, retailer_id__isnull=False
    ).select_related("retailer"):
        if row.retailer_id is None:
            continue
        req_addr = row.address or row.retailer_address or ""
        req_name = row.retailer_name or getattr(row.retailer, "name", "") or ""
        if addr_n and normalize_place(req_addr) == addr_n:
            _add(row.retailer, 92, "On the Account Map at this address", "store")
        elif addr_core and address_core_key(req_addr) == addr_core:
            _add(row.retailer, 80, "On the Account Map — same street", "store")
        elif name_tokens and len(name_tokens & set(normalize_place(req_name).split())) >= 2:
            _add(row.retailer, 60, "On the Account Map / Master Tracker", "store")

    return sorted(scored.values(), key=lambda x: -x["score"])[:limit]


def _event_date_utc(on_date):
    """A calendar date stored as NOON UTC.

    Midnight would be the obvious choice and is wrong here: read back in any US
    zone (UTC-4 … UTC-10) midnight UTC lands on the PREVIOUS evening, so the
    event would report a day early everywhere the work actually happened. Noon
    UTC reads as 2am–8am on the intended date across every US zone.
    """
    from datetime import time

    return datetime.combine(on_date, time(12, 0), tzinfo=dt_timezone.utc)


def event_calendar_date(event) -> date_cls | None:
    """The civil date ``Event.date`` was stored to represent.

    Walk-in events are stamped noon UTC, so the UTC date *is* the intended
    calendar day in every US zone. Do not convert the instant into a US
    timezone first — midnight UTC would then read as the previous evening.
    """
    day = getattr(event, "date", None)
    if day is None:
        return None
    if isinstance(day, datetime):
        if day.tzinfo is not None:
            return day.astimezone(dt_timezone.utc).date()
        return day.date()
    if isinstance(day, date_cls):
        return day
    raw = str(day)
    try:
        return date_cls.fromisoformat(raw[:10])
    except ValueError:
        return None


def selectable_event_types(tenant) -> list:
    """The programs this brand's standing link offers, in id order.

    See ``Tenant.checkin_event_types``. Empty or single-entry means there is
    nothing to choose, so the page asks nothing — the caller is expected to
    hide the selector rather than render a one-option dropdown.
    """
    if tenant is None:
        return []
    try:
        return list(
            tenant.checkin_event_types.filter(tenant_id=tenant.id).order_by("id")
        )
    except Exception:  # noqa: BLE001 — a broken config must never close the link
        logger.exception(
            "selectable event types failed tenant=%s", getattr(tenant, "id", None)
        )
        return []


def resolve_checkin_event_type(tenant, raw_id):
    """The EventType a BA picked, or ``None``.

    Tenant-scoped on purpose: this arrives from a PUBLIC endpoint, and an
    unscoped lookup would let anyone stamp another brand's event type onto this
    brand's event — which is also how they'd pull another brand's recap
    template through ``resolve_template_for_event``. Anything we can't match
    inside the tenant is treated as "not answered" and falls back to
    ``_default_event_type``, so a forged or stale id can never block a
    check-in.
    """
    if tenant is None or raw_id in (None, ""):
        return None
    from events.models import EventType

    try:
        wanted = int(str(raw_id).strip())
    except (TypeError, ValueError):
        return None
    return EventType.objects.filter(tenant_id=tenant.id, id=wanted).first()


def _default_event_type(tenant):
    """The event type the standing link stamps when the BA didn't choose one.

    This decides WHICH RECAP FORM the BA gets: `resolve_template_for_event`
    matches the tenant's templates on `event_type_id` before anything else.
    Prefer the tenant's explicit `checkin_event_type`, then the first of its
    selectable programs; the lowest-id fallback at the end is arbitrary and
    actively wrong for a brand running more than one program. Liquid Death has
    both an "Event Activation" and a "Retail Sampling" template — picking by id
    would hand a retail BA the activation form, and the resulting recap
    wouldn't look broken enough to notice.
    """
    from events.models import EventType

    try:
        pinned = getattr(tenant, "checkin_event_type", None)
        if pinned is not None:
            return pinned
        offered = selectable_event_types(tenant)
        if offered:
            return offered[0]
        return (
            EventType.objects.filter(tenant=tenant)
            .order_by("id")
            .first()
        )
    except Exception:  # noqa: BLE001 — event type is optional on Event
        return None


def find_or_create_walkin_event(
    *, tenant, store_name: str, address: str, on_date, actor, event_type=None
):
    """The event for (tenant, address, date, event type) — found, else created.

    Returns ``(event, created)``. Wrapped in a transaction with a re-check so two
    BAs tapping "start" at the same second still land on one event.

    THE EVENT TYPE IS PART OF THE KEY. It didn't used to be, because a brand's
    standing link ran one program and every walk-in event it opened carried the
    same type. Now that the BA picks their program, "same place, same day" is no
    longer the same activation: a retail demo and an event activation at one
    address on one date are two different jobs with two different recap forms.
    Keying only on (tenant, address, date) would collapse them into a single
    event carrying a single type, and the second BA would silently be handed the
    first BA's form — a recap that submits cleanly against the wrong template,
    which is the failure mode nobody notices.

    Several BAs working the SAME program at the same place on the same day still
    share one event, which is the behaviour this key exists to protect.

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

    chosen_type = event_type or _default_event_type(tenant)
    type_id = getattr(chosen_type, "id", None)
    # Only NAME the program when the brand actually runs more than one. A
    # single-program brand's event titles stay exactly as they read today.
    offered = selectable_event_types(tenant)
    program = (
        (getattr(chosen_type, "name", "") or "").strip()
        if chosen_type is not None and len(offered) > 1
        else ""
    )

    core_key = address_core_key(address)

    def _match():
        # Small set (one tenant, one day), so normalize in Python rather than
        # trying to express the same collapsing in SQL. Two tiers:
        #   1. EXACT  — normalize_place equality (the original behaviour).
        #   2. FUZZY  — address_core_key equality (suffix/ZIP-insensitive), so a
        #              reverse-geocoded walk-in address connects to the scheduled
        #              event's admin-typed one instead of forking a duplicate.
        # Within each tier prefer a SCHEDULED event (one born from a request) so
        # its "DUE" clears rather than the walk-in landing on a sibling event.
        exact: list = []
        fuzzy: list = []
        for ev in Event.objects.filter(tenant=tenant, date__gte=lo, date__lte=hi):
            # An untyped check-in (brand has no event types at all) keeps the
            # old address+date behaviour and joins whatever is there.
            if type_id is not None and getattr(ev, "event_type_id", None) != type_id:
                continue
            ev_addr = getattr(ev, "address", "")
            if normalize_place(ev_addr) == key:
                exact.append(ev)
            elif core_key and address_core_key(ev_addr) == core_key:
                fuzzy.append(ev)
        for bucket in (exact, fuzzy):
            if not bucket:
                continue
            scheduled = [e for e in bucket if getattr(e, "request_id", None)]
            return (scheduled or bucket)[0]
        return None

    with transaction.atomic():
        existing = _match()
        if existing is not None:
            return existing, False

        name = walkin_event_name(
            store_name=store_name,
            address=address,
            on_date=on_date,
            program=program,
        )
        # Carry the brand's mileage answer onto the event as it's born — a
        # walk-in event has no admin to tick the per-gig box, so without this
        # the drive control never appears on the standing link.
        event = Event.objects.create(
            tenant=tenant,
            track_mileage=bool(getattr(tenant, "default_track_mileage", False)),
            mileage_rate=getattr(tenant, "default_mileage_rate", None),
            name=name[:255],
            address=(address or "").strip(),
            date=day_start,
            event_type=chosen_type,
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


def build_tenant_context(tenant, *, recap_only: bool = False) -> dict:
    """Payload for a standing tenant link before any event exists.

    ``needsEventDetails`` tells the page to ask for store + date first; the rest
    of the flow is identical to the per-event link once identify resolves one.
    ``recap_only`` is the 3rd-party twin: no clock, typed store name +
    address (admin maps maybe-matches later), same recap questions.
    """
    stores = [] if recap_only else recent_checkin_locations(tenant)
    return {
        "mode": "tenant",
        "needsEventDetails": True,
        "recapOnly": bool(recap_only),
        "brand": _brand_payload(tenant),
        "recentLocations": stores,
        # Roaming brands pick a market instead of typing a store address.
        "locationMode": tenant_location_mode(tenant),
        "markets": tenant_markets(tenant),
        # The programs on offer. Fewer than two and the page must ask nothing —
        # a one-option dropdown is a worse version of no dropdown, and brands
        # with a single program (Total Wireless, Feel Free) have to look exactly
        # as they do today.
        "eventTypes": [
            {"id": str(t.id), "name": t.name or ""}
            for t in selectable_event_types(tenant)
        ],
        # BA-facing resources (training deck, photo-release QR, the brand's
        # /training/<code> hub) as ordered buttons. See build_checkin_resources.
        "resources": build_checkin_resources(tenant),
        # Legacy single-URL twin — see the note in build_public_context.
        "trainingUrl": _public_training_url(tenant),
    }


def checkin_recap_open_url(recap) -> str:
    """Client-host permalink for the recap-submitted ops mailer CTA.

    Walk-up filings are CustomRecap rows; the document lives at
    ``/recap/view-custom/:uuid``. Legacy Recap uses ``/recap/view/:uuid``.
    Always mint ``client.igniteproductions.co`` (never admin or spark.) so
    the click lands on that brand's recap, not a tenant-less /recaps list.
    """
    from events.event_confirmations import public_page_base
    from recaps import models as rmodels

    base = public_page_base().rstrip("/")
    uuid = getattr(recap, "uuid", None)
    if uuid:
        path = (
            f"/recap/view-custom/{uuid}"
            if isinstance(recap, rmodels.CustomRecap)
            else f"/recap/view/{uuid}"
        )
        return f"{base}{path}"
    return f"{base}/recaps/list"


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

    # People, by name — a recap is something a human reviews, unlike the clock
    # punches that go to the shared events@ inbox.
    to = [
        e.strip()
        for e in getattr(settings, "CHECKIN_RECAP_NOTIFY_EMAILS", [])
        if (e or "").strip()
    ]
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

    open_url = checkin_recap_open_url(recap)
    link = (
        f"<div style='margin:16px 0 4px'><a href='{escape(open_url)}' "
        "style='display:inline-block;background:#c5f546;color:#0a0d09;"
        "padding:10px 18px;border-radius:10px;text-decoration:none;"
        "font-weight:700'>Open recaps</a></div>"
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
        f"submitted a recap for <a href='{escape(open_url)}' "
        f"style='color:#14181a'><strong>{escape(where)}</strong></a>.</p>"
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


def open_shift_event_for(*, ambassador, tenant, on_date=None):
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
    clocked in — so an open shift wins over anything they type **on the same
    calendar day**.

    ``on_date`` is the date the BA picked. A leftover Sunday clock-in must
    not steal Wednesday's identify onto Sunday's market event (Feel Free:
    Alicia, Aug 2026). Same-day resume still ignores a retyped address.

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
                event = (
                    Event.objects.select_related(
                        "tenant", "request", "retailer", "location", "state", "timezone", "event_type"
                    )
                    .filter(id=event_id)
                    .first()
                )
                if event is None:
                    continue
                if on_date is not None and event_calendar_date(event) != on_date:
                    continue
                return event
    except Exception:  # noqa: BLE001 — never block a check-in over this
        logger.exception(
            "open-shift lookup failed ambassador=%s tenant=%s",
            getattr(ambassador, "id", None), getattr(tenant, "id", None),
        )
    return None


# ── Web mileage: an ODOMETER, not a breadcrumb trail ────────────────────────
#
# The mobile app records a dense GPS trail and map-matches it. A browser
# CANNOT: mobile Safari suspends JavaScript and geolocation the moment the
# screen locks or the tab backgrounds, which is most of any drive. A
# breadcrumb loop in a browser produces a trail full of holes and a mileage
# number that is badly under-reported while looking authoritative — worse than
# no number at all.
#
# So the web flow takes ONE fix at Start and ONE at Stop and asks OSRM to route
# between them over real streets. That is honest about what it knows: the road
# distance between where they started and where they stopped. Sessions are
# stamped ``route_source="osrm_route"`` so a reader can always tell an odometer
# leg from the app's matched trail.

# Anything longer than this is a forgotten Stop, not a drive. Auto-closed with
# no distance rather than left active forever blocking a new leg.
MILEAGE_MAX_OPEN_HOURS = 12
WEB_MILEAGE_SOURCE = "osrm_route"


def _mileage_enabled(event) -> bool:
    """Per-gig toggle — same `Event.track_mileage` flag the app respects."""
    return bool(getattr(event, "track_mileage", False))


def active_mileage_session(*, ambassador, event):
    """This BA's open leg on this event, or None. Auto-closes a stale one."""
    from ambassadors.models import MileageSession

    if ambassador is None or event is None:
        return None
    session = (
        MileageSession.objects.filter(
            ambassador=ambassador, event=event, status=MileageSession.STATUS_ACTIVE
        )
        .order_by("-started_at")
        .first()
    )
    if session is None:
        return None
    if session.started_at and session.started_at < dj_tz.now() - timedelta(
        hours=MILEAGE_MAX_OPEN_HOURS
    ):
        session.status = MileageSession.STATUS_CANCELED
        session.ended_at = dj_tz.now()
        session.save(update_fields=["status", "ended_at", "updated_at"])
        return None
    return session


def _leg_payload(session) -> dict:
    return {
        "uuid": str(session.uuid),
        "status": session.status,
        "startedAt": session.started_at.isoformat() if session.started_at else None,
        "endedAt": session.ended_at.isoformat() if session.ended_at else None,
        "miles": float(session.total_miles) if session.total_miles is not None else None,
        "amount": (
            float(session.reimbursement_amount)
            if session.reimbursement_amount is not None
            else None
        ),
    }


def mileage_state(*, ambassador, event) -> dict:
    """What the check-in page needs to render the mileage control."""
    from ambassadors.models import MileageSession

    if not _mileage_enabled(event) or ambassador is None:
        return {"enabled": False, "active": None, "legs": [], "totalMiles": 0.0,
                "totalAmount": 0.0}

    active = active_mileage_session(ambassador=ambassador, event=event)
    done = list(
        MileageSession.objects.filter(
            ambassador=ambassador, event=event,
            status=MileageSession.STATUS_COMPLETED,
        ).order_by("started_at")
    )
    total_miles = sum(float(s.total_miles or 0) for s in done)
    total_amount = sum(float(s.reimbursement_amount or 0) for s in done)
    return {
        "enabled": True,
        "active": _leg_payload(active) if active else None,
        "legs": [_leg_payload(s) for s in done],
        "totalMiles": round(total_miles, 2),
        "totalAmount": round(total_amount, 2),
    }


def start_mileage_leg(*, ambassador, event, coordinates) -> tuple[dict | None, str | None]:
    """Open a leg from a single GPS fix. Returns (state, error_message)."""
    from ambassadors.models import MileageBreadcrumb, MileageSession

    if not _mileage_enabled(event):
        return None, "Mileage isn't being tracked for this event."
    if not coordinates or len(coordinates) < 2:
        return None, "We couldn't get your location. Turn on location and try again."
    try:
        lat, lng = float(coordinates[0]), float(coordinates[1])
    except (TypeError, ValueError):
        return None, "We couldn't read your location."
    if lat == 0.0 and lng == 0.0:
        return None, "We couldn't get a location fix. Try again outside."

    if active_mileage_session(ambassador=ambassador, event=event) is not None:
        # Not an error worth blocking on — just hand back the current state so
        # a double-tap doesn't open two legs.
        return mileage_state(ambassador=ambassador, event=event), None

    session = MileageSession.objects.create(
        tenant=getattr(event, "tenant", None),
        ambassador=ambassador,
        event=event,
        status=MileageSession.STATUS_ACTIVE,
    )
    # The start fix is stored as a breadcrumb so the pair (first, last) is
    # readable by the same admin surfaces that render the app's trail.
    MileageBreadcrumb.objects.create(
        session=session, lat=lat, lng=lng, recorded_at=dj_tz.now()
    )
    return mileage_state(ambassador=ambassador, event=event), None


def stop_mileage_leg(*, ambassador, event, coordinates) -> tuple[dict | None, str | None]:
    """Close the open leg, routing start->end over real roads for distance."""
    from decimal import Decimal

    from ambassadors.models import MileageBreadcrumb, MileageSession
    from utils.map_matching import osrm_route

    if not _mileage_enabled(event):
        return None, "Mileage isn't being tracked for this event."
    session = active_mileage_session(ambassador=ambassador, event=event)
    if session is None:
        return None, "No drive is running. Tap Start drive first."

    end = None
    if coordinates and len(coordinates) >= 2:
        try:
            lat, lng = float(coordinates[0]), float(coordinates[1])
            if not (lat == 0.0 and lng == 0.0):
                end = (lat, lng)
        except (TypeError, ValueError):
            end = None

    start_crumb = session.breadcrumbs.order_by("recorded_at", "id").first()
    if end is not None:
        MileageBreadcrumb.objects.create(
            session=session, lat=end[0], lng=end[1], recorded_at=dj_tz.now()
        )

    miles = None
    route = None
    if start_crumb is not None and end is not None:
        routed = osrm_route((start_crumb.lat, start_crumb.lng), end)
        if routed:
            miles = routed.get("miles")
            route = routed.get("route")

    session.status = MileageSession.STATUS_COMPLETED
    session.ended_at = dj_tz.now()
    session.route_source = WEB_MILEAGE_SOURCE if miles is not None else ""
    if route:
        session.route = route
    if miles is not None:
        session.total_miles = Decimal(str(miles))
        rate = getattr(event, "mileage_rate", None)
        if rate:
            session.rate_per_mile = rate
            session.reimbursement_amount = (
                Decimal(str(miles)) * Decimal(str(rate))
            ).quantize(Decimal("0.01"))
    session.save(
        update_fields=[
            "status", "ended_at", "total_miles", "rate_per_mile",
            "reimbursement_amount", "route", "route_source", "updated_at",
        ]
    )

    state = mileage_state(ambassador=ambassador, event=event)
    if miles is None:
        # Leg is closed either way — never leave a BA with a stuck timer — but
        # say plainly that no distance was recorded rather than showing 0 mi as
        # if they drove nowhere.
        return state, "Drive stopped, but we couldn't work out the distance."
    return state, None


# ── Roaming crews: markets instead of store addresses ───────────────────────
#
# See Tenant.checkin_location_mode. A static activation keys its event on the
# store address the BA types; a roaming crew keys it on the MARKET they picked,
# and the individual spots become SamplingStops.

def tenant_location_mode(tenant) -> str:
    from tenants.models import Tenant

    mode = (getattr(tenant, "checkin_location_mode", "") or "").strip()
    return mode if mode == Tenant.CHECKIN_LOCATION_MARKET else Tenant.CHECKIN_LOCATION_ADDRESS


# A choice field named like this on the brand's recap template IS their market
# list. Reading it means the check-in page and the recap can't drift apart.
_MARKET_FIELD_RE = re.compile(r"market|event\s*location|city", re.IGNORECASE)


def tenant_markets(tenant) -> list:
    """The brand's market list, in order.

    Explicit ``Tenant.checkin_markets`` wins; otherwise read the options off
    the brand's own recap template choice field (Feel Free's "Event Location:"
    already holds Miami / Ft. Lauderdale / Tampa / Austin / San Antonio). ONE
    list, so a market added to the recap form shows up on the link too.
    """
    explicit = getattr(tenant, "checkin_markets", None)
    if isinstance(explicit, list) and explicit:
        return [str(m).strip() for m in explicit if str(m).strip()]

    from recaps.models import CustomField

    try:
        rows = (
            CustomField.objects.filter(
                custom_recap_template__tenant_id=getattr(tenant, "id", None)
            )
            .select_related("custom_field_type")
            .order_by("id")
        )
        for cf in rows:
            kind = (getattr(cf.custom_field_type, "name", "") or "").lower()
            if "select" not in kind and "dropdown" not in kind:
                continue
            if not _MARKET_FIELD_RE.search(cf.name or ""):
                continue
            opts = [str(o).strip() for o in (cf.options or []) if str(o).strip()]
            if opts:
                return opts
    except Exception:  # noqa: BLE001 — never break a check-in over this
        logger.exception("market lookup failed tenant=%s", getattr(tenant, "id", None))
    return []


def log_sampling_stop(
    *, ambassador, event, coordinates, name: str = ""
) -> tuple[dict | None, str | None]:
    """Record one place the BA sampled. Returns (stop_payload, error)."""
    from ambassadors.models import SamplingStop

    lat = lng = None
    if coordinates and len(coordinates) >= 2:
        try:
            lat, lng = float(coordinates[0]), float(coordinates[1])
        except (TypeError, ValueError):
            lat = lng = None
        if lat == 0.0 and lng == 0.0:
            lat = lng = None

    label = (name or "").strip()[:255]
    if lat is None and not label:
        return None, "Turn on location, or type where you are, so we can log the stop."

    # Reverse-geocode best-effort — a stop is still worth recording without a
    # street address, and the coordinates are the part we actually trust.
    address = ""
    if lat is not None:
        try:
            from utils.geocoding import photon_reverse

            address = (photon_reverse(lat, lng) or "")[:512]
        except Exception:  # noqa: BLE001
            address = ""

    stop = SamplingStop.objects.create(
        ambassador=ambassador,
        event=event,
        lat=lat,
        lng=lng,
        address=address,
        name=label,
        recorded_at=dj_tz.now(),
    )
    # Mirror onto the ping trail so the stop plots on the admin map and the
    # per-event GPS trail without any new admin UI.
    if lat is not None:
        record_location_ping(
            ambassador=ambassador, event=event, coordinates=[lat, lng], source="foreground"
        )
    return _stop_payload(stop), None


def _stop_payload(stop) -> dict:
    return {
        "uuid": str(stop.uuid),
        "name": stop.name or "",
        "address": stop.address or "",
        "lat": stop.lat,
        "lng": stop.lng,
        "recordedAt": stop.recorded_at.isoformat() if stop.recorded_at else None,
    }


def sampling_stops(*, ambassador, event) -> list:
    """This BA's stops on this event, oldest first."""
    from ambassadors.models import SamplingStop

    if ambassador is None or event is None:
        return []
    return [
        _stop_payload(s)
        for s in SamplingStop.objects.filter(
            ambassador=ambassador, event=event
        ).order_by("recorded_at", "id")
    ]
