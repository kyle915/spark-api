"""Torch retail-schedule Sheet → Event Confirmation / cancel automation.

Apps Script checks **Send Confirmation** or **Cancel Confirmation** on a row;
this module maps the row onto Spark's existing Event Confirmation mailer
(email-only, no Event create) and stamps status back onto the sheet.

Column O ("Event Confirmation Sent?") stays a human-readable status stamp.
Trigger columns are separate checkboxes so unchecking Send is a no-op.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django.utils import timezone as dj_tz

from events.routing import _US_STATE_NAME_TO_CODE, extract_state_code
from events.torch_portal import is_torch_tenant
from utils.geocoding import photon_geocode_feature
from utils.torch_public_form_sheet import (
    TORCH_PUBLIC_FORM_GID,
    TORCH_PUBLIC_FORM_SHEET_ID,
    _col_letter,
    _parse_sheet_date,
    _qualify,
    _read_header,
    _service,
    _tab_for_gid,
)
from utils.tz import iana_for_us_state

logger = logging.getLogger(__name__)

TORCH_TENANT_SLUG = "keee-torch-thc"
DEFAULT_EVENT_TYPE_LABEL = "Retail Sampling"
DEFAULT_TIMEZONE_NAME = "America/Los_Angeles"

# Appended after existing headers — never rewrite A–AB.
CONFIRMATION_EXTRA_HEADERS = [
    "Send Confirmation",
    "Cancel Confirmation",
    "Force Resend",
    "Confirmation Status",
    "Confirmation Sent At",
    "Confirmation Error",
    "Spark Confirmation UUID",
]

STATUS_QUEUED = "Queued"
STATUS_SENT = "Sent"
STATUS_CANCELLED = "Cancelled"
STATUS_ERROR = "Error"

SENT_STATUS_HEADER = "Event Confirmation Sent?"

_TIME_RE = re.compile(
    r"""
    ^\s*
    (?P<hour>\d{1,2})
    (?:
        :(?P<minute>\d{2})
      | \.(?P<minute_dot>\d{2})
    )?
    \s*
    (?P<ampm>[ap])?\.?m?\.?
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


@dataclass
class SheetRowPayload:
    """Normalized fields from one Retail Schedule row (or Apps Script POST)."""

    row_number: int
    sheet_id: str = TORCH_PUBLIC_FORM_SHEET_ID
    date: str = ""
    start_time: str = ""
    end_time: str = ""
    store_name: str = ""
    address: str = ""
    state: str = ""
    skus: str = ""
    ba_name: str = ""
    ba_email: str = ""
    spark_request_uuid: str = ""
    confirmation_uuid: str = ""
    confirmation_status: str = ""
    sent_status: str = ""
    force_resend: bool = False
    dry_run: bool = False


@dataclass
class ActionResult:
    ok: bool
    action: str
    status: str
    message: str
    confirmation_uuid: str = ""
    timezone_name: str = ""
    timezone_note: str = ""
    dry_run: bool = False
    details: dict[str, Any] = field(default_factory=dict)


def parse_sheet_clock(raw: str | None) -> time:
    """Parse Torch sheet times like ``1p``, ``4p``, ``10:30a``, ``13:00``."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("Time is required")
    match = _TIME_RE.match(text)
    if not match:
        raise ValueError(f"Unrecognized time {raw!r}")
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or match.group("minute_dot") or 0)
    ampm = (match.group("ampm") or "").lower()
    if ampm:
        if hour < 1 or hour > 12:
            raise ValueError(f"Invalid 12h hour in {raw!r}")
        if ampm == "a":
            hour = 0 if hour == 12 else hour
        else:
            hour = 12 if hour == 12 else hour + 12
    else:
        if hour > 23:
            raise ValueError(f"Invalid 24h hour in {raw!r}")
    if minute > 59:
        raise ValueError(f"Invalid minutes in {raw!r}")
    return time(hour, minute)


def parse_sheet_date_iso(raw: str | None) -> date:
    """Parse Date cell → ``date``. Accepts the same formats as sheet ordering."""
    parsed = _parse_sheet_date(raw or "")
    if parsed is None:
        raise ValueError(f"Unrecognized date {raw!r}")
    return parsed


def products_from_skus_cell(raw: str | None) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return []
    parts = re.split(r"[,;\n]+", text)
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        name = part.strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def _state_code_from_hint(state_hint: str | None, address: str | None) -> str | None:
    hint = (state_hint or "").strip()
    if hint:
        upper = hint.upper()
        if len(upper) == 2 and iana_for_us_state(upper):
            return upper
        mapped = _US_STATE_NAME_TO_CODE.get(hint.lower())
        if mapped:
            return mapped
    return extract_state_code(address)


def resolve_timezone_for_row(
    address: str,
    state_hint: str = "",
    *,
    geocode=photon_geocode_feature,
) -> tuple[str, str]:
    """Return ``(iana, note)``.

    Prefer Photon geocode → state → IANA. Fall back to sheet State / address
    parse. Always returns a usable IANA name; ``note`` explains fallbacks.
    """
    notes: list[str] = []
    feature = None
    try:
        feature = geocode(address) if (address or "").strip() else None
    except Exception as exc:  # noqa: BLE001 — never block a send on geocode
        notes.append(f"geocode error: {exc}")
        feature = None

    if feature:
        photon_state = (feature.get("state") or "").strip()
        code = _state_code_from_hint(photon_state, address)
        iana = iana_for_us_state(code) if code else None
        if iana:
            return iana, ""
        notes.append(
            f"geocode ok but no IANA for state={photon_state!r}"
        )
    else:
        notes.append("geocode failed")

    code = _state_code_from_hint(state_hint, address)
    iana = iana_for_us_state(code) if code else None
    if iana:
        notes.append(f"fallback State→TZ ({code}→{iana})")
        return iana, "; ".join(notes)

    notes.append(f"defaulting to {DEFAULT_TIMEZONE_NAME}")
    return DEFAULT_TIMEZONE_NAME, "; ".join(notes)


def _norm_status(raw: str | None) -> str:
    return (raw or "").strip().lower()


def _already_sent(payload: SheetRowPayload) -> bool:
    status = _norm_status(payload.confirmation_status)
    sent = _norm_status(payload.sent_status)
    if status.startswith("sent") or status == STATUS_SENT.lower():
        return True
    if sent in {"y", "yes", "true", "1"} or sent.startswith("sent"):
        return True
    if payload.confirmation_uuid.strip():
        # UUID present usually means a prior successful send path completed.
        return status not in {
            STATUS_CANCELLED.lower(),
            STATUS_ERROR.lower(),
            STATUS_QUEUED.lower(),
            "",
        }
    return False


def _already_cancelled(payload: SheetRowPayload) -> bool:
    return _norm_status(payload.confirmation_status).startswith("cancelled")


def _parse_local_instant(day: date, clock: time, tz_name: str) -> datetime:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        tz = ZoneInfo(DEFAULT_TIMEZONE_NAME)
    return datetime.combine(day, clock).replace(tzinfo=tz)


def _torch_tenant():
    from tenants.models import Tenant

    tenant = (
        Tenant.objects.filter(slug=TORCH_TENANT_SLUG).order_by("id").first()
        or Tenant.objects.filter(request_url_name=TORCH_TENANT_SLUG)
        .order_by("id")
        .first()
    )
    if tenant is None or not is_torch_tenant(tenant):
        raise ValueError(f"Torch tenant {TORCH_TENANT_SLUG!r} not found")
    return tenant


def _cell(values: dict[str, Any], *names: str) -> str:
    lower_map = {(k or "").strip().lower(): v for k, v in values.items()}
    for name in names:
        raw = lower_map.get(name.strip().lower())
        if raw is None:
            continue
        if isinstance(raw, bool):
            return "TRUE" if raw else "FALSE"
        return str(raw).strip()
    return ""


def _truthy(raw: str | bool | None) -> bool:
    if isinstance(raw, bool):
        return raw
    return (raw or "").strip().lower() in {"true", "yes", "y", "1", "checked"}


def payload_from_mapping(
    values: dict[str, Any],
    *,
    row_number: int,
    sheet_id: str | None = None,
    dry_run: bool = False,
) -> SheetRowPayload:
    """Build a payload from header→value mapping (Apps Script or sheet read)."""
    return SheetRowPayload(
        row_number=int(row_number),
        sheet_id=(sheet_id or TORCH_PUBLIC_FORM_SHEET_ID).strip()
        or TORCH_PUBLIC_FORM_SHEET_ID,
        date=_cell(values, "Date"),
        start_time=_cell(values, "Start Time"),
        end_time=_cell(values, "End Time"),
        store_name=_cell(values, "Store Name"),
        address=_cell(values, "Address"),
        state=_cell(values, "State"),
        skus=_cell(values, "SKUs to sample", "SKUs"),
        ba_name=_cell(values, "BA Name"),
        ba_email=_cell(values, "Email"),
        spark_request_uuid=_cell(values, "Spark Request UUID"),
        confirmation_uuid=_cell(values, "Spark Confirmation UUID"),
        confirmation_status=_cell(values, "Confirmation Status"),
        sent_status=_cell(values, SENT_STATUS_HEADER, "Event Confirmation Sent?"),
        force_resend=_truthy(values.get("Force Resend")),
        dry_run=bool(dry_run),
    )


def validate_sheet_id(sheet_id: str | None) -> str:
    sid = (sheet_id or "").strip() or TORCH_PUBLIC_FORM_SHEET_ID
    if sid != TORCH_PUBLIC_FORM_SHEET_ID:
        raise ValueError(
            f"Refusing non-Torch sheet id {sid!r}; expected "
            f"{TORCH_PUBLIC_FORM_SHEET_ID}"
        )
    return sid


def _ensure_confirmation_headers(svc, sheet_id: str, tab: str | None) -> list[str]:
    existing = _read_header(svc, sheet_id, tab)
    have = {h.strip().lower() for h in existing}
    missing = [
        h for h in CONFIRMATION_EXTRA_HEADERS if h.strip().lower() not in have
    ]
    if not missing:
        return existing
    start = len(existing) + 1
    end = start + len(missing) - 1
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=_qualify(tab, f"{_col_letter(start)}1:{_col_letter(end)}1"),
        valueInputOption="RAW",
        body={"values": [missing]},
    ).execute()
    return existing + missing


def _header_index(header: list[str], name: str) -> int | None:
    target = name.strip().lower()
    for i, col in enumerate(header):
        if (col or "").strip().lower() == target:
            return i
    return None


def write_row_status(
    *,
    sheet_id: str,
    row_number: int,
    status: str,
    error: str = "",
    confirmation_uuid: str = "",
    sent_at: str | None = None,
    sent_column_value: str | None = None,
) -> None:
    """Stamp Confirmation Status / Error / UUID / O on one row.

    Bypasses ``TORCH_SHEET_WRITES_ENABLED`` — that kill switch only gates
    public-form appends, not confirmation status write-back.
    """
    svc = _service()
    if svc is None:
        logger.warning("torch sheet confirmation: no Sheets credentials")
        return
    tab = _tab_for_gid(svc, sheet_id, TORCH_PUBLIC_FORM_GID)
    header = _ensure_confirmation_headers(svc, sheet_id, tab)
    updates: list[dict[str, Any]] = []

    def _put(col_name: str, value: str) -> None:
        idx = _header_index(header, col_name)
        if idx is None:
            return
        col = _col_letter(idx + 1)
        updates.append(
            {
                "range": _qualify(tab, f"{col}{row_number}"),
                "values": [[value]],
            }
        )

    _put("Confirmation Status", status)
    if sent_at is not None:
        _put("Confirmation Sent At", sent_at)
    _put("Confirmation Error", error)
    if confirmation_uuid:
        _put("Spark Confirmation UUID", confirmation_uuid)
    if sent_column_value is not None:
        _put(SENT_STATUS_HEADER, sent_column_value)

    if not updates:
        return
    svc.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "RAW", "data": updates},
    ).execute()


def _create_and_send(
    payload: SheetRowPayload,
    *,
    tz_name: str,
    tz_note: str,
) -> ActionResult:
    from events.event_confirmations import send_confirmation_stage
    from events.models import EventConfirmation, TimeZone

    tenant = _torch_tenant()
    day = parse_sheet_date_iso(payload.date)
    start_clock = parse_sheet_clock(payload.start_time)
    starts_at = _parse_local_instant(day, start_clock, tz_name)
    ends_at = None
    if (payload.end_time or "").strip():
        end_clock = parse_sheet_clock(payload.end_time)
        ends_at = _parse_local_instant(day, end_clock, tz_name)
        if ends_at <= starts_at:
            ends_at = ends_at + timedelta(days=1)

    ba_name = (payload.ba_name or "").strip()
    ba_email = (payload.ba_email or "").strip()
    if not ba_name:
        raise ValueError("BA Name is required")
    if not ba_email or "@" not in ba_email:
        raise ValueError("A valid BA Email is required")

    if payload.dry_run:
        return ActionResult(
            ok=True,
            action="send",
            status=STATUS_QUEUED,
            message=(
                f"dry-run ok — would email {ba_email} for {payload.store_name} "
                f"on {day.isoformat()} {payload.start_time} ({tz_name})"
            ),
            timezone_name=tz_name,
            timezone_note=tz_note,
            dry_run=True,
            details={
                "ba_name": ba_name,
                "ba_email": ba_email,
                "starts_at": starts_at.isoformat(),
                "ends_at": ends_at.isoformat() if ends_at else None,
                "products": products_from_skus_cell(payload.skus),
            },
        )

    tz_row = TimeZone.objects.filter(name=tz_name).order_by("id").first()
    confirmation = EventConfirmation.objects.create(
        tenant=tenant,
        ba_name=ba_name,
        ba_email=ba_email,
        store_name=(payload.store_name or "").strip(),
        address=(payload.address or "").strip(),
        event_type_label=DEFAULT_EVENT_TYPE_LABEL,
        starts_at=starts_at,
        ends_at=ends_at,
        timezone_name=tz_name,
        timezone=tz_row,
        products=products_from_skus_cell(payload.skus),
        send_reminders=True,
        created_by=None,
    )
    result = send_confirmation_stage(
        confirmation, EventConfirmation.STAGE_BOOKED
    )
    confirmation.refresh_from_db()
    uuid_str = str(confirmation.uuid)
    now_label = dj_tz.now().astimezone(ZoneInfo(tz_name)).strftime(
        "%Y-%m-%d %H:%M %Z"
    )

    if not result.sent:
        err = result.reason or "email-failed"
        write_row_status(
            sheet_id=payload.sheet_id,
            row_number=payload.row_number,
            status=STATUS_ERROR,
            error=err,
            confirmation_uuid=uuid_str,
            sent_column_value=f"Error: {err}",
        )
        return ActionResult(
            ok=False,
            action="send",
            status=STATUS_ERROR,
            message=f"Saved confirmation but email failed ({err})",
            confirmation_uuid=uuid_str,
            timezone_name=tz_name,
            timezone_note=tz_note,
        )

    err_note = tz_note
    write_row_status(
        sheet_id=payload.sheet_id,
        row_number=payload.row_number,
        status=STATUS_SENT,
        error=err_note,
        confirmation_uuid=uuid_str,
        sent_at=now_label,
        sent_column_value=f"Sent {now_label}",
    )
    return ActionResult(
        ok=True,
        action="send",
        status=STATUS_SENT,
        message=f"Confirmation emailed to {ba_email}",
        confirmation_uuid=uuid_str,
        timezone_name=tz_name,
        timezone_note=tz_note,
    )


def send_from_sheet_row(payload: SheetRowPayload) -> ActionResult:
    """Idempotent send path for one sheet row."""
    validate_sheet_id(payload.sheet_id)

    if _already_cancelled(payload) and not payload.force_resend:
        return ActionResult(
            ok=False,
            action="send",
            status=STATUS_CANCELLED,
            message="Row is Cancelled — check Force Resend to send again",
        )

    if _already_sent(payload) and not payload.force_resend:
        return ActionResult(
            ok=False,
            action="send",
            status=STATUS_SENT,
            message="Already Sent — check Force Resend to email again",
            confirmation_uuid=payload.confirmation_uuid,
        )

    if not payload.dry_run:
        write_row_status(
            sheet_id=payload.sheet_id,
            row_number=payload.row_number,
            status=STATUS_QUEUED,
            error="",
            confirmation_uuid=payload.confirmation_uuid,
        )

    try:
        tz_name, tz_note = resolve_timezone_for_row(
            payload.address, payload.state
        )
        return _create_and_send(payload, tz_name=tz_name, tz_note=tz_note)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "torch sheet confirmation send failed row=%s", payload.row_number
        )
        if not payload.dry_run:
            write_row_status(
                sheet_id=payload.sheet_id,
                row_number=payload.row_number,
                status=STATUS_ERROR,
                error=str(exc)[:500],
                confirmation_uuid=payload.confirmation_uuid,
                sent_column_value=f"Error: {exc}"[:200],
            )
        return ActionResult(
            ok=False,
            action="send",
            status=STATUS_ERROR,
            message=str(exc),
            dry_run=payload.dry_run,
        )


def cancel_from_sheet_row(payload: SheetRowPayload) -> ActionResult:
    """Cancel path: email BA only if previously Sent; always stamp Cancelled."""
    from events.event_confirmations import send_cancellation_email
    from events.models import EventConfirmation

    validate_sheet_id(payload.sheet_id)

    if _already_cancelled(payload):
        return ActionResult(
            ok=True,
            action="cancel",
            status=STATUS_CANCELLED,
            message="Already Cancelled",
            confirmation_uuid=payload.confirmation_uuid,
        )

    was_sent = _already_sent(payload)
    confirmation = None
    uuid_str = (payload.confirmation_uuid or "").strip()
    if uuid_str:
        confirmation = (
            EventConfirmation.objects.filter(uuid=uuid_str)
            .select_related("tenant", "timezone")
            .first()
        )

    if payload.dry_run:
        return ActionResult(
            ok=True,
            action="cancel",
            status=STATUS_CANCELLED,
            message=(
                f"dry-run ok — would "
                f"{'email cancel to ' + payload.ba_email if was_sent else 'stamp Cancelled without email (never Sent)'}"
            ),
            confirmation_uuid=uuid_str,
            dry_run=True,
            details={"would_email": was_sent},
        )

    emailed = False
    email_note = ""
    if was_sent:
        if confirmation is None:
            # Build a transient confirmation-shaped send from the row so the
            # BA still gets a cancel when the UUID cell is missing.
            try:
                tenant = _torch_tenant()
                tz_name, _note = resolve_timezone_for_row(
                    payload.address, payload.state
                )
                day = parse_sheet_date_iso(payload.date)
                start_clock = parse_sheet_clock(payload.start_time)
                starts_at = _parse_local_instant(day, start_clock, tz_name)
                confirmation = EventConfirmation(
                    tenant=tenant,
                    ba_name=(payload.ba_name or "").strip() or "there",
                    ba_email=(payload.ba_email or "").strip(),
                    store_name=(payload.store_name or "").strip(),
                    address=(payload.address or "").strip(),
                    event_type_label=DEFAULT_EVENT_TYPE_LABEL,
                    starts_at=starts_at,
                    timezone_name=tz_name,
                    products=products_from_skus_cell(payload.skus),
                    send_reminders=False,
                )
            except Exception as exc:  # noqa: BLE001
                email_note = f"could not build cancel email: {exc}"
                confirmation = None

        if confirmation is not None and (confirmation.ba_email or "").strip():
            if getattr(confirmation, "pk", None):
                if confirmation.cancelled_at is None:
                    confirmation.cancelled_at = dj_tz.now()
                    confirmation.send_reminders = False
                    confirmation.save(
                        update_fields=[
                            "cancelled_at",
                            "send_reminders",
                            "updated_at",
                        ]
                    )
            try:
                send_cancellation_email(confirmation)
                emailed = True
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "torch sheet cancel email failed row=%s", payload.row_number
                )
                email_note = str(exc)[:500]
        elif was_sent and not (payload.ba_email or "").strip():
            email_note = "no BA email on row"
    else:
        email_note = "never Sent — cancelled without email"

    now_label = dj_tz.now().strftime("%Y-%m-%d %H:%M UTC")
    write_row_status(
        sheet_id=payload.sheet_id,
        row_number=payload.row_number,
        status=STATUS_CANCELLED,
        error=email_note,
        confirmation_uuid=uuid_str,
        sent_at=now_label if emailed else None,
        sent_column_value=f"Cancelled {now_label}",
    )
    return ActionResult(
        ok=True,
        action="cancel",
        status=STATUS_CANCELLED,
        message=(
            f"Cancelled"
            + (f" and emailed {payload.ba_email}" if emailed else f" ({email_note})")
        ),
        confirmation_uuid=uuid_str,
        details={"emailed": emailed},
    )


def handle_action(
    action: str,
    values: dict[str, Any],
    *,
    row_number: int,
    sheet_id: str | None = None,
    dry_run: bool = False,
) -> ActionResult:
    """Entry used by the HTTP endpoint."""
    payload = payload_from_mapping(
        values,
        row_number=row_number,
        sheet_id=sheet_id,
        dry_run=dry_run,
    )
    validate_sheet_id(payload.sheet_id)
    normalized = (action or "").strip().lower()
    if normalized in {"send", "confirm", "confirmation"}:
        return send_from_sheet_row(payload)
    if normalized in {"cancel", "cancelled", "cancellation"}:
        return cancel_from_sheet_row(payload)
    raise ValueError(f"Unknown action {action!r}; use send or cancel")
