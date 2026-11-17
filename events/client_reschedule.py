"""Client "Change date": move an event (and its request) and tell Ignite.

A brand user (role "client") can move one of their events to a new day and
new start/end times from the event page. Only the date and times change:
staffing, address and products stay as they are. The event and its linked
request are written together so the tracker, the event page and every email
agree on when the visit is.

Rules (``reschedule_event_sync``):
  * the caller is an Ignite admin, or an active member of the EVENT's tenant
    (multi-brand clients are scoped by the record, never by their default
    tenant), and is not a brand ambassador;
  * the event is not cancelled, nobody has clocked in, and no recap has been
    filed;
  * the new day is not before today on the ops calendar (``_ops_today``, not
    the UTC ``localdate``) and the new start has not already passed.

Times are read as wall-clock at the venue (DST-aware, ``utils.tz``) and the
rows still go through ``_localize_model_times_to_own_tz`` before saving, the
same safety net as every other request/event write path.

When a non-admin moves an event (here, or by changing the date on a request
through ``updateRequest``), Ignite gets one email at the request CC list
(``_get_request_cc_emails``). Admin moves send nothing new. Booked
ambassadors are counted in that email but never named and never messaged
from here; Ignite confirms with them.
"""

from __future__ import annotations

import datetime as _dt
import logging
import uuid as _uuid
from dataclasses import dataclass, field
from html import escape as _esc
from typing import Any
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction

from events import models

logger = logging.getLogger(__name__)


class RescheduleError(Exception):
    """A refusal the caller can read: shown to the user as-is."""


@dataclass
class RescheduleOutcome:
    event: models.Event
    request: models.Request | None
    zone: ZoneInfo
    old_start: _dt.datetime | None
    old_end: _dt.datetime | None
    new_start: _dt.datetime
    new_end: _dt.datetime
    booked_count: int
    request_moved: bool
    notified: bool = False
    other_event_ids: list[int] = field(default_factory=list)


# ── Parsing ──────────────────────────────────────────────────────────────


def parse_day(value: str) -> _dt.date:
    raw = (value or "").strip()[:10]
    try:
        return _dt.date.fromisoformat(raw)
    except ValueError as exc:
        raise RescheduleError("Pick a date.") from exc


def parse_clock(value: str, label: str) -> _dt.time:
    raw = (value or "").strip().upper()
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p"):
        try:
            return _dt.datetime.strptime(raw, fmt).time()
        except ValueError:
            continue
    raise RescheduleError(f"Pick a {label} time.")


# ── Zones and labels ─────────────────────────────────────────────────────


def zone_for(obj) -> ZoneInfo:
    """The venue's zone for an event or request (ops zone when unknown)."""
    from events.venue_day import venue_zone

    return venue_zone(obj)


def local_window(start, end, zone: ZoneInfo):
    """(day, "HH:MM", "HH:MM") at the venue, for comparing two schedules."""
    if start is None:
        return None
    s = _aware(start).astimezone(zone)
    e = _aware(end).astimezone(zone) if end is not None else None
    return (s.date(), s.strftime("%H:%M"), e.strftime("%H:%M") if e else "")


def _aware(value: _dt.datetime) -> _dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=_dt.timezone.utc)
    return value


def _clock(value: _dt.datetime) -> str:
    return value.strftime("%-I:%M %p")


def window_label(start, end, zone: ZoneInfo, *, with_zone: bool = False) -> str:
    """"Thu Nov 20, 10:00 AM–2:00 PM" in the venue's zone."""
    if start is None:
        return "no date set"
    s = _aware(start).astimezone(zone)
    label = f"{s.strftime('%a %b %-d')}, {_clock(s)}"
    if end is not None:
        e = _aware(end).astimezone(zone)
        if e.date() != s.date():
            label += f"–{e.strftime('%a %b %-d')}, {_clock(e)}"
        else:
            label += f"–{_clock(e)}"
    if with_zone:
        abbr = s.strftime("%Z")
        if abbr:
            label += f" {abbr}"
    return label


# ── Access ───────────────────────────────────────────────────────────────


def _is_member(user, tenant_id: int) -> bool:
    from tenants.models import TenantedUser

    return TenantedUser.objects.filter(
        user_id=getattr(user, "id", None), tenant_id=tenant_id, is_active=True
    ).exists()


def _role_slug(user) -> str:
    from tenants.models import Role

    role_id = getattr(user, "role_id", None)
    if not role_id:
        return ""
    return (
        Role.objects.filter(id=role_id).values_list("slug", flat=True).first() or ""
    ).strip().lower()


def _is_ambassador(user) -> bool:
    from tenants.models import Role
    from utils.utils import ROLE_ID

    if getattr(user, "role_id", None) == ROLE_ID.Ambassadors:
        return True
    return _role_slug(user) == Role.AMBASSADOR_SLUG


def load_event(raw_id) -> models.Event | None:
    """Event by uuid, Relay id or numeric id."""
    from graphql import GraphQLError

    from utils.graphql.mixins import resolve_id_to_int

    qs = models.Event.objects.select_related(
        "tenant", "retailer", "timezone", "status", "request", "request__status",
        "request__timezone",
    )
    text = str(raw_id or "").strip()
    if not text:
        return None
    try:
        return qs.filter(uuid=_uuid.UUID(text)).first()
    except ValueError:
        pass
    try:
        pk = resolve_id_to_int(text)
    except (TypeError, ValueError, GraphQLError):
        return None
    return qs.filter(id=pk).first()


# ── The move ─────────────────────────────────────────────────────────────


def _has_clock_in(event_id: int) -> bool:
    from ambassadors.models import Attendance

    return Attendance.objects.filter(event_id=event_id).exists()


def _has_recap(event_id: int) -> bool:
    from recaps.models import CustomRecap, Recap

    return (
        CustomRecap.objects.filter(event_id=event_id).exists()
        or Recap.objects.filter(event_id=event_id).exists()
    )


def booked_ambassador_count(event_id: int) -> int:
    from ambassadors.models import AmbassadorEvent

    return AmbassadorEvent.objects.filter(event_id=event_id, is_approved=True).count()


def _primary_events_for_request(request_id: int) -> list[int]:
    """The request's own events, not co-brand copies."""
    return list(
        models.Event.objects.filter(
            request_id=request_id, co_activation_host__isnull=True
        ).values_list("id", flat=True)
    )


def reschedule_event_sync(
    *,
    event_id,
    actor,
    is_admin: bool,
    date: str,
    start_time: str,
    end_time: str,
    now: _dt.datetime | None = None,
) -> RescheduleOutcome:
    """Validate and move one event (+ its request). Raises RescheduleError."""
    from django.utils import timezone as dj_tz

    from events.cancelled import event_is_called_off
    from events.live_board import _ops_today
    from events.mutations import _localize_model_times_to_own_tz

    event = load_event(event_id)
    not_found = RescheduleError("Event not found.")
    if event is None:
        raise not_found
    if not is_admin:
        if _is_ambassador(actor) or not _is_member(actor, event.tenant_id):
            raise not_found
    if event.co_activation_host_id:
        raise RescheduleError(
            "This is a partner brand's copy of the event. Ask Ignite to move it."
        )
    if event_is_called_off(event):
        raise RescheduleError("This event was cancelled, so its date can't be changed.")
    if _has_clock_in(event.id):
        raise RescheduleError(
            "An ambassador has already clocked in to this event, so its date "
            "can't be changed here. Contact Ignite if it needs to move."
        )
    if _has_recap(event.id):
        raise RescheduleError(
            "A recap has already been submitted for this event, so its date "
            "can't be changed here. Contact Ignite if it needs to move."
        )

    day = parse_day(date)
    start_clock = parse_clock(start_time, "start")
    end_clock = parse_clock(end_time, "end")
    if end_clock <= start_clock:
        raise RescheduleError("The end time has to be after the start time.")

    zone = zone_for(event)
    new_start = _dt.datetime.combine(day, start_clock, tzinfo=zone)
    new_end = _dt.datetime.combine(day, end_clock, tzinfo=zone)
    new_midnight = _dt.datetime.combine(day, _dt.time(0, 0), tzinfo=zone)

    if day < _ops_today():
        raise RescheduleError("Pick today or a later date.")
    if new_start <= (now or dj_tz.now()):
        raise RescheduleError("That start time has already passed. Pick a later time.")

    old_start, old_end = event.start_time, event.end_time
    if local_window(old_start, old_end, zone) == local_window(new_start, new_end, zone):
        raise RescheduleError("That's already the scheduled date and time.")

    request = event.request if event.request_id else None
    request_moved = False
    other_event_ids: list[int] = []

    with transaction.atomic():
        event.date = new_midnight
        event.start_time = new_start
        event.end_time = new_end
        fields = ["date", "start_time", "end_time", "updated_at"]
        if event.new_end_time is not None:
            # An extension was approved for the old day; it doesn't carry over.
            event.new_end_time = None
            fields.append("new_end_time")
        if getattr(actor, "id", None):
            event.updated_by = actor
            fields.append("updated_by")
        _localize_model_times_to_own_tz(event)
        event.save(update_fields=fields)

        # Co-brand copies hold only that brand's recap slice; they happen at
        # the same time as the host event.
        models.Event.objects.filter(co_activation_host_id=event.id).update(
            date=event.date, start_time=event.start_time, end_time=event.end_time
        )

        if request is not None:
            siblings = [
                pk for pk in _primary_events_for_request(request.id) if pk != event.id
            ]
            if siblings:
                # A multi-visit request: the request row can't hold every
                # visit's date, so leave it and say so in the email.
                other_event_ids = siblings
            else:
                request.date = new_midnight
                request.start_time = new_start
                request.end_time = new_end
                req_fields = ["date", "start_time", "end_time", "updated_at"]
                if getattr(actor, "id", None):
                    request.updated_by = actor
                    req_fields.append("updated_by")
                _localize_model_times_to_own_tz(request)
                request.save(update_fields=req_fields)
                request_moved = True

            old_label = window_label(old_start, old_end, zone, with_zone=True)
            new_label = window_label(new_start, new_end, zone, with_zone=True)
            try:
                models.RequestActivityLog.objects.create(
                    tenant_id=request.tenant_id,
                    request=request,
                    kind=models.RequestActivityLog.KIND_UPDATED,
                    actor_user=actor if getattr(actor, "id", None) else None,
                    summary=f"Date changed: {old_label} → {new_label}"[:512],
                    metadata={
                        "fields": ["date", "start_time", "end_time"],
                        "source": "reschedule_event",
                        "event_id": event.id,
                        "by_client": not is_admin,
                        "from": {
                            "start": old_start.isoformat() if old_start else None,
                            "end": old_end.isoformat() if old_end else None,
                        },
                        "to": {"start": new_start.isoformat(), "end": new_end.isoformat()},
                        "request_moved": request_moved,
                    },
                )
            except Exception:
                logger.exception("reschedule activity log failed for event %s", event.id)

    return RescheduleOutcome(
        event=event,
        request=request,
        zone=zone,
        old_start=old_start,
        old_end=old_end,
        new_start=new_start,
        new_end=new_end,
        booked_count=booked_ambassador_count(event.id),
        request_moved=request_moved,
        other_event_ids=other_event_ids,
    )


# ── updateRequest: client date changes ───────────────────────────────────


def _parse_input_instant(value, zone: ZoneInfo):
    """An updateRequest time string as an aware datetime (naive = venue wall clock)."""
    if value in (None, ""):
        return None
    if isinstance(value, _dt.datetime):
        parsed = value
    else:
        try:
            parsed = _dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed


def request_input_window(input_obj, zone: ZoneInfo):
    start = _parse_input_instant(getattr(input_obj, "start_time", None), zone)
    end = _parse_input_instant(getattr(input_obj, "end_time", None), zone)
    return local_window(start, end, zone)


def request_zone(request=None, timezone_id=None) -> ZoneInfo:
    """Zone for a request, optionally for a different TimeZone row id."""
    from utils.tz import resolve_zoneinfo

    from events.venue_day import OPS_TZ

    tz_row = None
    if timezone_id:
        tz_row = models.TimeZone.objects.filter(id=timezone_id).first()
    if tz_row is None and request is not None and getattr(request, "timezone_id", None):
        tz_row = models.TimeZone.objects.filter(id=request.timezone_id).first()
    return (resolve_zoneinfo(tz_row) if tz_row is not None else None) or ZoneInfo(OPS_TZ)


def live_events_for_request(request_id: int) -> list[int]:
    """Request's own events that are still on (not cancelled)."""
    from events.cancelled import CANCELLED_EVENT_SLUGS

    return list(
        models.Event.objects.filter(request_id=request_id, co_activation_host__isnull=True)
        .exclude(status__slug__in=CANCELLED_EVENT_SLUGS)
        .values_list("id", flat=True)
    )


@dataclass
class RequestDateSnapshot:
    zone: ZoneInfo
    old_start: _dt.datetime | None
    old_end: _dt.datetime | None
    changed: bool


def check_client_request_date_change(*, request_id: int, actor, input_obj):
    """Before a non-admin updateRequest saves: did the date/times change?

    Returns None when the caller isn't a member of the request's tenant
    (the update itself then refuses with "not found", so nothing leaks).
    Raises RescheduleError when the client moved a request that already has
    live events: those must move through Change date on the event so the
    event, the request and the booked team stay in sync.
    """
    request = (
        models.Request.objects.filter(id=request_id)
        .only("id", "tenant_id", "timezone_id", "start_time", "end_time")
        .first()
    )
    if request is None or not _is_member(actor, request.tenant_id):
        return None
    old_zone = request_zone(request)
    new_tz_id = getattr(input_obj, "timezone_id", None)
    try:
        from utils.graphql.mixins import resolve_id_to_int

        new_tz_pk = resolve_id_to_int(new_tz_id) if new_tz_id else None
    except Exception:
        new_tz_pk = None
    new_zone = request_zone(request, new_tz_pk)
    before = local_window(request.start_time, request.end_time, old_zone)
    after = request_input_window(input_obj, new_zone)
    changed = after is not None and before != after
    if changed and live_events_for_request(request.id):
        raise RescheduleError(
            "This request is already on the schedule. Open the event and use "
            "“Change date” to move it, so the visit and the booked team stay in sync."
        )
    return RequestDateSnapshot(
        zone=old_zone,
        old_start=request.start_time,
        old_end=request.end_time,
        changed=changed,
    )


def notify_after_client_request_update(*, snapshot: RequestDateSnapshot, request_id: int, actor) -> bool:
    """After the save: email Ignite if the saved request's window moved."""
    request = (
        models.Request.objects.select_related("tenant", "retailer", "timezone")
        .filter(id=request_id)
        .first()
    )
    if request is None or not snapshot.changed:
        return False
    zone = request_zone(request)
    if local_window(snapshot.old_start, snapshot.old_end, zone) == local_window(
        request.start_time, request.end_time, zone
    ):
        return False
    sent = notify_admins_of_client_reschedule(
        actor=actor,
        tenant=request.tenant,
        zone=zone,
        old_start=snapshot.old_start,
        old_end=snapshot.old_end,
        new_start=request.start_time,
        new_end=request.end_time,
        request=request,
        source="request",
    )
    try:
        models.RequestActivityLog.objects.create(
            tenant_id=request.tenant_id,
            request=request,
            kind=models.RequestActivityLog.KIND_UPDATED,
            actor_user=actor if getattr(actor, "id", None) else None,
            summary=(
                "Date changed: "
                f"{window_label(snapshot.old_start, snapshot.old_end, zone, with_zone=True)}"
                f" → {window_label(request.start_time, request.end_time, zone, with_zone=True)}"
            )[:512],
            metadata={
                "fields": ["date", "start_time", "end_time"],
                "source": "update_request",
                "by_client": True,
            },
        )
    except Exception:
        logger.exception("request date-change activity log failed for %s", request_id)
    return sent


# ── Email ────────────────────────────────────────────────────────────────


def _admin_base_url() -> str:
    return str(
        getattr(settings, "ADMIN_FRONTEND_URL", "https://admin.igniteproductions.co")
    ).rstrip("/")


def _actor_label(user: Any) -> tuple[str, str]:
    email = (getattr(user, "email", None) or "").strip()
    try:
        full = (user.get_full_name() or "").strip()
    except Exception:
        full = ""
    if full and email and full.lower() != email.lower():
        return f"{full} ({email})", email
    return full or email or "Someone", email


def _store_label(event=None, request=None) -> str:
    for obj in (event, request):
        if obj is None:
            continue
        retailer = getattr(obj, "retailer", None) if getattr(obj, "retailer_id", None) else None
        name = (getattr(retailer, "name", None) or "").strip()
        if name:
            return name
    for obj in (event, request):
        if obj is None:
            continue
        name = (getattr(obj, "retailer_name", None) or "").strip()
        if name:
            return name
    return "—"


def notify_admins_of_client_reschedule(
    *,
    actor: Any,
    tenant: Any,
    zone: ZoneInfo,
    old_start,
    old_end,
    new_start,
    new_end,
    event=None,
    request=None,
    booked_count: int = 0,
    source: str = "event",
    unmoved_events: int = 0,
) -> bool:
    """Email the request CC list that a brand moved a visit. Never raises.

    ``source`` is "event" (Change date on the event page) or "request"
    (the date on a request edit). Returns True if an email was sent.
    """
    try:
        from events.mutations import _get_request_cc_emails
        from utils.mailer import Envelope, Mailer

        recipients = _get_request_cc_emails(tenant=tenant)
        if not recipients:
            return False

        brand = (getattr(tenant, "name", None) or "").strip() or "A brand"
        subject_obj = event if event is not None else request
        title = (getattr(subject_obj, "name", None) or "").strip() or _store_label(
            event, request
        )
        old_label = window_label(old_start, old_end, zone)
        new_label = window_label(new_start, new_end, zone)
        subject = f"{brand} moved {title}: {old_label} → {new_label}"[:200]

        who, actor_email = _actor_label(actor)
        store = _store_label(event, request)
        address = (getattr(subject_obj, "address", None) or "").strip() or "—"
        base = _admin_base_url()
        links: list[str] = []
        if event is not None and getattr(event, "uuid", None):
            links.append(
                f'<a href="{_esc(f"{base}/event/view/{event.uuid}")}">Open the event in Spark</a>'
            )
        if request is not None and getattr(request, "uuid", None):
            links.append(
                f'<a href="{_esc(f"{base}/request/view/{request.uuid}")}">Open the request</a>'
            )

        if booked_count > 0:
            noun = "ambassador" if booked_count == 1 else "ambassadors"
            staffing = (
                f"<p style='padding:10px 12px;background:#fff7e6;border-radius:6px'>"
                f"<strong>{booked_count} {noun} booked — confirm they can make the "
                f"new time.</strong> Spark has not messaged them about this change.</p>"
            )
        else:
            staffing = "<p>No ambassadors are booked yet.</p>"
        extra = ""
        if unmoved_events > 0:
            noun = "event is" if unmoved_events == 1 else "events are"
            extra = (
                f"<p><strong>{unmoved_events} linked {noun} still on the old date.</strong> "
                "Move them to match, or ask the brand which date is right.</p>"
            )

        via = "the event page" if source == "event" else "the request edit form"

        def row(label: str, value: str) -> str:
            return (
                f"<tr><td style='padding:3px 14px 3px 0;color:#666'>{_esc(label)}</td>"
                f"<td>{value}</td></tr>"
            )

        html = (
            "<div style='font-family:sans-serif;font-size:14px;line-height:1.45'>"
            f"<p><strong>{_esc(brand)}</strong> changed the date of "
            f"<strong>{_esc(title)}</strong> from {_esc(via)}.</p>"
            "<table style='border-collapse:collapse;margin:12px 0'>"
            + row("Was", f"<s>{_esc(window_label(old_start, old_end, zone, with_zone=True))}</s>")
            + row("Now", f"<strong>{_esc(window_label(new_start, new_end, zone, with_zone=True))}</strong>")
            + row("Store", _esc(store))
            + row("Address", _esc(address))
            + row("Brand", _esc(brand))
            + row("Changed by", _esc(who))
            + "</table>"
            + staffing
            + extra
            + (f"<p>{' · '.join(links)}</p>" if links else "")
            + "</div>"
        )

        class _RescheduleMailer(Mailer):
            def envelope(self) -> Envelope:
                return Envelope(
                    subject=subject,
                    html=html,
                    to_emails=recipients,
                    headers={"Reply-To": actor_email or "staffing@igniteproductions.co"},
                )

        _RescheduleMailer().send_now()
        return True
    except Exception:
        logger.exception("client reschedule notify failed")
        return False
