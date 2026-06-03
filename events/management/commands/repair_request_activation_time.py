"""
Repair a single request's activation start/end time when it was mis-captured
(e.g. an AM/PM mix-up: REQ-1072's Encinitas activation stored as 3:00 AM
instead of 3:00 PM).

Sets the LOCAL wall-clock start/end for the request and stores the correct
UTC, resolving the activation's timezone the SAME way the approval email does
(request.timezone → state → parsed from the address). DRY-RUN by default —
prints the before/after; pass --execute to write. Targets exactly ONE request
(``--request <id>``) so it can never rewrite times in bulk by accident.

Usage:
    # preview REQ-1072 → 3:00 PM – 6:00 PM local
    python manage.py repair_request_activation_time --request 1072 \
        --start-local 15:00 --end-local 18:00
    # apply
    python manage.py repair_request_activation_time --request 1072 \
        --start-local 15:00 --end-local 18:00 --execute
"""

from __future__ import annotations

import datetime as dt
import logging

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone as djtz

from events.models import Request
from events.routing import extract_state_code
from utils.tz import offset_minutes_for, offset_minutes_for_state

logger = logging.getLogger(__name__)


def _resolve_offset_minutes(request) -> int:
    """Effective UTC offset (minutes) for the request, DST-aware, mirroring
    the approval-email resolution: explicit TimeZone row → state → address."""
    when = getattr(request, "start_time", None) or getattr(request, "date", None)
    tz_row = request.timezone if getattr(request, "timezone_id", None) else None
    if tz_row is not None:
        return offset_minutes_for(tz_row, at=when)

    state_code = None
    for getter in (
        lambda: request.state.code,
        lambda: request.retailer.location.state.code,
        lambda: extract_state_code(getattr(request, "address", None)),
    ):
        try:
            state_code = getter()
        except Exception:
            state_code = None
        if state_code:
            break
    if state_code:
        off = offset_minutes_for_state(str(state_code).strip().upper(), at=when)
        if off is not None:
            return off
    return 0


def _parse_hhmm(label: str, value: str) -> tuple[int, int]:
    try:
        h, m = value.strip().split(":")
        h, m = int(h), int(m)
        assert 0 <= h <= 23 and 0 <= m <= 59
        return h, m
    except Exception:
        raise CommandError(f"--{label} must be 24h HH:MM (got {value!r}).")


class Command(BaseCommand):
    help = (
        "Set a single request's LOCAL activation start/end time (storing the "
        "correct UTC). DRY-RUN by default; pass --execute. Requires --request, "
        "--start-local HH:MM, --end-local HH:MM."
    )

    def add_arguments(self, parser):
        parser.add_argument("--request", type=int, required=True)
        parser.add_argument("--start-local", type=str, required=True)
        parser.add_argument("--end-local", type=str, required=True)
        parser.add_argument("--execute", action="store_true", default=False)

    def handle(self, *args, **opts):
        execute = bool(opts.get("execute"))
        req_id = opts["request"]
        sh, sm = _parse_hhmm("start-local", opts["start_local"])
        eh, em = _parse_hhmm("end-local", opts["end_local"])

        request = (
            Request.objects.select_related(
                "timezone", "state", "retailer__location__state", "tenant"
            )
            .filter(id=req_id)
            .first()
        )
        if request is None:
            raise CommandError(f"No request with id={req_id}.")

        offset_min = _resolve_offset_minutes(request)
        off_delta = dt.timedelta(minutes=offset_min)

        # Local wall-clock date of the activation (from the existing start).
        base_utc = request.start_time or request.date
        if base_utc is None:
            raise CommandError(f"Request {req_id} has no start_time/date to anchor the date.")
        if djtz.is_naive(base_utc):
            base_utc = djtz.make_aware(base_utc, dt.timezone.utc)
        local_date = (base_utc + off_delta).date()

        # New local naive datetimes → UTC (utc = local - offset).
        start_local = dt.datetime.combine(local_date, dt.time(sh, sm))
        end_local = dt.datetime.combine(local_date, dt.time(eh, em))
        if end_local <= start_local:
            end_local += dt.timedelta(days=1)  # overnight end → next day
        new_start = djtz.make_aware(start_local - off_delta, dt.timezone.utc)
        new_end = djtz.make_aware(end_local - off_delta, dt.timezone.utc)

        def _fmt_local(utc_dt):
            return (utc_dt + off_delta).strftime("%Y-%m-%d %I:%M %p").lstrip("0")

        self.stdout.write(
            f"REQ-{request.id} ({request.name}) — offset={offset_min}min"
        )
        self.stdout.write(
            f"  BEFORE: start={_fmt_local(base_utc)} (local)  [{base_utc.isoformat()} UTC]"
        )
        self.stdout.write(
            f"  AFTER:  start={_fmt_local(new_start)}  end={_fmt_local(new_end)} (local)"
        )
        self.stdout.write(
            f"          [{new_start.isoformat()} / {new_end.isoformat()} UTC]"
        )

        if not execute:
            self.stdout.write(self.style.WARNING("DRY RUN — no write. Re-run with --execute."))
            self.stdout.write(
                f"RESULT mode=dry-run request={request.id} "
                f"new_start_utc={new_start.isoformat()} new_end_utc={new_end.isoformat()}"
            )
            return None

        Request.objects.filter(pk=request.pk).update(
            start_time=new_start, end_time=new_end, updated_at=djtz.now()
        )
        # Re-sync the linked sheet with the corrected time (best-effort).
        try:
            from utils.sheets_mirror import upsert_request_row

            fresh = Request.objects.select_related("tenant", "timezone").get(pk=request.pk)
            upsert_request_row(fresh)
        except Exception as exc:  # noqa: BLE001
            logger.warning("sheet re-sync failed for request=%s: %s", request.id, exc)

        self.stdout.write(self.style.SUCCESS(f"Updated REQ-{request.id}."))
        self.stdout.write(
            f"RESULT mode=execute request={request.id} "
            f"new_start_utc={new_start.isoformat()} new_end_utc={new_end.isoformat()}"
        )
        return None
