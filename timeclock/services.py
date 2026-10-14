"""Clock in / clock out for public ``ClockLink`` pages.

Views stay thin; everything that decides what a clock action means lives
here so the admin surface and tests exercise the same rules.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core import signing
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, close_old_connections, transaction
from django.utils import timezone as dj_tz

from ambassadors.checkin_web import _brand_payload
from timeclock.models import DEFAULT_TIMEZONE, ClockLink, ClockSession
from timeclock.sheets import sync_session
from utils import cloud_tasks

logger = logging.getLogger(__name__)

SESSION_SALT = "timeclock.session"
# Long enough for an overnight or multi-day shift; past that the worker
# clocks out by typing their email again.
SESSION_MAX_AGE_SECONDS = 14 * 24 * 3600

MAX_LEN = {"first_name": 80, "last_name": 80, "market": 120, "address": 300}


class ClockError(Exception):
    def __init__(self, message: str, *, code: str = "invalid", status: int = 400, extra: dict | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.extra = extra or {}


@dataclass(frozen=True)
class GpsFix:
    status: str
    lat: float | None = None
    lng: float | None = None
    accuracy_m: float | None = None


def make_session_token(session: ClockSession) -> str:
    return signing.dumps({"s": str(session.uuid), "l": session.link_id}, salt=SESSION_SALT)


def read_session_token(token: str, link: ClockLink) -> ClockSession | None:
    if not token:
        return None
    try:
        data = signing.loads(token, salt=SESSION_SALT, max_age=SESSION_MAX_AGE_SECONDS)
    except signing.BadSignature:
        return None
    if not isinstance(data, dict) or data.get("l") != link.id:
        return None
    return ClockSession.objects.filter(link=link, uuid=data.get("s")).first()


def resolve_link(token: str) -> ClockLink | None:
    if not token or len(token) > 64:
        return None
    return ClockLink.objects.select_related("tenant").filter(token=token).first()


def normalize_email(raw) -> str:
    email = (raw or "").strip().lower() if isinstance(raw, str) else ""
    if not email:
        raise ClockError("Enter your email.", code="email_required")
    try:
        validate_email(email)
    except ValidationError:
        raise ClockError("That email doesn't look right. Check it and try again.", code="bad_email")
    return email


def _required_text(data: dict, key: str, field: str, label: str) -> str:
    value = data.get(key)
    value = " ".join(value.split()) if isinstance(value, str) else ""
    if not value:
        raise ClockError(f"Enter your {label}.", code=f"{field}_required")
    if len(value) > MAX_LEN[field]:
        raise ClockError(f"{label[:1].upper()}{label[1:]} is too long.", code=f"{field}_too_long")
    return value


def safe_timezone(name, fallback: str = DEFAULT_TIMEZONE) -> str:
    if isinstance(name, str) and 0 < len(name) <= 64:
        try:
            ZoneInfo(name)
            return name
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return fallback


def parse_gps(data: dict) -> GpsFix:
    """Read the phone's one-shot fix. Anything unusable is recorded, not fatal."""
    gps = data.get("gps")
    status = data.get("gpsStatus")
    if isinstance(gps, dict):
        try:
            lat = float(gps.get("lat"))
            lng = float(gps.get("lng"))
        except (TypeError, ValueError):
            lat = lng = None
        if lat is not None and -90 <= lat <= 90 and -180 <= lng <= 180:
            accuracy = gps.get("accuracy")
            try:
                accuracy = round(float(accuracy), 1) if accuracy is not None else None
            except (TypeError, ValueError):
                accuracy = None
            if accuracy is not None and accuracy < 0:
                accuracy = None
            return GpsFix(ClockSession.GPS_OK, round(lat, 6), round(lng, 6), accuracy)
    if status in ClockSession.GPS_STATUSES and status != ClockSession.GPS_OK:
        return GpsFix(status)
    return GpsFix(ClockSession.GPS_UNAVAILABLE)


def open_session_for(link: ClockLink, email: str) -> ClockSession | None:
    return ClockSession.objects.filter(link=link, email=email, clock_out_at__isnull=True).first()


def _already_in(session: ClockSession) -> ClockError:
    return ClockError(
        "You're already clocked in on this link. Clock out first.",
        code="already_clocked_in",
        status=409,
        extra={"openSession": session_payload(session)},
    )


def clock_in(link: ClockLink, data: dict, *, ip: str = "", user_agent: str = "") -> ClockSession:
    if not link.is_active:
        raise ClockError("This clock link is turned off.", code="inactive", status=410)
    first = _required_text(data, "firstName", "first_name", "first name")
    last = _required_text(data, "lastName", "last_name", "last name")
    email = normalize_email(data.get("email"))
    market = _required_text(data, "market", "market", "market")
    address = _required_text(data, "address", "address", "work address")
    fix = parse_gps(data)

    existing = open_session_for(link, email)
    if existing is not None:
        raise _already_in(existing)
    try:
        with transaction.atomic():
            session = ClockSession.objects.create(
                link=link,
                first_name=first,
                last_name=last,
                email=email,
                market=market,
                address=address,
                timezone=safe_timezone(data.get("timezone"), link.timezone),
                clock_in_at=dj_tz.now(),
                clock_in_lat=fix.lat,
                clock_in_lng=fix.lng,
                clock_in_accuracy_m=fix.accuracy_m,
                clock_in_gps=fix.status,
                ip_address=(ip or "")[:64],
                user_agent=(user_agent or "")[:300],
            )
    except IntegrityError:
        # Two taps raced past the check above; the partial unique index won.
        existing = open_session_for(link, email)
        if existing is not None:
            raise _already_in(existing)
        raise
    queue_sheet_sync(session)
    return session


def clock_out(link: ClockLink, data: dict, *, session: ClockSession | None = None) -> ClockSession:
    """Close the device's session, or the open session for the typed email."""
    if session is None:
        email = normalize_email(data.get("email"))
        session = open_session_for(link, email)
        if session is None:
            raise ClockError(
                "No open clock-in for that email on this link.",
                code="not_clocked_in",
                status=404,
            )
    elif not session.is_open:
        raise ClockError("You're already clocked out.", code="already_clocked_out", status=409,
                         extra={"session": session_payload(session)})
    fix = parse_gps(data)
    updated = ClockSession.objects.filter(pk=session.pk, clock_out_at__isnull=True).update(
        clock_out_at=dj_tz.now(),
        clock_out_lat=fix.lat,
        clock_out_lng=fix.lng,
        clock_out_accuracy_m=fix.accuracy_m,
        clock_out_gps=fix.status,
        sheet_dirty=True,
        updated_at=dj_tz.now(),
    )
    session.refresh_from_db()
    if not updated:
        raise ClockError("You're already clocked out.", code="already_clocked_out", status=409,
                         extra={"session": session_payload(session)})
    queue_sheet_sync(session)
    return session


def session_payload(session: ClockSession) -> dict:
    return {
        "id": str(session.uuid),
        "firstName": session.first_name,
        "lastName": session.last_name,
        "email": session.email,
        "market": session.market,
        "address": session.address,
        "timezone": session.timezone,
        "clockInAt": session.clock_in_at.isoformat(),
        "clockInGps": session.clock_in_gps,
        "clockOutAt": session.clock_out_at.isoformat() if session.clock_out_at else None,
        "clockOutGps": session.clock_out_gps or None,
        "durationMinutes": session.duration_minutes,
    }


def link_payload(link: ClockLink) -> dict:
    return {
        "label": link.label,
        "active": link.is_active,
        "timezone": link.timezone,
        "brand": _brand_payload(link.tenant) if link.tenant_id else None,
    }


# --------------------------------------------------------------------------
# Sheet mirror dispatch
# --------------------------------------------------------------------------
TASK_PATH = "/api/tasks/clock-sheet-sync"


def queue_sheet_sync(session: ClockSession) -> None:
    """Mirror after commit, off the request path.

    Cloud Tasks first (retried by the queue); a daemon thread when tasks are
    off. Either way the ``clock-sheet-sync`` cron sweeps anything still dirty.
    """
    if not session.link.sheet_id:
        return
    session_id = session.id
    transaction.on_commit(lambda: _dispatch(session_id))


def _dispatch(session_id: int) -> None:
    if cloud_tasks.enqueue(TASK_PATH, {"session_id": session_id}):
        return
    threading.Thread(target=_sync_in_thread, args=(session_id,), daemon=True).start()


def _sync_in_thread(session_id: int) -> None:
    try:
        sync_session(session_id)
    except Exception:  # noqa: BLE001 — the sweep retries; never crash a thread loudly
        logger.warning("clock sheet sync thread failed session=%s", session_id, exc_info=True)
    finally:
        close_old_connections()
