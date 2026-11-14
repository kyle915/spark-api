"""Shift texts to ambassadors, sent from Ignite's Quo number.

Three texts, each sent at most once per ambassador per event:

* ``pre_shift``     — the shift starts within two hours. Carries the brand's
  check-in link, which is where they clock in, clock out and file the recap.
* ``recap_due``     — 30 minutes after they clocked out (or after the event
  ended, when they never clocked out) with no recap filed.
* ``recap_morning`` — the recap is still missing the next morning.

Who is on a shift comes from Spark's own data: an approved booking for the
pre-shift text, a real clock-in for the recap texts — so a walk-up who
clocked in from a link gets the recap reminders even though nobody booked
them. The brand, and therefore the link, comes from the event.

Nothing is sent unless ``SHIFT_SMS_ENABLED`` is on and Quo is configured;
``dry_run`` reports who would be texted, and what, without sending or
recording anything. Every send is reserved in ``ShiftSms`` first, so
overlapping cron runs cannot double-text.

A send that fails for a reason that looks temporary (Quo unreachable, a 5xx
or a 429) is tried again on a later run, up to ``MAX_SEND_ATTEMPTS`` in all,
and only while the text is still due. Anything else — a 4xx such as a
rejected number or no credits, or a timeout where Quo may already have taken
the message — is final.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import IntegrityError
from django.db.models import F, Q
from django.utils import timezone

logger = logging.getLogger(__name__)

PRE_SHIFT_LEAD = timedelta(hours=2)
RECAP_GRACE = timedelta(minutes=30)
# Past this, the 30-minute text would no longer be "just after the shift";
# the next-morning text covers it instead.
RECAP_DUE_MAX_AGE = timedelta(hours=12)
RECAP_MORNING_MAX_AGE = timedelta(hours=48)
MORNING_HOURS = range(9, 18)
# Only used to decide "is it morning there" when an event has no time zone.
_FALLBACK_TZ = "America/Chicago"

_OPT_OUT = "Reply STOP to opt out."

# First try plus retries, for one (kind, ambassador, event).
MAX_SEND_ATTEMPTS = 3


class QuoSendError(RuntimeError):
    """Quo answered with an HTTP error. ``status`` is the HTTP status code."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


def is_transient_send_error(exc: BaseException) -> bool:
    """Worth trying again later? Only when the provider did not take the
    message: it could not be reached at all, it was overloaded (5xx), or it
    asked us to slow down (429).

    Any other 4xx is Quo saying no to this message. A timeout, or a
    connection that dropped while we waited for the answer, is NOT retried:
    Quo may already have accepted the text, and sending it again would text
    the same person twice. An error we do not recognise is final too —
    never re-text on a guess."""
    if isinstance(exc, QuoSendError):
        return exc.status == 429 or (exc.status is not None and 500 <= exc.status < 600)
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or 500 <= exc.code < 600
    if isinstance(exc, urllib.error.URLError):
        # urllib wraps failures to CONNECT (refused, DNS, unreachable) in
        # URLError — nothing was sent. A connect timeout is the exception.
        return not isinstance(exc.reason, TimeoutError)
    return False


def normalize_us_phone(raw: str | None) -> str | None:
    """E.164 for a US/Canada number, or None when it is not one."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] in "01":
        return None
    return f"+1{digits}"


def sms_configured() -> bool:
    return bool(
        getattr(settings, "SHIFT_SMS_ENABLED", False)
        and (getattr(settings, "QUO_API_KEY", "") or "").strip()
        and (getattr(settings, "QUO_FROM_NUMBER", "") or "").strip()
    )


def send_quo_sms(*, to: str, body: str) -> str:
    """Send one text through Quo. Returns Quo's message id.

    ``setInboxStatus: done`` files the conversation under Done, so automated
    texts do not pile up as open threads in the team's shared inbox. A reply
    reopens it like any other conversation.
    """
    base = (getattr(settings, "QUO_API_BASE_URL", "") or "https://api.quo.com").rstrip("/")
    payload = json.dumps(
        {
            "content": body,
            "from": settings.QUO_FROM_NUMBER.strip(),
            "to": [to],
            "setInboxStatus": "done",
        }
    ).encode()
    request = urllib.request.Request(
        f"{base}/v1/messages",
        data=payload,
        method="POST",
        headers={
            "Authorization": settings.QUO_API_KEY.strip(),
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise QuoSendError(f"Quo returned {exc.code}: {detail}", status=exc.code) from exc
    return str((data.get("data") or {}).get("id") or "")


def _venue(event) -> str:
    """The place, without the date prefix and type suffix of generated names
    ("10/30/2026 - Hollywood Bowl · Event Activation" → "Hollywood Bowl")."""
    name = (getattr(event, "name", None) or "").strip()
    name = re.sub(r"^\d{1,2}/\d{1,2}(/\d{2,4})?\s*-\s*", "", name)
    name = name.split(" · ")[0].strip()
    return (name or "your shift")[:60]


def _first_name(ambassador) -> str:
    user = getattr(ambassador, "user", None)
    return ((getattr(user, "first_name", "") or "").strip().split(" ")[0]) or "there"


def _event_zone(event) -> ZoneInfo | None:
    from utils.tz import resolve_zoneinfo

    return resolve_zoneinfo(getattr(event, "timezone", None))


def _clock(dt, zone: ZoneInfo) -> str:
    local = dt.astimezone(zone)
    hour = local.hour % 12 or 12
    suffix = "AM" if local.hour < 12 else "PM"
    return f"{hour} {suffix}" if local.minute == 0 else f"{hour}:{local.minute:02d} {suffix}"


def message_for(*, kind: str, ambassador, event, url: str) -> str:
    from ambassadors.models import ShiftSms

    first = _first_name(ambassador)
    brand = (getattr(getattr(event, "tenant", None), "name", "") or "your").strip()
    venue = _venue(event)
    if kind == ShiftSms.KIND_PRE_SHIFT:
        zone = _event_zone(event)
        start = getattr(event, "start_time", None)
        # Without a time zone the hour would be a guess, so don't print one.
        when = f"starts at {_clock(start, zone)}" if zone and start else "starts soon"
        return (
            f"Ignite: Hi {first}, your {brand} shift at {venue} {when}. "
            f"Clock in and out and file your recap here: {url} {_OPT_OUT}"
        )
    if kind == ShiftSms.KIND_RECAP_DUE:
        return (
            f"Ignite: Hi {first}, thanks for working {brand} at {venue}. "
            f"Your recap isn't in yet. File it here: {url} {_OPT_OUT}"
        )
    return (
        f"Ignite: Hi {first}, your {brand} recap for {venue} is still "
        f"missing. Please file it today: {url} {_OPT_OUT}"
    )


def events_from():
    """Events starting before this are never texted about (None = no floor)."""
    from django.utils.dateparse import parse_datetime

    raw = (getattr(settings, "SHIFT_SMS_EVENTS_FROM", "") or "").strip()
    floor = parse_datetime(raw) if raw else None
    if floor is not None and timezone.is_naive(floor):
        floor = timezone.make_aware(floor)
    return floor


def _live_event_q(prefix: str = "event__") -> Q:
    """Events whose request was not deleted (standalone events always pass)."""
    return Q(**{f"{prefix}request__isnull": True}) | Q(
        **{f"{prefix}request__deleted_at__isnull": True}
    )


# What events/demo_cancel.py moves a cancelled demo to: the request to the
# tenant's declined/cancelled status, each linked event to its
# suspended/declined/cancelled one.
_CANCELLED_REQUEST_SLUGS = ("declined", "cancelled", "canceled")
_CANCELLED_EVENT_SLUGS = ("suspended", "declined", "cancelled", "canceled")


def _not_cancelled_q(prefix: str = "event__") -> Q:
    """Events that were not cancelled, directly or through their request."""
    return ~Q(**{f"{prefix}status__slug__in": _CANCELLED_EVENT_SLUGS}) & ~Q(
        **{f"{prefix}request__status__slug__in": _CANCELLED_REQUEST_SLUGS}
    )


def _pre_shift_candidates(now):
    from ambassadors.checkin_web import clock_state
    from ambassadors.models import AmbassadorEvent, ShiftSms

    rosters = (
        AmbassadorEvent.objects.select_related(
            "event", "event__tenant", "event__timezone", "ambassador", "ambassador__user"
        )
        .filter(
            _live_event_q(),
            _not_cancelled_q(),
            is_approved=True,
            event__start_time__gt=now,
            event__start_time__lte=now + PRE_SHIFT_LEAD,
        )
        .order_by("event__start_time")
    )
    floor = events_from()
    for roster in rosters:
        if floor is not None and roster.event.start_time < floor:
            continue
        state = clock_state(ambassador_id=roster.ambassador_id, event_id=roster.event_id)
        if state.get("state") != "not_started":
            continue  # already there
        yield ShiftSms.KIND_PRE_SHIFT, roster.ambassador, roster.event


def _recap_candidates(now):
    """(kind, ambassador, event) for everyone who clocked in, has finished,
    and has no recap on file."""
    from ambassadors.checkin_web import clock_state
    from ambassadors.models import Ambassador, Attendance, ShiftSms
    from events.models import Event
    from recaps.filed import has_filed_recap

    pairs = list(
        Attendance.objects.filter(
            _live_event_q(),
            _not_cancelled_q(),
            source__name="clock_in",
            clock_time__gte=now - RECAP_MORNING_MAX_AGE - timedelta(hours=12),
            ambassador__isnull=False,
            event__isnull=False,
        )
        .values_list("ambassador_id", "event_id")
        .distinct()
    )
    if not pairs:
        return
    events = {
        e.id: e
        for e in Event.objects.select_related("tenant", "timezone").filter(
            id__in={event_id for _, event_id in pairs}
        )
    }
    ambassadors = {
        a.id: a
        for a in Ambassador.objects.select_related("user").filter(
            id__in={ambassador_id for ambassador_id, _ in pairs}
        )
    }

    floor = events_from()
    for ambassador_id, event_id in pairs:
        event = events.get(event_id)
        ambassador = ambassadors.get(ambassador_id)
        if event is None or ambassador is None:
            continue
        state = clock_state(ambassador_id=ambassador_id, event_id=event_id)
        if floor is not None:
            # A walk-up event may carry no start time: its first clock-in is
            # when it began.
            from django.utils.dateparse import parse_datetime as _parse

            began = getattr(event, "start_time", None) or (
                _parse(state["clockInAt"]) if state.get("clockInAt") else None
            )
            if began is None or began < floor:
                continue
        if state.get("state") == "clocked_out" and state.get("clockOutAt"):
            from django.utils.dateparse import parse_datetime

            finished = parse_datetime(state["clockOutAt"])
        else:
            # Still on the clock: only "finished" once the event itself ended.
            finished = getattr(event, "end_time", None)
            if finished is None or finished > now:
                continue
        if finished is None or finished > now - RECAP_GRACE:
            continue
        age = now - finished
        if age <= RECAP_DUE_MAX_AGE:
            kind = ShiftSms.KIND_RECAP_DUE
        elif age <= RECAP_MORNING_MAX_AGE:
            zone = _event_zone(event) or ZoneInfo(_FALLBACK_TZ)
            local_now = now.astimezone(zone)
            if local_now.hour not in MORNING_HOURS:
                continue
            if local_now.date() <= finished.astimezone(zone).date():
                continue
            kind = ShiftSms.KIND_RECAP_MORNING
        else:
            continue
        if has_filed_recap(ambassador_id=ambassador_id, event_id=event_id):
            continue
        yield kind, ambassador, event


def send_shift_sms(*, now=None, dry_run: bool = False, send=None) -> dict:
    """Send every shift text that is due. Returns counts and, for a dry run,
    one line per text that would go out (first name and last four digits
    only — the log is read by people)."""
    from ambassadors.draft_reminders import standing_checkin_url
    from ambassadors.models import ShiftSms

    now = now or timezone.now()
    send = send or send_quo_sms
    counts = {
        "sent": 0,
        "would_send": 0,
        "already_sent": 0,
        "skipped_opt_out": 0,
        "skipped_no_phone": 0,
        "skipped_no_link": 0,
        "failed": 0,
        # Of ``failed``: temporary failures a later run will try again.
        "will_retry": 0,
    }
    preview: list[str] = []
    live = sms_configured()
    if not dry_run and not live:
        return {**counts, "enabled": False, "preview": preview}

    candidates = [*_pre_shift_candidates(now), *_recap_candidates(now)]
    for kind, ambassador, event in candidates:
        existing = ShiftSms.objects.filter(
            kind=kind, ambassador_id=ambassador.id, event_id=event.id
        ).first()
        # Only a send that failed for a temporary reason, with attempts left,
        # is picked up again. Sent, pending and finally-failed rows are done.
        retry_row = (
            existing
            if existing is not None
            and existing.status == ShiftSms.STATUS_RETRY
            and existing.attempts < MAX_SEND_ATTEMPTS
            else None
        )
        if existing is not None and retry_row is None:
            counts["already_sent"] += 1
            continue
        if getattr(ambassador, "sms_opt_out", False):
            counts["skipped_opt_out"] += 1
            continue
        to = normalize_us_phone(getattr(ambassador, "phone", None))
        if not to:
            counts["skipped_no_phone"] += 1
            continue
        url = standing_checkin_url(event)
        if not url:
            counts["skipped_no_link"] += 1
            continue
        body = message_for(kind=kind, ambassador=ambassador, event=event, url=url)

        if dry_run:
            counts["would_send"] += 1
            preview.append(f"{kind} → {_first_name(ambassador)} (…{to[-4:]}): {body}")
            continue

        if retry_row is not None:
            # Claim the retry atomically: the row goes back to pending with
            # one more attempt counted, and only if no other run got there
            # first — so an overlapping run cannot send it a second time.
            claimed = ShiftSms.objects.filter(
                id=retry_row.id,
                status=ShiftSms.STATUS_RETRY,
                attempts=retry_row.attempts,
            ).update(
                status=ShiftSms.STATUS_PENDING,
                attempts=F("attempts") + 1,
                to_number=to,
                body=body,
            )
            if not claimed:
                counts["already_sent"] += 1
                continue
            row = retry_row
            attempts = retry_row.attempts + 1
        else:
            try:
                row = ShiftSms.objects.create(
                    kind=kind,
                    ambassador=ambassador,
                    event=event,
                    tenant_id=event.tenant_id,
                    to_number=to,
                    body=body,
                )
            except IntegrityError:
                counts["already_sent"] += 1  # another run reserved it first
                continue
            attempts = 1
        try:
            provider_id = send(to=to, body=body)
        except Exception as exc:  # noqa: BLE001 — one number must not stop the run
            will_retry = (
                is_transient_send_error(exc) and attempts < MAX_SEND_ATTEMPTS
            )
            logger.warning(
                "shift sms failed kind=%s event=%s attempt=%s will_retry=%s",
                kind,
                event.id,
                attempts,
                will_retry,
                exc_info=True,
            )
            ShiftSms.objects.filter(id=row.id).update(
                status=(
                    ShiftSms.STATUS_RETRY if will_retry else ShiftSms.STATUS_FAILED
                ),
                error=str(exc)[:500],
            )
            counts["failed"] += 1
            if will_retry:
                counts["will_retry"] += 1
            continue
        ShiftSms.objects.filter(id=row.id).update(
            status=ShiftSms.STATUS_SENT,
            provider_id=provider_id or "",
            error="",
            sent_at=timezone.now(),
        )
        counts["sent"] += 1

    return {**counts, "enabled": live, "preview": preview}
