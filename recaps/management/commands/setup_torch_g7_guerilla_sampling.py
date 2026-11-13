"""Torch THC: G7 Guerilla Sampling on the BA walk-up.

Adds **G7 Guerilla Sampling** to ``/checkin/TH-2HRV3D`` (code never reminted)
and seeds the recap of the same name. Questions follow the Torch Sampling
recap doc. The Test Group block is left off. Photos are a Photo Recap
dropzone (at least one) plus an optional Parking Receipt.

Retail, Event Activation, Guerilla Activation, Product Seeding, and the
agency link stay as they are. This program keys on a venue address, not a
store number, and stays out of retail conversion.

Idempotent. DRY-RUN by default; ``--apply`` writes. Run on prod via
``/internal/cron/setup-torch-g7-guerilla-sampling``.
"""

from __future__ import annotations

import re

from recaps.management.commands.setup_torch_event_activation import (
    TENANT_FORM_SLUG,
    TENANT_SLUG,
    CHECKIN_CODE,
    Field,
    Command as EventActivationCommand,
)

EVENT_LABEL = "G7 Guerilla Sampling"
TEMPLATE_NAME = "G7 Guerilla Sampling"
PICKER_LABEL = "G7 Guerilla Sampling"
PICKER_DESCRIPTION = (
    "G7 street-team sampling. Sidewalks, nearby events, bars, and tailgates."
)

PHOTO_BUCKETS: list[dict] = [
    {
        "name": "Photo Recap",
        "helper": "Upload your sampling photos",
        "min": 1,
    },
    {"name": "Parking Receipt"},
]

# (name, kind, required, options, placeholder) — the doc's questions, in order.
# Test Group is intentionally absent.
SPEC_FIELDS: list[tuple[str, list[Field]]] = [
    (
        "Shift Details",
        [
            ("Your Name", "text", True, [], ""),
            (
                "Date & Time slot you sampled",
                "text",
                True,
                [],
                "9/18 from 3 PM - 5 PM",
            ),
            (
                "Where did you sample?",
                "longtext",
                True,
                [],
                "Address, and whether it was a street roam, nearby event, "
                "or a specific location (restaurant, bar, tailgate, etc.)",
            ),
        ],
    ),
    (
        "Sampling",
        [
            (
                "Who accepted samples?",
                "longtext",
                True,
                [],
                "Male, female, age range (best guess, must be 21+)",
            ),
            ("TOTAL - how many cans did you give away?", "number", True, [], ""),
            (
                "# of samples for each flavor",
                "longtext",
                True,
                [],
                "e.g. Mango 12, Lime 8",
            ),
            ("How many QR Code Scans", "number", True, [], ""),
            (
                "Koozies Distributed (total out of 25)",
                "number",
                True,
                [],
                "Out of 25",
            ),
            (
                "Number of people asking where they could buy the product?",
                "number",
                True,
                [],
                "",
            ),
        ],
    ),
    (
        "Feedback",
        [
            ("What did people like about the product?", "longtext", True, [], ""),
            ("What did people not like about the product?", "longtext", True, [], ""),
            (
                "Consumer feedback (provide 2-3 comments consumers are saying)",
                "longtext",
                True,
                [],
                "",
            ),
            (
                "Additional notes/feedback (include 2-3 notes about the "
                "sampling location/crowds/product)",
                "longtext",
                True,
                [],
                "",
            ),
            (
                "Top competitors in the market? Any competitors mentioned?",
                "longtext",
                True,
                [],
                "",
            ),
            (
                "Are any local businesses interested in supplying Torch? "
                "Include their name, address, and number",
                "longtext",
                False,
                [],
                "",
            ),
        ],
    ),
]

SECTION_ORDER = {
    "Shift Details": 0,
    "Sampling": 1,
    "Feedback": 2,
}

OPTIONAL_FIELDS = frozenset(
    {
        "Are any local businesses interested in supplying Torch? "
        "Include their name, address, and number",
    }
)


def _program_key(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


class Command(EventActivationCommand):
    help = (
        "Torch THC: add G7 Guerilla Sampling to the TH-2HRV3D walk-up and "
        "seed its recap (dry-run by default; --apply to write)."
    )

    event_label = EVENT_LABEL
    tenant_slug = TENANT_SLUG
    tenant_form_slug = TENANT_FORM_SLUG
    checkin_code = CHECKIN_CODE
    template_name = TEMPLATE_NAME
    brand_name = "Torch"
    activation_buckets = PHOTO_BUCKETS
    sample_qty_layout: dict = {}
    section_order = SECTION_ORDER
    include_product_samples = False
    field_conditions: dict = {}

    def build_spec(self, product_opts: list[str]) -> list[tuple[str, list[Field]]]:
        return [(name, list(fields)) for name, fields in SPEC_FIELDS]

    def _add_to_picker(self, tenant, activation, apply: bool) -> None:
        super()._add_to_picker(tenant, activation, apply)
        cfg = getattr(tenant, "checkin_program_picker", None)
        if not isinstance(cfg, dict):
            self.stdout.write(
                "  picker copy : none set — the program shows under its own name"
            )
            return
        options = [row for row in (cfg.get("options") or []) if isinstance(row, dict)]
        key = _program_key(self.event_label)
        if any(_program_key(row.get("eventType")) == key for row in options):
            self.stdout.write("  picker copy : already listed")
            return
        options.append(
            {
                "eventType": self.event_label,
                "label": PICKER_LABEL,
                "description": PICKER_DESCRIPTION,
            }
        )
        verb = "added" if apply else "would add"
        self.stdout.write(f"  picker copy : {verb} {PICKER_LABEL!r}")
        if apply:
            tenant.checkin_program_picker = {**cfg, "options": options}
            tenant.save(update_fields=["checkin_program_picker"])
