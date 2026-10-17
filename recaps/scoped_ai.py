"""Filed-data blocks for Ask AI scoped to one recap or one event.

``tenantAiAnswer`` stays the only model call. When the caller passes a
recap id or an event id, this module builds the plaintext the model is
allowed to see: that record's fields, notes, stored KPIs, and photo
metadata. Other stores' recaps never enter the block.
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass

from events.models import Event
from recaps.models import CustomRecap, Recap

MAX_VALUE_CHARS = 400
MAX_FIELDS = 80
MAX_PHOTOS = 40
MAX_EVENT_RECAPS = 12


@dataclass(frozen=True)
class ScopeBlock:
    """Prompt text plus whether any filed content was found."""

    text: str
    has_filed_data: bool
    kind: str


def build_recap_scope(tenant_id: int, raw_id: str) -> ScopeBlock | None:
    """One recap that belongs to ``tenant_id``, or None when it does not."""
    recap = _load_recap(tenant_id, raw_id)
    if recap is None:
        return None
    if isinstance(recap, CustomRecap):
        return _custom_block(recap)
    return _legacy_block(recap)


def build_event_scope(tenant_id: int, raw_id: str) -> ScopeBlock | None:
    """One event plus the recaps filed on it, or None when out of scope."""
    event = _load_event(tenant_id, raw_id)
    if event is None:
        return None
    return _event_block(event)


def _load_recap(tenant_id: int, raw_id: str):
    type_name, token = _split_id(raw_id)
    if not token:
        return None
    want_custom = type_name in {"CustomRecap", "customrecap"}
    want_legacy = type_name in {"Recap", "recap"}
    if want_custom or type_name is None:
        found = _one(CustomRecap.objects.filter(tenant_id=tenant_id), token)
        if found is not None:
            return found
    if want_legacy or type_name is None:
        return _one(
            Recap.objects.filter(event__tenant_id=tenant_id),
            token,
        )
    return None


def _load_event(tenant_id: int, raw_id: str) -> Event | None:
    _type_name, token = _split_id(raw_id)
    if not token:
        return None
    return _one(Event.objects.filter(tenant_id=tenant_id), token)


def _one(queryset, token: str):
    try:
        uuid.UUID(token)
        return queryset.filter(uuid=token).first()
    except (ValueError, AttributeError, TypeError):
        pass
    if token.isdigit():
        return queryset.filter(id=int(token)).first()
    return None


def _split_id(raw: str) -> tuple[str | None, str]:
    """Relay global id, numeric pk, or uuid → ``(type name or None, token)``."""
    text = (raw or "").strip()
    if not text:
        return None, ""
    try:
        uuid.UUID(text)
        return None, text
    except (ValueError, AttributeError, TypeError):
        pass
    if text.isdigit():
        return None, text
    try:
        decoded = base64.b64decode(text, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None, text
    if ":" not in decoded:
        return None, text
    type_name, db_id = decoded.split(":", 1)
    return type_name, db_id.strip()


def _clip(value) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= MAX_VALUE_CHARS:
        return text
    return text[: MAX_VALUE_CHARS - 1] + "…"


def _line(label: str, value) -> str | None:
    text = _clip(value)
    if not text:
        return None
    return f"{label}: {text}"


def _ba_name(recap) -> str:
    ambassador = getattr(recap, "ambassador", None)
    user = getattr(ambassador, "user", None) if ambassador is not None else None
    if user is not None:
        name = " ".join(
            part
            for part in (getattr(user, "first_name", ""), getattr(user, "last_name", ""))
            if part
        ).strip()
        if name:
            return name
    external = (getattr(recap, "external_ba_name", None) or "").strip()
    return external


def _store_name(recap) -> str:
    retailer = getattr(recap, "retailer", None)
    if retailer is not None and getattr(retailer, "name", None):
        return str(retailer.name)
    return str(getattr(recap, "typed_store_name", "") or "")


def _header(kind: str, recap, event) -> list[str]:
    lines = [
        f"SCOPE: {kind}",
        "Answer only from the lines below. Other stores and other recaps are not included.",
    ]
    for label, value in (
        ("RECAP", recap.name),
        ("STORE", _store_name(recap)),
        ("ADDRESS", getattr(recap, "typed_store_address", None) or getattr(event, "address", None)),
        ("EVENT", getattr(event, "name", None)),
        ("EVENT DATE", getattr(event, "date", None)),
        ("BA", _ba_name(recap)),
        ("STATUS", "approved" if recap.approved else "needs review"),
    ):
        line = _line(label, value)
        if line:
            lines.append(line)
    return lines


def _kpi_lines(pairs: list[tuple[str, object]]) -> list[str]:
    lines = ["STORED KPIs:"]
    any_value = False
    for label, value in pairs:
        if value is None or value == "":
            continue
        any_value = True
        lines.append(f"- {label}: {value}")
    if not any_value:
        return []
    return lines


def _has_filed(content: list[str], photos: list) -> bool:
    """True when the block has a KPI, note, field, or photo — not the empty photo line."""
    if photos:
        return True
    for line in content:
        if line.startswith("PHOTOS:"):
            continue
        if line.strip():
            return True
    return False


def _photo_lines(files) -> list[str]:
    rows = []
    for photo in files[:MAX_PHOTOS]:
        category = getattr(photo, "file_recap_category", None)
        category_name = getattr(category, "name", None) or "uncategorized"
        name = getattr(photo, "name", None) or "untitled"
        rows.append(f"- {category_name} | {_clip(name)}")
    if not rows:
        return ["PHOTOS: none filed. Image pixels are not available."]
    lines = [
        "PHOTOS (metadata only — filenames and categories, not image pixels):",
        *rows,
    ]
    if len(list(files)) > MAX_PHOTOS:
        lines.append(f"TRUNCATED: {len(list(files)) - MAX_PHOTOS} more photos omitted.")
    return lines


def _legacy_block(recap: Recap) -> ScopeBlock:
    event = recap.event
    lines = _header("single recap (legacy)", recap, event)
    content: list[str] = []
    content.extend(
        _kpi_lines(
            [
                ("total_engagements", recap.total_engagements),
                ("products_sold", recap.products_sold),
                ("total_cans_sold", recap.total_cans_sold),
                ("total_packs_sold", recap.total_packs_sold),
                ("account_spend_amount", recap.account_spend_amount),
            ]
        )
    )
    for label, value in (
        ("traffic_description", recap.traffic_description),
        ("competitive_presence", recap.competitive_presence),
    ):
        line = _line(label, value)
        if line:
            content.append(line)
    for engagement in recap.consumer_engagements.all()[:5]:
        content.extend(
            _kpi_lines(
                [
                    ("consumers", engagement.total_consumer),
                    ("first_time_consumers", engagement.first_time_consumers),
                    ("brand_aware_consumers", engagement.brand_aware_consumers),
                    ("willing_to_purchase", engagement.willing_to_purchase_consumers),
                ]
            )
        )
    for sample in recap.product_samples.select_related("product").all()[:MAX_FIELDS]:
        product = getattr(sample, "product", None)
        content.append(
            f"- product sample: {getattr(product, 'name', 'product')} × {sample.quantity}"
        )
    notes = []
    for feedback in recap.consumer_feedback.all()[:20]:
        for label, value in (
            ("demographics", feedback.demographics),
            ("feedback", feedback.feedback),
            ("quotes", feedback.quotes),
            ("positive_stories", feedback.positive_stories),
            ("reasons_to_decline", feedback.reasons_to_decline),
        ):
            line = _line(label, value)
            if line:
                notes.append(line)
    for account in recap.account_feedback.all()[:10]:
        for label, value in (
            ("account_feedback", account.feedback),
            ("do_differently", account.do_differently_feedback),
        ):
            line = _line(label, value)
            if line:
                notes.append(line)
    if getattr(event, "notes", None):
        line = _line("event_notes", event.notes)
        if line:
            notes.append(line)
    if notes:
        content.append("NOTES:")
        content.extend(notes[:MAX_FIELDS])
    photos = list(
        recap.recap_files.select_related("file_recap_category").all()[: MAX_PHOTOS + 1]
    )
    content.extend(_photo_lines(photos))
    lines.extend(content)
    return ScopeBlock(
        text="\n".join(lines),
        has_filed_data=_has_filed(content, photos),
        kind="recap",
    )


def _custom_block(recap: CustomRecap) -> ScopeBlock:
    event = recap.event
    lines = _header("single recap (custom)", recap, event)
    content: list[str] = []
    content.extend(_kpi_lines([("total_engagements", recap.total_engagements)]))
    flags = (recap.data_quality_flags or "").strip()
    if flags:
        content.append(f"STORED DATA-QUALITY FLAGS: {_clip(flags)}")
    values = list(
        recap.custom_field_value.select_related("custom_field").all()[: MAX_FIELDS + 1]
    )
    field_lines = []
    for row in values[:MAX_FIELDS]:
        label = getattr(row.custom_field, "name", None) or "field"
        text = _clip(row.value)
        if text:
            field_lines.append(f"- {label}: {text}")
    if field_lines:
        content.append("FIELDS:")
        content.extend(field_lines)
    if len(values) > MAX_FIELDS:
        content.append("TRUNCATED: additional fields omitted.")
    for sample in recap.custom_recap_product_sample.select_related("product").all()[:40]:
        product = getattr(sample, "product", None)
        content.append(
            f"- product sample: {getattr(product, 'name', 'product')} × {sample.quantity}"
        )
    if getattr(event, "notes", None):
        line = _line("event_notes", event.notes)
        if line:
            content.append(line)
    photos = list(
        recap.custom_recap_files.select_related("file_recap_category").all()[
            : MAX_PHOTOS + 1
        ]
    )
    content.extend(_photo_lines(photos))
    lines.extend(content)
    return ScopeBlock(
        text="\n".join(lines),
        has_filed_data=_has_filed(content, photos),
        kind="recap",
    )


def _event_block(event: Event) -> ScopeBlock:
    lines = [
        "SCOPE: single event",
        "Answer only from this event's filed recaps, notes, and photo metadata.",
        "Do not use other events or other stores.",
    ]
    for label, value in (
        ("EVENT", event.name),
        ("DATE", event.date),
        ("ADDRESS", event.address),
        ("NOTES", event.notes),
    ):
        line = _line(label, value)
        if line:
            lines.append(line)
    request = event.request
    if request is not None:
        request_type = getattr(request, "request_type", None)
        for label, value in (
            ("REQUEST", getattr(request, "name", None)),
            ("REQUEST TYPE", getattr(request_type, "name", None)),
            ("REQUEST NOTES", getattr(request, "notes", None)),
            ("REQUEST ADDRESS", getattr(request, "address", None)),
        ):
            line = _line(label, value)
            if line:
                lines.append(line)
    legacy = list(Recap.objects.filter(event_id=event.id).order_by("id")[: MAX_EVENT_RECAPS + 1])
    custom = list(
        CustomRecap.objects.filter(event_id=event.id).order_by("id")[: MAX_EVENT_RECAPS + 1]
    )
    blocks = [_legacy_block(recap) for recap in legacy[:MAX_EVENT_RECAPS]]
    blocks.extend(_custom_block(recap) for recap in custom[:MAX_EVENT_RECAPS])
    omitted = max(0, len(legacy) - MAX_EVENT_RECAPS) + max(0, len(custom) - MAX_EVENT_RECAPS)
    if omitted:
        lines.append(
            f"TRUNCATED: {omitted} additional recaps on this event were omitted. "
            "Say so instead of guessing their contents."
        )
    if not blocks:
        lines.append("RECAPS: none filed on this event.")
        return ScopeBlock(text="\n".join(lines), has_filed_data=bool(event.notes or (request and getattr(request, "notes", None))), kind="event")
    filed = False
    for block in blocks:
        lines.append("---")
        lines.append(block.text)
        filed = filed or block.has_filed_data
    return ScopeBlock(text="\n".join(lines), has_filed_data=filed or bool(event.notes), kind="event")
