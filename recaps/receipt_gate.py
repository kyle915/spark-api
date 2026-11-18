"""Refuse a BA's recap that claims product spend with no receipt photo.

The admin review already flags this after the fact (``spend_without_receipt``
in ``recaps.recap_review_checks``). By then the BA has gone home and the
receipt is in a bin, so the flag can only start a chase. This gate stops the
recap at the moment the BA files it, while they can still take the photo.

Rules, all deliberately narrow:

* Spend comes from the template's typed dollar fields, picked by NAME with
  ``recaps.spend_amount.is_spend_amount_field`` — the same matcher the submit
  paths already use to validate dollars and cents, and the one the review
  flag reads. (The export's looser ``_account_spend_from_fields`` also matches
  a bare "spend" in any label, so a question like "How long did you spend
  setting up?" would refuse a recap here. A gate that refuses must not guess.)
* 0, blank or unparseable spend: no requirement.
* A receipt photo is any file on the recap filed under a receipt bucket
  (``RECEIPT_CATEGORY_RE``: receipt / product spend / expense).
* If the BA's form has no receipt bucket to put a photo in, there is nothing
  they could do to satisfy the rule, so it is skipped (and logged).
* Only BA submissions. Staff entering spend after the fact are not blocked;
  each caller decides that before asking.
"""

from __future__ import annotations

import logging
from typing import Callable, Iterable

from recaps.receipt_reader import RECEIPT_CATEGORY_RE
from recaps.spend_amount import (
    is_spend_amount_field,
    sanitize_spend_amount,
)

logger = logging.getLogger(__name__)

NEEDS_RECEIPT_CODE = "needs_receipt"


class RecapNeedsAReceipt(ValueError):
    """Spend was entered but the recap carries no receipt photo.

    The BA's to fix, so callers turn it into a 400 / a mutation message with
    ``message`` as the sentence to show."""

    def __init__(self, amount: float):
        self.amount = amount
        super().__init__(receipt_required_message(amount))

    @property
    def message(self) -> str:
        return receipt_required_message(self.amount)


def receipt_required_message(amount: float) -> str:
    return (
        "Add a photo of your receipt — you entered "
        f"${amount:,.2f} in product spend."
    )


def _money(value) -> float | None:
    cleaned = sanitize_spend_amount(None if value is None else str(value))
    if not cleaned or cleaned == ".":
        return None
    try:
        amount = float(cleaned)
    except ValueError:
        return None
    if amount != amount or amount <= 0:  # NaN / zero / negative
        return None
    return amount


def spend_total(fields: Iterable[tuple[str | None, str | None, object]]) -> float:
    """Sum of every positive typed spend amount. ``fields`` is
    ``(field name, field type name, value)``. 0.0 when there is none."""
    total = 0.0
    for name, type_name, value in fields:
        if not is_spend_amount_field(name, type_name):
            continue
        amount = _money(value)
        if amount is not None:
            total += amount
    return round(total, 2)


def is_receipt_category(name: str | None) -> bool:
    return bool(name and RECEIPT_CATEGORY_RE.search(name))


def custom_recap_spend(recap) -> float:
    from recaps.models import CustomFieldValue

    rows = CustomFieldValue.objects.filter(custom_recap=recap).values_list(
        "custom_field__name", "custom_field__custom_field_type__name", "value"
    )
    return spend_total(rows)


def custom_recap_has_receipt(recap) -> bool:
    from recaps.models import CustomRecapFile

    return CustomRecapFile.objects.filter(
        custom_recap=recap,
        file_recap_category__name__iregex=RECEIPT_CATEGORY_RE.pattern,
    ).exists()


def tenant_has_receipt_bucket(tenant, event_type=None) -> bool:
    """Whether the logged-in recap forms (app / web) give this tenant's BAs a
    receipt slot: a configured receipt photo bucket, or a receipt-type file
    category on the tenant. Same test the review flag uses."""
    if tenant is None:
        return False
    try:
        from ambassadors.checkin_web import photo_bucket_specs

        for entry in photo_bucket_specs(tenant, event_type) or []:
            if isinstance(entry, dict) and is_receipt_category(
                str(entry.get("name") or "")
            ):
                return True
    except Exception:  # noqa: BLE001 — fall through to the category rows
        logger.exception("receipt gate: bucket lookup failed tenant=%s", tenant.pk)
    from recaps.models import FileRecapCategory

    return FileRecapCategory.objects.filter(
        tenant=tenant, name__iregex=RECEIPT_CATEGORY_RE.pattern
    ).exists()


def assert_custom_recap_has_receipt(
    recap, *, offers_receipt_bucket: bool | Callable[[], bool]
) -> None:
    """Raise ``RecapNeedsAReceipt`` when ``recap`` (as it now stands, inside
    the caller's transaction) has spend > 0 and no receipt photo.

    Checks what the recap ENDS UP with, so an edit of a recap that already
    has its receipt goes through without re-sending it. ``offers_receipt_bucket``
    may be a callable, evaluated only when spend is entered and no receipt is
    on the recap."""
    amount = custom_recap_spend(recap)
    if amount <= 0:
        return
    if custom_recap_has_receipt(recap):
        return
    if callable(offers_receipt_bucket):
        offers_receipt_bucket = offers_receipt_bucket()
    if not offers_receipt_bucket:
        logger.info(
            "receipt gate skipped: recap=%s tenant=%s has $%.2f spend but the "
            "form offers no receipt bucket",
            getattr(recap, "pk", None),
            getattr(recap, "tenant_id", None),
            amount,
        )
        return
    raise RecapNeedsAReceipt(amount)
