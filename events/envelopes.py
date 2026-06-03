import datetime

from django.conf import settings

from utils.mailer import Envelope, Mailer
from events import models


def _apply_offset(
    value: datetime.datetime | None, offset_minutes: int
) -> datetime.datetime | None:
    """DEPRECATED — see `_apply_request_tz` below.

    Static offset arithmetic gets DST wrong (Pacific stored as -480
    minutes under-shifts during PDT by 1 hour). Kept only because some
    legacy callers still reach for the raw offset minutes.
    """
    if not value:
        return None
    return value + datetime.timedelta(minutes=offset_minutes)


def _apply_request_tz(
    value: datetime.datetime | None, obj
) -> datetime.datetime | None:
    """DST-aware variant — converts `value` to naive-local in obj's TZ.

    `obj` is an event or request that owns a `timezone` foreign key.
    See `utils.tz.apply_dst_aware_offset` for the resolution order.
    """
    if not value:
        return None
    # Local import: utils.tz is dep-free but importing at module top
    # creates a small import-time graph that the test harness has been
    # known to evaluate before django settings are configured.
    from utils.tz import apply_dst_aware_offset

    tz_row = None
    try:
        tz_row = getattr(obj, "timezone", None)
    except Exception:
        tz_row = None
    if tz_row is None and getattr(obj, "timezone_id", None):
        try:
            tz_row = models.TimeZone.objects.filter(id=obj.timezone_id).first()
        except Exception:
            tz_row = None
    return apply_dst_aware_offset(value, tz_row)


def _admin_request_url(request: models.Request | None) -> str:
    """Canonical deep-link to the request detail page on the admin site.

    Reads ADMIN_FRONTEND_URL from settings (set to
    https://admin.igniteproductions.co on Cloud Run; falls back to the
    *.web.app default in local/dev). Every transactional email should
    surface this so the reviewer goes straight to the request instead
    of landing on /requests/list and hunting for it.
    """
    if not request or not getattr(request, "uuid", None):
        return ""
    base = getattr(
        settings,
        "ADMIN_FRONTEND_URL",
        "https://spark-new-admin.web.app",
    ).rstrip("/")
    return f"{base}/request/view/{request.uuid}"


def _state_code_for_tz_fallback(obj) -> str | None:
    """Best-effort 2-letter US state code for the no-TimeZone email fallback.

    Tries, in order: the request/event's own ``state`` FK, the retailer's
    location state, then parsing the address text (e.g. "...Encinitas CA
    92024" → "CA"). Returns None when nothing resolves."""
    from events.routing import extract_state_code

    candidates = (
        lambda: obj.state.code,
        lambda: obj.retailer.location.state.code,
        lambda: extract_state_code(getattr(obj, "address", None)),
    )
    for getter in candidates:
        try:
            code = getter()
        except Exception:
            continue
        if code:
            return str(code).strip().upper()
    return None


def _get_timezone_offset_minutes(obj) -> int:
    """Return effective timezone offset (minutes) for event/request, DST-aware.

    Resolution order (matches `utils.tz.resolve_zoneinfo` so the GraphQL
    serializers and the email mailers agree on what "Pacific" means):

      1. If `obj.timezone` resolves to an IANA zone via the mapping in
         `utils.tz._APP_TZ_TO_IANA`, use the offset that zone reports
         for `obj.start_time` (or `date`, or now()). This is the path
         that fixes the "12pm saved as 11am" bug — PDT vs PST is
         computed against the actual shift datetime.
      2. Otherwise fall back to the static `TimeZone.offset` field, with
         the same hours-vs-minutes auto-detect used elsewhere.
    """
    from utils.tz import offset_minutes_for, offset_minutes_for_state

    # Prefer attached relation
    tz_row = None
    try:
        tz_row = getattr(obj, "timezone", None)
    except Exception:
        tz_row = None
    if tz_row is None:
        tz_id = getattr(obj, "timezone_id", None)
        if tz_id:
            try:
                tz_row = models.TimeZone.objects.filter(id=tz_id).first()
            except Exception:
                tz_row = None
    # Pick the most relevant datetime for the DST lookup — events have
    # `start_time` (most precise), requests have `start_time` too, both
    # have `date` as a fallback. If none are set, fall through to "now"
    # inside the offset helpers; the IANA mapping is what matters, the
    # exact wall-clock only matters near a DST transition.
    when = (
        getattr(obj, "start_time", None)
        or getattr(obj, "date", None)
    )

    if tz_row is None:
        # No TimeZone relation — fall back to the activation's STATE so the
        # email renders LOCAL time, not raw UTC (an Encinitas request with
        # no tz row was showing its UTC time). State comes from the request
        # FK, the retailer location, or the address text.
        state_code = _state_code_for_tz_fallback(obj)
        if state_code:
            state_off = offset_minutes_for_state(state_code, at=when)
            if state_off is not None:
                return state_off
        return 0

    return offset_minutes_for(tz_row, at=when)


def _format_dt_no_tz(
    value: datetime.datetime | None,
    fmt: str,
    offset_minutes: int = 0,
    *,
    obj=None,
) -> str:
    """Format a datetime in an event/request's local timezone.

    Prefer passing `obj=` (the event or request) — that path is
    DST-aware via `_apply_request_tz`. The positional `offset_minutes`
    arg is the legacy fallback; it stays so existing call-sites keep
    compiling, but anything new should go through `obj=`.
    """
    if not value:
        return ""
    if obj is not None:
        shifted = _apply_request_tz(value, obj)
        if shifted is not None:
            value = shifted
        else:
            # Fallback to the legacy fixed-offset path if obj didn't
            # resolve to a TZ row for some reason.
            value = _apply_offset(value, offset_minutes) or value
    else:
        value = _apply_offset(value, offset_minutes) or value
    formatted = value.replace(tzinfo=None).strftime(fmt)
    if fmt.startswith("%I"):
        return formatted.lstrip("0")
    return formatted


class EventApprovedNotificationMailer(Mailer):
    def __init__(
        self,
        event: models.Event,
        location: models.Location,
        to_emails: list[str],
    ) -> None:
        self.event = event
        self.location = location
        self.to_emails = to_emails

    def envelope(self) -> Envelope:
        offset = _get_timezone_offset_minutes(self.event)
        return Envelope(
            subject="Event approved",
            template="events.templates.emails.event_approved_notification",
            to_emails=self.to_emails,
            context={
                "event": self.event,
                "location": self.location,
                "event_date": _format_dt_no_tz(self.event.date, "%B %d, %Y", offset),
                "event_start_time": _format_dt_no_tz(
                    self.event.start_time, "%I:%M %p", offset
                ),
                "event_end_time": _format_dt_no_tz(
                    self.event.end_time, "%I:%M %p", offset
                ),
            },
        )


class RequestApprovedNotificationMailer(Mailer):
    def __init__(
        self,
        request: models.Request,
        location: models.Location,
        to_emails: list[str],
    ) -> None:
        self.request = request
        self.location = location
        self.to_emails = to_emails

    def envelope(self) -> Envelope:
        offset = _get_timezone_offset_minutes(self.request)
        return Envelope(
            subject="Request approved",
            template="events.templates.emails.request_approved_notification",
            to_emails=self.to_emails,
            context={
                "request": self.request,
                "location": self.location,
                "request_date": _format_dt_no_tz(
                    self.request.date, "%B %d, %Y", offset
                ),
                "request_start_time": _format_dt_no_tz(
                    self.request.start_time, "%I:%M %p", offset
                ),
                "request_end_time": _format_dt_no_tz(
                    self.request.end_time, "%I:%M %p", offset
                ),
            },
        )


class RequestorRequestApprovedMailer(Mailer):
    def _build_logo_attachment(self):
        return None

    def __init__(
        self,
        request: models.Request,
        location: models.Location | None,
        to_emails: list[str],
        cc_emails: list[str] | None = None,
        approver_email_fallback: str | None = None,
    ) -> None:
        self.request = request
        self.location = location
        self.to_emails = to_emails
        self.cc_emails = cc_emails or []
        # Used when the request has no approved_by User on it — e.g. the
        # public token-approval path where the RMM clicked the email link
        # but isn't (or can't be resolved to) a Spark user. Lets the email
        # still name who approved instead of showing a bare "-".
        self.approver_email_fallback = approver_email_fallback

    def envelope(self) -> Envelope:
        offset = _get_timezone_offset_minutes(self.request)
        request_id = f"REQ-{self.request.id}" if self.request.id else "-"
        location_name = "-"
        if self.location and self.location.name:
            location_name = self.location.name
            if self.location.state:
                location_name = f"{location_name}, {self.location.state.code}"
        elif self.request.address:
            location_name = self.request.address

        submitted_name = ""
        if self.request.created_by:
            submitted_name = (
                self.request.created_by.get_full_name() or self.request.created_by.email
            )
        if not submitted_name:
            submitted_name = self.request.client_name or "Client user"

        approved_by_name = "-"
        approved_by_email = "-"
        if self.request.approved_by:
            approved_by_name = (
                self.request.approved_by.get_full_name()
                or self.request.approved_by.email
                or "-"
            )
            approved_by_email = self.request.approved_by.email or "-"
        elif self.approver_email_fallback:
            # No User on the request (token approval by a non-user RMM) —
            # still show who clicked approve, from the token's recipient.
            approved_by_email = self.approver_email_fallback
            approved_by_name = self.approver_email_fallback

        bas_requested = self.request.request_details.count()
        requestor_name = (
            getattr(self.request, "client_name", None)
            or self.request.requestor_email
            or ""
        ).strip()
        first_name = (
            requestor_name.split()[0]
            if requestor_name and " " in requestor_name
            else requestor_name or "there"
        )
        account_name = None
        retailer = getattr(self.request, "retailer", None)
        if retailer and getattr(retailer, "name", None):
            account_name = retailer.name
        if not account_name:
            account_name = self.request.name or (
                self.location.name if self.location else None
            )

        return Envelope(
            subject="Your activation request is approved — Spark by Ignite",
            template="events.templates.emails.request_approved_requestor_v2",
            to_emails=self.to_emails,
            cc_emails=self.cc_emails,
            headers={"Reply-To": "events@igniteproductions.co"},
            from_email="Spark by Ignite <no-reply@igniteproductions.co>",
            context={
                "request": self.request,
                "location": self.location,
                "request_id": getattr(self.request, "id", None),
                "request_url": _admin_request_url(self.request),
                "first_name": first_name,
                "requestor_name": requestor_name,
                "requestor_email": getattr(self.request, "requestor_email", None)
                or getattr(self.request, "client_email", None),
                "reviewed_by_name": approved_by_name,
                "reviewed_by_email": approved_by_email,
                "tenant_name": getattr(
                    getattr(self.request, "tenant", None), "name", None
                ),
                "account_name": account_name,
                "full_address": getattr(self.request, "address", None),
                "activation_type": getattr(
                    getattr(self.request, "request_type", None), "name", None
                ),
                "distributor_name": getattr(
                    getattr(self.request, "distributor", None), "name", None
                ),
                "bas_requested": bas_requested,
                "request_date": _format_dt_no_tz(
                    self.request.date, "%B %d, %Y", offset
                ),
                "request_start_time": _format_dt_no_tz(
                    self.request.start_time, "%I:%M %p", offset
                ),
                "request_end_time": _format_dt_no_tz(
                    self.request.end_time, "%I:%M %p", offset
                ),
            },
        )


class RequestorRequestDeclinedMailer(Mailer):
    def _build_logo_attachment(self):
        return None

    def __init__(
        self,
        request: models.Request,
        location: models.Location | None,
        to_emails: list[str],
        cc_emails: list[str] | None = None,
        reviewed_by_name: str | None = None,
        reviewed_by_email: str | None = None,
    ) -> None:
        self.request = request
        self.location = location
        self.to_emails = to_emails
        self.cc_emails = cc_emails or []
        self.reviewed_by_name = reviewed_by_name
        self.reviewed_by_email = reviewed_by_email

    def envelope(self) -> Envelope:
        offset = _get_timezone_offset_minutes(self.request)
        request_id = f"REQ-{self.request.id}" if self.request.id else "-"
        location_name = "-"
        if self.location and self.location.name:
            location_name = self.location.name
            if self.location.state:
                location_name = f"{location_name}, {self.location.state.code}"
        elif self.request.address:
            location_name = self.request.address

        submitted_name = self.request.name or "there"

        requestor_name = (
            getattr(self.request, "client_name", None)
            or self.request.requestor_email
            or submitted_name
            or ""
        ).strip()
        first_name = (
            requestor_name.split()[0]
            if requestor_name and " " in requestor_name
            else requestor_name or "there"
        )
        account_name = None
        retailer = getattr(self.request, "retailer", None)
        if retailer and getattr(retailer, "name", None):
            account_name = retailer.name
        if not account_name:
            account_name = (
                self.request.name or (self.location.name if self.location else None)
            )

        return Envelope(
            subject="Update on your activation request — revision needed",
            template="events.templates.emails.request_declined_requestor_v2",
            to_emails=self.to_emails,
            cc_emails=self.cc_emails,
            headers={"Reply-To": "events@igniteproductions.co"},
            from_email="Spark by Ignite <no-reply@igniteproductions.co>",
            context={
                "request": self.request,
                "location": self.location,
                "request_id": getattr(self.request, "id", None),
                "request_url": _admin_request_url(self.request),
                "first_name": first_name,
                "requestor_name": requestor_name,
                "requestor_email": getattr(self.request, "requestor_email", None)
                or getattr(self.request, "client_email", None),
                "reviewed_by_name": self.reviewed_by_name or "the brand POC",
                "reviewed_by_email": self.reviewed_by_email or "-",
                "decline_reason": self.request.decline_reason or "",
                "tenant_name": getattr(
                    getattr(self.request, "tenant", None), "name", None
                ),
                "account_name": account_name,
                "full_address": getattr(self.request, "address", None),
                "activation_type": getattr(
                    getattr(self.request, "request_type", None), "name", None
                ),
                "distributor_name": getattr(
                    getattr(self.request, "distributor", None), "name", None
                ),
                "request_date": _format_dt_no_tz(
                    self.request.date, "%B %d, %Y", offset
                ),
                "request_start_time": _format_dt_no_tz(
                    self.request.start_time, "%I:%M %p", offset
                ),
                "request_end_time": _format_dt_no_tz(
                    self.request.end_time, "%I:%M %p", offset
                ),
            },
        )


class RequestCreatedNotificationMailer(Mailer):
    def __init__(
        self,
        request: models.Request,
        location: models.Location,
        to_emails: list[str],
        recipient_name: str | None = None,
    ) -> None:
        self.request = request
        self.location = location
        self.to_emails = to_emails
        self.recipient_name = recipient_name

    def envelope(self) -> Envelope:
        offset = _get_timezone_offset_minutes(self.request)
        request_url = (
            f"{settings.CLIENT_FRONTEND_URL.rstrip('/')}/request/view/{self.request.uuid}"
        )
        return Envelope(
            subject="New request created",
            template="events.templates.emails.request_created_notification",
            to_emails=self.to_emails,
            context={
                "request": self.request,
                "location": self.location,
                "recipient_name": self.recipient_name or "",
                "request_url": request_url,
                "request_date": _format_dt_no_tz(
                    self.request.date, "%B %d, %Y", offset
                ),
                "request_start_time": _format_dt_no_tz(
                    self.request.start_time, "%I:%M %p", offset
                ),
                "request_end_time": _format_dt_no_tz(
                    self.request.end_time, "%I:%M %p", offset
                ),
            },
        )


class ClientRequestCreatedNotificationMailer(Mailer):
    def __init__(
        self,
        request: models.Request,
        location: models.Location | None,
        to_emails: list[str],
    ) -> None:
        self.request = request
        self.location = location
        self.to_emails = to_emails

    def envelope(self) -> Envelope:
        offset = _get_timezone_offset_minutes(self.request)
        return Envelope(
            subject="New request created",
            template="events.templates.emails.request_created_admin_notification",
            to_emails=self.to_emails,
            context={
                "request": self.request,
                "location": self.location,
                "request_date": _format_dt_no_tz(
                    self.request.date, "%B %d, %Y", offset
                ),
                "request_start_time": _format_dt_no_tz(
                    self.request.start_time, "%I:%M %p", offset
                ),
                "request_end_time": _format_dt_no_tz(
                    self.request.end_time, "%I:%M %p", offset
                ),
            },
        )


class RmmAssignedRequestMailer(Mailer):
    """Sent to the RMM(s) responsible for a territory when a new
    public request comes in. CC's the Ignite team. Includes one-tap
    Approve/Decline links into the Spark admin."""

    def _build_logo_attachment(self):
        return None

    def __init__(
        self,
        request: models.Request,
        location: models.Location | None,
        to_emails: list[str],
        cc_emails: list[str] | None = None,
        rmm_first_name: str | None = None,
        state_code: str | None = None,
        review_link: str | None = None,
    ) -> None:
        self.request = request
        self.location = location
        self.to_emails = to_emails
        self.cc_emails = cc_emails or []
        self.rmm_first_name = rmm_first_name
        self.state_code = state_code
        self.review_link = review_link

    def envelope(self) -> Envelope:
        from django.conf import settings
        offset = _get_timezone_offset_minutes(self.request)

        requestor_name = (
            getattr(self.request, "client_name", None)
            or getattr(self.request, "requestor_email", None)
            or ""
        ).strip()
        requestor_email = (
            getattr(self.request, "requestor_email", None)
            or getattr(self.request, "client_email", None)
        )
        account_name = None
        retailer = getattr(self.request, "retailer", None)
        if retailer and getattr(retailer, "name", None):
            account_name = retailer.name
        if not account_name:
            account_name = (
                getattr(self.request, "name", None)
                or (self.location.name if self.location else None)
            )

        admin_base = getattr(
            settings, "ADMIN_FRONTEND_URL", "https://spark-new-admin.web.app",
        ).rstrip("/")
        # Mint a signed token bound to the first recipient. RMM emails
        # are typically To: a single mapped client + CC: the Ignite team,
        # so embedding the To:'s email in the token gives us an audit
        # trail of who actually acted. (CCs still have the same link in
        # their copy; if they click first we attribute the action to the
        # To:'s email — acceptable noise for a v1 audit log.)
        # Falls back to a tokenless internal /approvals URL only if no
        # recipient is on the envelope, which shouldn't happen in prod
        # but keeps the mailer robust for one-off internal sends.
        from events.views import make_approval_token

        primary_recipient = (self.to_emails[0] if self.to_emails else "") or ""
        if self.review_link:
            review_link = self.review_link
        elif primary_recipient:
            token = make_approval_token(self.request.id, primary_recipient)
            review_link = f"{admin_base}/approve/{token}"
        else:
            review_link = f"{admin_base}/approvals?request={self.request.id}"

        return Envelope(
            subject=f"[{getattr(getattr(self.request, 'tenant', None), 'name', 'New')}] {account_name or 'Request'} — needs your approval",
            template="events.templates.emails.rmm_assigned_request",
            from_email="Spark by Ignite <no-reply@igniteproductions.co>",
            to_emails=self.to_emails,
            cc_emails=self.cc_emails,
            headers={"Reply-To": "staffing@igniteproductions.co"},
            context={
                "request": self.request,
                "location": self.location,
                "request_id": getattr(self.request, "id", None),
                "request_url": _admin_request_url(self.request),
                "rmm_first_name": self.rmm_first_name,
                "state_code": self.state_code,
                "review_link": review_link,
                "requestor_name": requestor_name,
                "requestor_email": requestor_email,
                "tenant_name": getattr(
                    getattr(self.request, "tenant", None), "name", None
                ),
                "account_name": account_name,
                "full_address": getattr(self.request, "address", None),
                "activation_type": getattr(
                    getattr(self.request, "request_type", None), "name", None
                ),
                "distributor_name": getattr(
                    getattr(self.request, "distributor", None), "name", None
                ),
                "request_date": _format_dt_no_tz(
                    self.request.date, "%B %d, %Y", offset
                ),
                "request_start_time": _format_dt_no_tz(
                    self.request.start_time, "%I:%M %p", offset
                ),
                "request_end_time": _format_dt_no_tz(
                    self.request.end_time, "%I:%M %p", offset
                ),
            },
        )


class RequestorRequestCreatedMailer(Mailer):
    """Spark v2 branded 'We received your request' email — sent to the
    person who submitted the request form (public or internal). Pulls
    requestor name, routed-to RMM, full address, distributor, and the
    rest into the new template's context. Skips the auto-attached
    spark_logo.png (the template references the hosted URL directly)."""

    def _build_logo_attachment(self):
        return None

    def __init__(
        self,
        request: models.Request,
        location: models.Location | None,
        to_emails: list[str],
    ) -> None:
        self.request = request
        self.location = location
        self.to_emails = to_emails

    def envelope(self) -> Envelope:
        offset = _get_timezone_offset_minutes(self.request)

        # Pull the RMM the request was routed to so we can surface the
        # name in the email + the post-submit popup. rmm_asigned is the
        # canonical column.
        routed_to_name = None
        try:
            rmm = getattr(self.request, "rmm_asigned", None)
            if rmm:
                routed_to_name = (
                    f"{rmm.first_name or ''} {rmm.last_name or ''}".strip()
                    or rmm.email
                )
        except Exception:
            pass

        # Requestor identity — explicit fields on the request, falling
        # back to client_name / requestor_email.
        requestor_name = (
            getattr(self.request, "client_name", None)
            or getattr(self.request, "requestor_email", None)
            or ""
        ).strip()
        if requestor_name:
            requestor_first = requestor_name.split()[0]
        else:
            requestor_first = "there"

        # Account / venue label — fall back through retailer → name →
        # location so we always show something useful.
        account_name = None
        retailer = getattr(self.request, "retailer", None)
        if retailer and getattr(retailer, "name", None):
            account_name = retailer.name
        if not account_name:
            account_name = (
                getattr(self.request, "name", None)
                or (self.location.name if self.location else None)
            )

        return Envelope(
            subject="We received your request — Spark by Ignite",
            template="events.templates.emails.request_created_requestor_notification_v2",
            from_email="Spark by Ignite <no-reply@igniteproductions.co>",
            to_emails=self.to_emails,
            headers={"Reply-To": "staffing@igniteproductions.co"},
            context={
                "request": self.request,
                "location": self.location,
                "request_id": getattr(self.request, "id", None),
                "request_url": _admin_request_url(self.request),
                "requestor_name": requestor_name,
                "requestor_email": getattr(self.request, "requestor_email", None)
                or getattr(self.request, "client_email", None),
                "first_name": requestor_first,
                "routed_to_name": routed_to_name,
                "tenant_name": getattr(
                    getattr(self.request, "tenant", None), "name", None
                ),
                "account_name": account_name,
                "full_address": getattr(self.request, "address", None),
                "activation_type": getattr(
                    getattr(self.request, "request_type", None), "name", None
                ),
                "distributor_name": getattr(
                    getattr(self.request, "distributor", None), "name", None
                ),
                "request_date": _format_dt_no_tz(
                    self.request.date, "%B %d, %Y", offset
                ),
                "request_start_time": _format_dt_no_tz(
                    self.request.start_time, "%I:%M %p", offset
                ),
                "request_end_time": _format_dt_no_tz(
                    self.request.end_time, "%I:%M %p", offset
                ),
            },
        )


class RequestorRequestAutoApprovedMailer(Mailer):
    def __init__(
        self,
        request: models.Request,
        location: models.Location | None,
        to_emails: list[str],
    ) -> None:
        self.request = request
        self.location = location
        self.to_emails = to_emails

    def envelope(self) -> Envelope:
        offset = _get_timezone_offset_minutes(self.request)
        request_id = f"REQ-{self.request.id}" if self.request.id else "-"
        location_name = "-"
        if self.location and self.location.name:
            location_name = self.location.name
            if self.location.state:
                location_name = f"{location_name}, {self.location.state.code}"
        elif self.request.address:
            location_name = self.request.address

        submitted_name = ""
        if self.request.created_by:
            submitted_name = (
                self.request.created_by.get_full_name() or self.request.created_by.email
            )
        if not submitted_name:
            submitted_name = self.request.client_name or "Client user"
        submitted_email = (
            self.request.requestor_email
            or (self.request.created_by.email if self.request.created_by else "")
            or self.request.client_email
            or "-"
        )
        bas_requested = self.request.request_details.count()
        return Envelope(
            subject="Confirmed - your activation request is locked in",
            template="events.templates.emails.request_auto_approved_requestor_notification",
            to_emails=self.to_emails,
            headers={"Reply-To": "events@igniteproductions.co"},
            from_email=getattr(
                settings,
                "DEFAULT_FROM_EMAIL",
                "Spark by Ignite <no-reply@igniteproductions.co>",
            ),
            context={
                "request": self.request,
                "location": self.location,
                "request_id": request_id,
                "request_url": _admin_request_url(self.request),
                "location_name": location_name,
                "submitted_name": submitted_name,
                "submitted_email": submitted_email,
                "bas_requested": bas_requested,
                "request_date": _format_dt_no_tz(
                    self.request.date, "%m/%d/%Y", offset
                ),
                "request_start_time": _format_dt_no_tz(
                    self.request.start_time, "%I:%M %p", offset
                ),
                "request_end_time": _format_dt_no_tz(
                    self.request.end_time, "%I:%M %p", offset
                ),
            },
        )


class NoteMentionMailer(Mailer):
    """
    Fires when a teammate @-mentions someone in an internal Master
    Tracker note. The mentioned user gets a branded email with the
    note text + a link to the request so they can open and respond.

    No backend notes table yet (notes are localStorage-only), so this
    mailer takes the body + author info as plain inputs rather than
    loading a Note row. When server-side notes ship the wiring stays
    the same — we just stop passing the body verbatim.
    """

    def _build_logo_attachment(self):
        return None

    def __init__(
        self,
        *,
        request: models.Request,
        mentioned_email: str,
        mentioned_name: str | None,
        note_body: str,
        author_name: str,
        author_email: str | None,
        request_url: str,
    ) -> None:
        self.request = request
        self.mentioned_email = mentioned_email
        self.mentioned_name = mentioned_name
        self.note_body = note_body
        self.author_name = author_name
        self.author_email = author_email
        self.request_url = request_url

    def envelope(self) -> Envelope:
        request_id = f"REQ-{self.request.id}" if self.request.id else "-"
        account_name = None
        retailer = getattr(self.request, "retailer", None)
        if retailer and getattr(retailer, "name", None):
            account_name = retailer.name
        if not account_name:
            account_name = self.request.name or "—"

        tenant_name = "—"
        tenant = getattr(self.request, "tenant", None)
        if tenant and getattr(tenant, "name", None):
            tenant_name = tenant.name

        first_name = "there"
        if self.mentioned_name:
            first = self.mentioned_name.split()[0].strip() if self.mentioned_name else ""
            if first:
                first_name = first
        elif self.mentioned_email:
            first_name = self.mentioned_email.split("@")[0]

        return Envelope(
            subject=f"[{tenant_name}] {self.author_name} tagged you on {account_name}",
            template="events.templates.emails.note_mention",
            from_email="Spark by Ignite <no-reply@igniteproductions.co>",
            to_emails=[self.mentioned_email],
            headers={"Reply-To": self.author_email or "staffing@igniteproductions.co"},
            context={
                "first_name": first_name,
                "mentioned_name": self.mentioned_name or "",
                "author_name": self.author_name,
                "author_email": self.author_email or "",
                "note_body": self.note_body,
                "request_id": request_id,
                "request_url": self.request_url,
                "account_name": account_name,
                "tenant_name": tenant_name,
            },
        )
