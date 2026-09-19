"""Dollar spend fields on walk-up recaps.

A whole number with no period (2489) is rejected. 24.89, 24.8, and 2489.00
are stored as a numeric dollar string. This does not rewrite rows that are
not part of the current submit.
"""

from __future__ import annotations

import re

SPEND_CENTS_MESSAGE = (
    "Enter dollars and cents, like 24.89 "
    "(use 2489.00 if you really mean two thousand)."
)

_PHOTO_TYPE = re.compile(r"photo|image", re.IGNORECASE)
_PRODUCT_SPEND = re.compile(r"product\s*spend", re.IGNORECASE)
_AMOUNT = re.compile(r"\bamount\b", re.IGNORECASE)
_RECEIPT = re.compile(r"\breceipts?\b", re.IGNORECASE)
_USED = re.compile(r"\bused\b", re.IGNORECASE)
_SPEND_LABEL = re.compile(
    r"account\s*spend|spend\s*amount|amount\s*spent|total\s*spend|"
    r"corporate\s*card\s*spend",
    re.IGNORECASE,
)


class SpendAmountNeedsCents(Exception):
    """BA typed a spend figure with no decimal point."""


def is_spend_amount_field(name: str | None, type_name: str | None = "") -> bool:
    label = (name or "").strip()
    kind = (type_name or "").strip()
    if not label or _PHOTO_TYPE.search(kind):
        return False
    if "?" in label:
        return False
    if _RECEIPT.search(label) and not _AMOUNT.search(label):
        return False
    if _USED.search(label) and not _AMOUNT.search(label):
        return False
    if _PRODUCT_SPEND.search(label):
        return True
    return bool(_SPEND_LABEL.search(label))


def sanitize_spend_amount(raw: str | None) -> str:
    stripped = str(raw or "").replace("$", "").replace(",", "").replace(" ", "")
    out: list[str] = []
    seen_dot = False
    for ch in stripped:
        if ch.isdigit():
            out.append(ch)
            continue
        if ch == "." and not seen_dot:
            seen_dot = True
            out.append(ch)
    return "".join(out)


def _parsed_dollars(cleaned: str) -> float | None:
    if not cleaned or cleaned == "." or "." not in cleaned:
        return None
    if not re.fullmatch(r"\d*\.\d*", cleaned):
        return None
    try:
        amount = float(cleaned)
    except ValueError:
        return None
    if amount < 0 or amount != amount:  # NaN
        return None
    return amount


def spend_amount_error(raw: str | None) -> str | None:
    trimmed = str(raw or "").strip()
    if not trimmed or trimmed == "$":
        return None
    cleaned = sanitize_spend_amount(trimmed)
    if not cleaned or cleaned == "." or "." not in cleaned:
        return SPEND_CENTS_MESSAGE
    if _parsed_dollars(cleaned) is None:
        return SPEND_CENTS_MESSAGE
    return None


def spend_amount_stored(raw: str | None) -> str | None:
    trimmed = str(raw or "").strip()
    if not trimmed or trimmed == "$":
        return None
    if spend_amount_error(trimmed):
        return None
    amount = _parsed_dollars(sanitize_spend_amount(trimmed))
    if amount is None:
        return None
    return f"{amount:.2f}"


def guard_spend_amount(
    name: str | None,
    type_name: str | None,
    value,
    *,
    previous=None,
) -> str:
    """Value to persist. Raises SpendAmountNeedsCents when a new or changed
    spend figure has no decimal. An unchanged legacy whole number is kept."""
    text = "" if value is None else str(value)
    if not is_spend_amount_field(name, type_name):
        return text
    if previous is not None and sanitize_spend_amount(text) == sanitize_spend_amount(
        str(previous)
    ):
        return text
    err = spend_amount_error(text)
    if err:
        label = (name or "Spend amount").strip() or "Spend amount"
        raise SpendAmountNeedsCents(f"{label}: {err}")
    stored = spend_amount_stored(text)
    if stored is None:
        return text
    return stored
