"""Feel Free: add an Event Activation program to the BA walk-up.

Adds **Event Activation** next to Feel Free's existing field-sampling program
on the standing check-in ``/checkin/FF-YMMK3Q`` (code never reminted) and
builds its recap form, ``Feel Free · Event Activation Recap``. Same structure
as the other brands' Event Activation recaps, sampling only: Feel Free's own
field-sampling form tracks no onsite sales (only purchase intent), so there
are no units-sold, spend, or purchase fields. Products Sampled resolves from
the live Feel Free Product catalog with a per-SKU "Samples distributed" count,
matching the retail form's "How many samples of … were distributed?" wording.

Feel Free files storage→stops payable mileage before every recap, so the form
carries a ``Mileage`` field the walk-up fills from that itinerary.

The walk-up's current default program (pinned, else the one walk-ups stamp
today) is pinned explicitly and offered first, so retail check-ins keep the
same event type and the same "Feel Free - Field Sampling" form. Retail has no
photo buckets; the Event Activation buckets are keyed to that program only,
so the retail page stays on its single photo grid.

Event Activation is excluded from Retail + On-Premise conversion by its
program name; recaps still land as ``approved=False`` (Needs review).

Idempotent. DRY-RUN by default; ``--apply`` writes. Run on prod via
``/internal/cron/setup-feel-free-event-activation`` (or the "Setup Feel Free
Event Activation" GitHub Action).
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import CommandError

from recaps.management.commands.setup_torch_event_activation import (
    EVENT_LABEL,
    PRODUCTS_SAMPLED,
    Field,
)
from recaps.management.commands.setup_torch_event_activation import (
    Command as EventActivationCommand,
)

TENANT_SLUG = "feel-free"
TENANT_FORM_SLUG = "bl00-feel-free"
CHECKIN_CODE = "FF-YMMK3Q"

TEMPLATE_NAME = "Feel Free · Event Activation Recap"

ACTIVATION_BUCKETS: list[dict] = [
    {
        "name": "Activation Set Up",
        "helper": "Booth / table, signage, and the full footprint",
    },
    {
        "name": "Consumer Sampling Pictures",
        "helper": "please try to upload 8+",
        "min": 8,
    },
    {
        "name": "Expense Receipts",
        "helper": "Every corporate card receipt (parking, ice, supplies)",
    },
]

EVENT_KIND_OPTS = [
    "Music Festival / Concert",
    "Sporting Event",
    "Street Fair / Community Event",
    "Farmers Market / Pop-Up",
    "College / Campus Event",
    "Private / Corporate Event",
    "Other",
]
TRAFFIC_OPTS = ["High", "Medium", "Low"]

SAMPLE_QTY_LAYOUT = {
    "sampleQtyLabel": "Samples distributed",
    "sampleQtyTotalLabel": "Total samples distributed",
}

COMPETITOR_FIELD = (
    "Ask consumers what competitor products they drink. "
    "And if so, which one. Why do you like it?"
)

SPEC_FIELDS: list[tuple[str, list[Field]]] = [
    (
        "Event Details",
        [
            ("What kind of event was it?", "select", True, list(EVENT_KIND_OPTS), ""),
            (
                "Total Estimated Attendance",
                "number",
                True,
                [],
                "Best guess for the whole event, e.g. 2500",
            ),
            (
                "Describe the traffic at your booth",
                "select",
                True,
                list(TRAFFIC_OPTS),
                "",
            ),
            (
                "Mileage",
                "number",
                True,
                [],
                "Filled in from your trip log — only change it for a detour",
            ),
        ],
    ),
    (
        "Consumer Engagement",
        [
            (
                "How many TOTAL consumers did you sample?",
                "number",
                True,
                [],
                "People who actually tasted a sample",
            ),
            (
                "People engaged",
                "number",
                True,
                [],
                "People you talked with about Feel Free, tasted or not",
            ),
            (
                "How many consumers were trying Feel Free for the first time?",
                "number",
                True,
                [],
                "",
            ),
            (
                "How many consumers that were engaged with knew about Feel "
                "Free product/brand?",
                "number",
                True,
                [],
                "",
            ),
            (
                "How many consumers would be willing to purchase Feel Free at "
                "a store after tasting it?",
                "number",
                True,
                [],
                "",
            ),
        ],
    ),
    (
        "Feedback & Account Notes",
        [
            (
                "Demographics (general age, sex, ethnicities of consumers)",
                "longtext",
                True,
                [],
                "e.g. Mostly 25-40, even male/female split, after-work crowd",
            ),
            (
                "Consumer Feedback",
                "longtext",
                True,
                [],
                "Which flavors people liked best and what they said about the taste",
            ),
            (
                "Quotes from Consumers",
                "longtext",
                True,
                [],
                '"Didn\'t expect it to taste this good." - male, ~30',
            ),
            (
                "What were the top 5 frequently asked questions you received "
                "from consumers?",
                "longtext",
                True,
                [],
                "e.g. Where can I buy it? How much is it? What flavors are there?",
            ),
            (COMPETITOR_FIELD, "longtext", False, [], ""),
            ("Positive stories from the event", "longtext", False, [], ""),
            (
                "Event organizer / venue feedback",
                "longtext",
                False,
                [],
                "Anything the organizer or venue staff said about Feel Free or the booth",
            ),
            ("Anything you'd change or do differently?", "longtext", False, [], ""),
        ],
    ),
]

SECTION_ORDER = {
    "Event Details": 0,
    "Consumer Engagement": 1,
    "Feedback & Account Notes": 2,
    PRODUCTS_SAMPLED: 3,
}


def build_spec(product_opts: list[str]) -> list[tuple[str, list[Field]]]:
    """SPEC with catalog-backed Products Sampled options."""
    sections = [(name, list(fields)) for name, fields in SPEC_FIELDS]
    sections.append(
        (
            PRODUCTS_SAMPLED,
            [
                (
                    PRODUCTS_SAMPLED,
                    "multiselect",
                    True,
                    list(product_opts),
                    "Pick every product you sampled, then enter samples distributed for each",
                )
            ],
        )
    )
    return sections


SPEC = build_spec([])


class Command(EventActivationCommand):
    help = (
        "Feel Free: add Event Activation to the FF-YMMK3Q walk-up and seed its "
        "sampling-only recap template (dry-run by default; --apply to write)."
    )

    tenant_slug = TENANT_SLUG
    tenant_form_slug = TENANT_FORM_SLUG
    checkin_code = CHECKIN_CODE
    template_name = TEMPLATE_NAME
    brand_name = "Feel Free"
    activation_buckets = ACTIVATION_BUCKETS
    sample_qty_layout = SAMPLE_QTY_LAYOUT
    section_order = SECTION_ORDER

    def build_spec(self, product_opts: list[str]) -> list[tuple[str, list[Field]]]:
        return build_spec(product_opts)

    def _retail_program(self, tenant):
        """The program walk-ups stamp today (pinned, else the implicit default)."""
        from ambassadors import checkin_web

        program = getattr(tenant, "checkin_event_type", None)
        if program is None:
            offered = [
                et
                for et in checkin_web.selectable_event_types(tenant)
                if not checkin_web.is_event_activation_type(et)
            ]
            program = offered[0] if offered else checkin_web._default_event_type(tenant)
        return program

    def _report_current(self, tenant) -> None:
        from django.utils import timezone

        from ambassadors import checkin_web
        from events.models import Event, EventType
        from recaps.models import CustomRecapTemplate

        since = timezone.now() - timedelta(days=90)
        self.stdout.write(
            f"\nLocation mode: {checkin_web.tenant_location_mode(tenant)!r}"
        )
        self.stdout.write("Event types (events in the last 90 days):")
        for et in EventType.objects.filter(tenant_id=tenant.id).order_by("id"):
            recent = Event.objects.filter(
                tenant_id=tenant.id, event_type_id=et.id, date__gte=since
            ).count()
            self.stdout.write(f"  [{et.id}] {et.name!r} — {recent}")
        pin = getattr(tenant, "checkin_event_type", None)
        self.stdout.write(
            f"Pinned program : {pin.name!r}" if pin else "Pinned program : (none)"
        )
        retail = self._retail_program(tenant)
        self.stdout.write(
            f"Walk-up default: {getattr(retail, 'name', None)!r} "
            f"[{getattr(retail, 'id', None)}]"
        )
        self.stdout.write(
            f"Photo buckets  : {getattr(tenant, 'checkin_photo_buckets', None)!r}"
        )
        self.stdout.write("Templates:")
        for tpl in CustomRecapTemplate.objects.filter(tenant_id=tenant.id).order_by(
            "id"
        ):
            self.stdout.write(
                f"  [{tpl.id}] {tpl.name!r} event_type="
                f"{getattr(tpl.event_type, 'name', None)!r}"
            )

    def _add_to_picker(self, tenant, activation, apply: bool) -> None:
        """Offer [current default, Event Activation]; pin the default if unpinned."""
        from ambassadors import checkin_web

        retail = self._retail_program(tenant)
        if retail is None:
            raise CommandError(
                "Feel Free has no event types — nothing to keep as the default."
            )
        if checkin_web.is_event_activation_type(retail):
            raise CommandError(
                f"The walk-up default is {retail.name!r}; pin the field-sampling "
                "program as checkin_event_type first."
            )
        offered = list(
            tenant.checkin_event_types.filter(tenant_id=tenant.id).order_by("id")
        )
        wanted = list(offered) or [retail]
        if all(et.id != retail.id for et in wanted):
            wanted.insert(0, retail)
        if activation is not None and all(et.id != activation.id for et in wanted):
            wanted.append(activation)

        self.stdout.write("\nWalk-up picker:")
        for et in wanted:
            self.stdout.write(f"  [{et.id}] {et.name!r}")
        if activation is None:
            self.stdout.write(f"  [new] {EVENT_LABEL!r}")
        pin = getattr(tenant, "checkin_event_type", None)
        if pin is not None:
            self.stdout.write(f"Pinned default : {pin.name!r} (unchanged)")
        else:
            verb = "pinned" if apply else "would pin"
            self.stdout.write(f"Pinned default : {verb} {retail.name!r} [{retail.id}]")
        if apply:
            if pin is None:
                tenant.checkin_event_type = retail
                tenant.save(update_fields=["checkin_event_type"])
            tenant.checkin_event_types.set(wanted)
