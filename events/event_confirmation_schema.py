"""GraphQL surface for the "Send Event Confirmation" admin tab.

Four reads and two writes:

* ``eventConfirmationFormOptions`` — everything the form needs in one round
  trip: the SKU picker's options, the tenant's recap + training links, and the
  timezone list. The links are READ from the tenant (``checkin_code``,
  ``checkin_training_url``) so the tab shows the admin the same URLs the email
  will actually contain, rather than a copy that can go stale.
* ``upcomingShiftsForConfirmation`` — the optional prefill picker, for the case
  where the shift DOES already exist in Spark.
* ``eventConfirmations`` — what's been sent, and what's still queued, so the tab
  can show history instead of being write-only.
* ``sendEventConfirmation`` / ``cancelEventConfirmationReminders`` — the writes.

Mounted on the SPARK (admin) schema only — see events/schema.py. Deliberately
NOT added to ``EventQueryClient``/``EventMutationsClient``: ``EventQuerySpark``
inherits from the client types, so anything added there would also land in the
clients schema, whose ``schema_clients.graphql`` is hand-patched.
"""

from __future__ import annotations

from datetime import date as _date, datetime, time as _time

import strawberry
from asgiref.sync import sync_to_async
from strawberry import relay

from events.staffing_board import _accessible_tenants, _iso
from utils.graphql.mixins import resolve_id_to_int
from utils.graphql.permissions import IsClientOrSparkAdmin

DEFAULT_TIMEZONE_NAME = "America/Los_Angeles"
# The picker is a convenience, not a report — one screen of upcoming shifts.
SHIFT_PICKER_LIMIT = 200
CONFIRMATION_HISTORY_LIMIT = 200


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@strawberry.type
class ConfirmationTimezoneOption:
    id: strawberry.ID
    name: str
    code: str


@strawberry.type
class EventConfirmationFormOptions:
    product_options: list[str]
    recap_url: str
    training_url: str
    timezones: list[ConfirmationTimezoneOption]
    default_timezone_name: str
    from_email: str
    # False when the tenant has no checkin_code / training URL yet — the tab
    # warns rather than silently sending an email with a missing button.
    has_recap_link: bool
    has_training_link: bool


@strawberry.type
class ConfirmationShiftOption:
    event_uuid: strawberry.ID
    event_name: str
    store_name: str
    address: str
    event_type_label: str
    starts_at: str | None
    ends_at: str | None
    timezone_id: strawberry.ID | None
    timezone_name: str
    products: list[str]
    ba_name: str
    ba_email: str
    ambassador_event_uuid: strawberry.ID | None


@strawberry.type
class EventConfirmationStageStatus:
    stage: str
    sent_at: str | None
    attempts: int
    last_error: str


@strawberry.type
class EventConfirmationRow:
    uuid: strawberry.ID
    ba_name: str
    ba_email: str
    store_name: str
    address: str
    event_type_label: str
    starts_at: str | None
    ends_at: str | None
    date_label: str
    time_label: str
    products: list[str]
    send_reminders: bool
    cancelled_at: str | None
    created_at: str | None
    sends: list[EventConfirmationStageStatus]


@strawberry.input
class SendEventConfirmationInput:
    tenant_id: strawberry.ID
    ba_name: str
    ba_email: str
    # YYYY-MM-DD and HH:MM (24h), both local to `timezone_name` — NOT UTC. The
    # backend combines them into the aware instant the reminders key off, so the
    # FE never has to know the venue's offset (getting that wrong is exactly how
    # a 9 AM Central demo once surfaced as 4 AM — REQ-1514).
    date: str
    start_time: str
    end_time: str | None = None
    timezone_name: str | None = None
    store_name: str = ""
    address: str = ""
    event_type_label: str = ""
    products: list[str] | None = None
    send_reminders: bool = True
    # Set when the admin prefilled from a real shift — kept as a back-reference.
    event_uuid: strawberry.ID | None = None
    ambassador_event_uuid: strawberry.ID | None = None
    # Off for "save without emailing" (schedule the reminders only).
    send_now: bool = True
    client_mutation_id: strawberry.ID | None = None


@strawberry.type
class SendEventConfirmationResponse:
    success: bool
    message: str
    confirmation: EventConfirmationRow | None = None
    client_mutation_id: strawberry.ID | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row(confirmation) -> EventConfirmationRow:
    from events.event_confirmations import format_event_date, format_time_range

    local_start = confirmation.local_start()
    return EventConfirmationRow(
        uuid=strawberry.ID(str(confirmation.uuid)),
        ba_name=confirmation.ba_name or "",
        ba_email=confirmation.ba_email or "",
        store_name=confirmation.store_name or "",
        address=confirmation.address or "",
        event_type_label=confirmation.event_type_label or "",
        starts_at=_iso(confirmation.starts_at),
        ends_at=_iso(confirmation.ends_at),
        date_label=format_event_date(local_start),
        time_label=format_time_range(local_start, confirmation.local_end()),
        products=list(confirmation.products or []),
        send_reminders=bool(confirmation.send_reminders),
        cancelled_at=_iso(confirmation.cancelled_at),
        created_at=_iso(confirmation.created_at),
        sends=[
            EventConfirmationStageStatus(
                stage=s.stage,
                sent_at=_iso(s.sent_at),
                attempts=int(s.attempts or 0),
                last_error=s.last_error or "",
            )
            for s in confirmation.sends.all()
        ],
    )


def _parse_local_instant(
    date_str: str, time_str: str, tz_name: str
) -> datetime:
    """Combine a local date + wall-clock time into an AWARE instant.

    ``ZoneInfo`` (not a fixed offset) resolves the offset for that specific
    date, so a shift either side of a DST boundary lands on the right instant.
    """
    from zoneinfo import ZoneInfo

    day = _date.fromisoformat(str(date_str).strip())
    raw = str(time_str).strip()
    parts = raw.split(":")
    if len(parts) < 2:
        raise ValueError(f"Time must be HH:MM — got {raw!r}")
    clock = _time(int(parts[0]), int(parts[1]))
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 — unknown IANA name
        tz = ZoneInfo(DEFAULT_TIMEZONE_NAME)
    return datetime.combine(day, clock).replace(tzinfo=tz)


def _products_for_event(event) -> list[str]:
    """The SKUs already recorded on the shift's request, if any.

    Same source the LD master-tracker mirror reads for its "SKUs to sample"
    column (utils.sheets_mirror), so a prefilled confirmation agrees with the
    client's own sheet instead of being re-keyed by hand.
    """
    request = getattr(event, "request", None)
    if request is None:
        return []
    out: list[str] = []
    seen: set[str] = set()
    try:
        for rp in request.request_product.all():
            product = getattr(rp, "product", None)
            name = (getattr(product, "name", "") or "").strip()
            if name and name.lower() not in seen:
                seen.add(name.lower())
                out.append(name)
    except Exception:  # noqa: BLE001 — prefill must never break the form
        return []
    return out


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

@strawberry.type
class EventConfirmationQueries:
    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def event_confirmation_form_options(
        self, info: strawberry.Info, tenant_id: strawberry.ID
    ) -> EventConfirmationFormOptions:
        """Everything the send form needs, in one round trip."""
        user = info.context.request.user
        is_admin, allowed = await _accessible_tenants(user)

        def _go():
            from events.models import TimeZone
            from events.event_confirmations import (
                CONFIRMATION_FROM_EMAIL, recap_url_for, training_url_for,
            )
            # The ONLY tenant-specific piece of this whole feature. Imported
            # rather than re-typed so the picker and LD's recap form can't drift.
            from recaps.management.commands.setup_ld_retail_checkin import (
                product_options,
            )
            from tenants.models import Tenant

            try:
                tid = resolve_id_to_int(tenant_id)
            except Exception:  # noqa: BLE001
                raise ValueError("Invalid tenant id.")
            if not is_admin and tid not in (allowed or set()):
                raise ValueError("Not allowed for this tenant.")

            tenant = Tenant.objects.filter(id=tid).first()
            if tenant is None:
                raise ValueError("Tenant not found.")

            recap = recap_url_for(tenant)
            training = training_url_for(tenant)
            zones = [
                ConfirmationTimezoneOption(
                    id=strawberry.ID(str(z.id)), name=z.name or "", code=z.code or ""
                )
                for z in TimeZone.objects.order_by("name").distinct("name")
            ]
            return EventConfirmationFormOptions(
                product_options=product_options(),
                recap_url=recap,
                training_url=training,
                timezones=zones,
                default_timezone_name=DEFAULT_TIMEZONE_NAME,
                from_email=CONFIRMATION_FROM_EMAIL,
                has_recap_link=bool(recap),
                has_training_link=bool(training),
            )

        return await sync_to_async(_go)()

    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def upcoming_shifts_for_confirmation(
        self, info: strawberry.Info, tenant_id: strawberry.ID
    ) -> list[ConfirmationShiftOption]:
        """Upcoming shifts (with their rostered BA, when there is one) to
        prefill the form. Empty is a normal answer — most of these shifts are
        typed straight into the tab and were never in Spark."""
        user = info.context.request.user
        is_admin, allowed = await _accessible_tenants(user)

        def _go():
            from django.utils import timezone as dj_tz

            from ambassadors.models import AmbassadorEvent
            from events.models import Event

            try:
                tid = resolve_id_to_int(tenant_id)
            except Exception:  # noqa: BLE001
                raise ValueError("Invalid tenant id.")
            if not is_admin and tid not in (allowed or set()):
                raise ValueError("Not allowed for this tenant.")

            now = dj_tz.now()
            events = list(
                Event.objects.filter(
                    tenant_id=tid, start_time__gt=now
                )
                .select_related("timezone", "event_type", "retailer", "request")
                .prefetch_related("request__request_product__product")
                .order_by("start_time")[:SHIFT_PICKER_LIMIT]
            )
            if not events:
                return []

            rosters: dict[int, list] = {}
            for ae in AmbassadorEvent.objects.filter(
                event_id__in=[e.id for e in events], is_approved=True
            ).select_related("ambassador__user"):
                rosters.setdefault(ae.event_id, []).append(ae)

            out: list[ConfirmationShiftOption] = []
            for event in events:
                tz = getattr(event, "timezone", None)
                store = (
                    getattr(getattr(event, "retailer", None), "name", "")
                    or event.name
                    or ""
                )
                base = dict(
                    event_uuid=strawberry.ID(str(event.uuid)),
                    event_name=event.name or "",
                    store_name=store,
                    address=event.address or "",
                    event_type_label=(
                        getattr(getattr(event, "event_type", None), "name", "") or ""
                    ),
                    starts_at=_iso(event.start_time),
                    ends_at=_iso(event.end_time or event.new_end_time),
                    timezone_id=(
                        strawberry.ID(str(tz.id)) if tz is not None else None
                    ),
                    timezone_name=(getattr(tz, "name", "") or ""),
                    products=_products_for_event(event),
                )
                crew = rosters.get(event.id) or []
                if not crew:
                    # Still offered — an admin can pick the shift and type the BA.
                    out.append(
                        ConfirmationShiftOption(
                            **base, ba_name="", ba_email="",
                            ambassador_event_uuid=None,
                        )
                    )
                    continue
                for ae in crew:
                    ba_user = getattr(getattr(ae, "ambassador", None), "user", None)
                    full = " ".join(
                        p for p in (
                            getattr(ba_user, "first_name", "") or "",
                            getattr(ba_user, "last_name", "") or "",
                        ) if p
                    ).strip()
                    out.append(
                        ConfirmationShiftOption(
                            **base,
                            ba_name=full,
                            ba_email=(getattr(ba_user, "email", "") or ""),
                            ambassador_event_uuid=strawberry.ID(str(ae.uuid)),
                        )
                    )
            return out

        return await sync_to_async(_go)()

    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def event_confirmations(
        self, info: strawberry.Info, tenant_id: strawberry.ID
    ) -> list[EventConfirmationRow]:
        """Confirmations for a tenant, newest shift first — the tab's history."""
        user = info.context.request.user
        is_admin, allowed = await _accessible_tenants(user)

        def _go():
            from events.models import EventConfirmation

            try:
                tid = resolve_id_to_int(tenant_id)
            except Exception:  # noqa: BLE001
                raise ValueError("Invalid tenant id.")
            if not is_admin and tid not in (allowed or set()):
                raise ValueError("Not allowed for this tenant.")

            rows = (
                EventConfirmation.objects.filter(tenant_id=tid)
                .select_related("tenant", "timezone")
                .prefetch_related("sends")
                .order_by("-starts_at")[:CONFIRMATION_HISTORY_LIMIT]
            )
            return [_row(r) for r in rows]

        return await sync_to_async(_go)()


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

@strawberry.type
class EventConfirmationMutations:
    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def send_event_confirmation(
        self, info: strawberry.Info, input: SendEventConfirmationInput
    ) -> SendEventConfirmationResponse:
        """Save a confirmation and (by default) email the BA their details now.

        Creates NOTHING but the confirmation: no Event, no roster row. Minting a
        booking to hang the reminders off would fire the "New shift offered"
        push, sync Google Calendar and land in dashboard KPIs — three loud side
        effects for a tab whose job is to send one email. The reminders key off
        this row's own ``starts_at`` instead.
        """
        user = info.context.request.user
        is_admin, allowed = await _accessible_tenants(user)

        def _go():
            from ambassadors.models import AmbassadorEvent
            from events.event_confirmations import send_confirmation_stage
            from events.models import Event, EventConfirmation, TimeZone

            try:
                tid = resolve_id_to_int(input.tenant_id)
            except Exception:  # noqa: BLE001
                return False, "Invalid tenant id.", None
            if not is_admin and tid not in (allowed or set()):
                return False, "Not allowed for this tenant.", None

            ba_name = (input.ba_name or "").strip()
            ba_email = (input.ba_email or "").strip()
            if not ba_name:
                return False, "BA name is required.", None
            if not ba_email or "@" not in ba_email:
                return False, "A valid BA email is required.", None

            linked_event = None
            if input.event_uuid:
                linked_event = Event.objects.filter(
                    uuid=str(input.event_uuid), tenant_id=tid
                ).select_related("timezone").first()
            linked_roster = None
            if input.ambassador_event_uuid:
                linked_roster = AmbassadorEvent.objects.filter(
                    uuid=str(input.ambassador_event_uuid), tenant_id=tid
                ).first()

            # Timezone: what the admin chose, else the linked shift's, else ops.
            tz_name = (input.timezone_name or "").strip()
            if not tz_name and linked_event is not None:
                tz_name = getattr(
                    getattr(linked_event, "timezone", None), "name", ""
                ) or ""
            tz_name = tz_name or DEFAULT_TIMEZONE_NAME

            try:
                starts_at = _parse_local_instant(input.date, input.start_time, tz_name)
            except (TypeError, ValueError) as exc:
                return False, f"Couldn't read the date/time: {exc}", None
            ends_at = None
            if (input.end_time or "").strip():
                try:
                    ends_at = _parse_local_instant(
                        input.date, input.end_time, tz_name
                    )
                except (TypeError, ValueError) as exc:
                    return False, f"Couldn't read the end time: {exc}", None
                # An end before the start means an overnight shift, not a typo.
                if ends_at <= starts_at:
                    from datetime import timedelta

                    ends_at = ends_at + timedelta(days=1)

            tz_row = (
                TimeZone.objects.filter(name=tz_name).order_by("id").first()
            )

            confirmation = EventConfirmation.objects.create(
                tenant_id=tid,
                event=linked_event,
                ambassador_event=linked_roster,
                ba_name=ba_name,
                ba_email=ba_email,
                store_name=(input.store_name or "").strip(),
                address=(input.address or "").strip(),
                event_type_label=(input.event_type_label or "").strip(),
                starts_at=starts_at,
                ends_at=ends_at,
                timezone=tz_row,
                products=[str(p) for p in (input.products or [])],
                send_reminders=bool(input.send_reminders),
                created_by=user if getattr(user, "id", None) else None,
            )

            note = ""
            if input.send_now:
                result = send_confirmation_stage(
                    confirmation, EventConfirmation.STAGE_BOOKED
                )
                if not result.sent:
                    # The row is saved either way — the admin can retry the send
                    # without re-typing the shift.
                    note = f" Saved, but the email didn't send ({result.reason})."
            else:
                note = " Saved without emailing."

            confirmation.refresh_from_db()
            reminders = (
                "24h + 3h reminders are on."
                if confirmation.send_reminders
                else "Reminders are off for this one."
            )
            msg = (
                f"Confirmation for {ba_name} saved. {reminders}{note}"
                if note
                else f"Confirmation emailed to {ba_email}. {reminders}"
            )
            return True, msg, _row(confirmation)

        ok, msg, row = await sync_to_async(_go)()
        return SendEventConfirmationResponse(
            success=ok,
            message=msg,
            confirmation=row,
            client_mutation_id=input.client_mutation_id,
        )

    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def cancel_event_confirmation_reminders(
        self,
        info: strawberry.Info,
        uuid: strawberry.ID,
        client_mutation_id: strawberry.ID | None = None,
    ) -> SendEventConfirmationResponse:
        """Stop the 24h/3h reminders for one confirmation (shift called off).

        Marks it cancelled rather than deleting it, so the record of what was
        already emailed to the BA survives.
        """
        user = info.context.request.user
        is_admin, allowed = await _accessible_tenants(user)

        def _go():
            from django.utils import timezone as dj_tz

            from events.models import EventConfirmation

            confirmation = (
                EventConfirmation.objects.filter(uuid=str(uuid))
                .select_related("tenant", "timezone")
                .prefetch_related("sends")
                .first()
            )
            if confirmation is None:
                return False, "Confirmation not found.", None
            if not is_admin and confirmation.tenant_id not in (allowed or set()):
                return False, "Not allowed for this tenant.", None

            if confirmation.cancelled_at is None:
                confirmation.cancelled_at = dj_tz.now()
                confirmation.send_reminders = False
                confirmation.save(
                    update_fields=["cancelled_at", "send_reminders", "updated_at"]
                )
            return True, "Reminders cancelled.", _row(confirmation)

        ok, msg, row = await sync_to_async(_go)()
        return SendEventConfirmationResponse(
            success=ok,
            message=msg,
            confirmation=row,
            client_mutation_id=client_mutation_id,
        )
