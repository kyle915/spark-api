"""Filed-data blocks for Ask AI scoped to one recap or one event.

``tenantAiAnswer`` stays the only model call. When the caller passes a
recap id or an event id, this module builds the plaintext the model is
allowed to see: that record's fields, notes, stored KPIs, and photo
metadata. Other stores' recaps never enter the block.

``client_safe`` builds the block for a brand user instead of Ignite staff:
a recap that is not approved, is archived, or sits under a deleted request
is not found; an event block holds approved recaps only; and no ambassador
name is included (the BA line is dropped, name fields are skipped, and a
known ambassador's name written into free text is replaced).
"""

from __future__ import annotations

import base64
import re
import uuid
from dataclasses import dataclass

from django.db.models import Q

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


def build_recap_scope(
    tenant_id: int, raw_id: str, *, client_safe: bool = False
) -> ScopeBlock | None:
    """One recap that belongs to ``tenant_id``, or None when it does not.

    With ``client_safe`` a recap the client may not see is None as well.
    """
    recap = _load_recap(tenant_id, raw_id, client_safe=client_safe)
    if recap is None:
        return None
    if isinstance(recap, CustomRecap):
        block = _custom_block(recap, client_safe=client_safe)
    else:
        block = _legacy_block(recap, client_safe=client_safe)
    if client_safe:
        block = _without_names(block, _ba_names([recap]))
    return block


def build_event_scope(
    tenant_id: int, raw_id: str, *, client_safe: bool = False
) -> ScopeBlock | None:
    """One event plus the recaps filed on it, or None when out of scope."""
    event = _load_event(tenant_id, raw_id, client_safe=client_safe)
    if event is None:
        return None
    return _event_block(event, client_safe=client_safe)


# The Request an event hangs off was not soft-deleted (standalone events,
# with no request, are kept). ``prefix`` is the ORM path to the Event.
def _live_request_q(prefix: str = "") -> Q:
    return Q(**{f"{prefix}request__isnull": True}) | Q(
        **{f"{prefix}request__deleted_at__isnull": True}
    )


def _client_visible(queryset, event_prefix: str = "event__"):
    """Recaps a client may be shown: approved, not archived, request live."""
    return queryset.filter(
        _live_request_q(event_prefix), approved=True, archived_at__isnull=True
    )


def _load_recap(tenant_id: int, raw_id: str, *, client_safe: bool = False):
    type_name, token = _split_id(raw_id)
    if not token:
        return None
    want_custom = type_name in {"CustomRecap", "customrecap"}
    want_legacy = type_name in {"Recap", "recap"}
    if want_custom or type_name is None:
        custom = CustomRecap.objects.filter(tenant_id=tenant_id)
        if client_safe:
            custom = _client_visible(custom)
        found = _one(custom, token)
        if found is not None:
            return found
    if want_legacy or type_name is None:
        legacy = Recap.objects.filter(event__tenant_id=tenant_id)
        if client_safe:
            legacy = _client_visible(legacy)
        return _one(legacy, token)
    return None


def _load_event(
    tenant_id: int, raw_id: str, *, client_safe: bool = False
) -> Event | None:
    _type_name, token = _split_id(raw_id)
    if not token:
        return None
    events = Event.objects.filter(tenant_id=tenant_id)
    if client_safe:
        events = events.filter(_live_request_q())
    return _one(events, token)


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


def _ba_names(recaps, event=None) -> list[str]:
    """Every name an ambassador on these recaps (or booked on ``event``) goes by."""
    names: set[str] = set()
    for recap in recaps:
        name = _ba_name(recap)
        if name:
            names.add(name)
        external = (getattr(recap, "external_ba_name", None) or "").strip()
        if external:
            names.add(external)
    if event is not None:
        from ambassadors.models import AmbassadorEvent

        booked = AmbassadorEvent.objects.filter(event_id=event.id).values_list(
            "ambassador__user__first_name", "ambassador__user__last_name"
        )
        for first, last in booked:
            name = " ".join(part for part in (first, last) if part).strip()
            if name:
                names.add(name)
    # Longest first, so "Jordan Rivera Jr" goes before "Jordan Rivera".
    return sorted(names, key=len, reverse=True)


_BA_PLACEHOLDER = "the brand ambassador"

# A recap form field that asks who filed it ("BA Name", "Ambassador name",
# "Your name", "Full name"). Its value is a person's name, never a metric.
_BA_NAME_FIELD_RE = re.compile(
    r"\b(ba|brand\s+ambassador|ambassador)s?\b.*\bname\b"
    r"|\bname\b.*\b(ba|brand\s+ambassador|ambassador)s?\b"
    r"|^\s*(your|full|first|last)?\s*name\s*$",
    re.IGNORECASE,
)


def _hide_names(text: str, names: list[str]) -> str:
    """Replace each full name wherever it appears in ``text``.

    Only names of two or more words are replaced. A lone first name ("Sam",
    "May") would also hit store names and ordinary words, so it is left
    alone; the BA line and the name fields are dropped separately.
    """
    for name in names:
        if len(name.split()) < 2:
            continue
        pattern = r"(?<!\w)" + r"\s+".join(map(re.escape, name.split())) + r"(?!\w)"
        text = re.sub(pattern, _BA_PLACEHOLDER, text, flags=re.IGNORECASE)
    return text


def _without_names(block: ScopeBlock, names: list[str]) -> ScopeBlock:
    if not names:
        return block
    return ScopeBlock(
        text=_hide_names(block.text, names),
        has_filed_data=block.has_filed_data,
        kind=block.kind,
    )


def _store_name(recap) -> str:
    retailer = getattr(recap, "retailer", None)
    if retailer is not None and getattr(retailer, "name", None):
        return str(retailer.name)
    return str(getattr(recap, "typed_store_name", "") or "")


def _header(kind: str, recap, event, *, client_safe: bool = False) -> list[str]:
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
        ("BA", "" if client_safe else _ba_name(recap)),
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


def _legacy_block(recap: Recap, *, client_safe: bool = False) -> ScopeBlock:
    event = recap.event
    lines = _header("single recap (legacy)", recap, event, client_safe=client_safe)
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


def _custom_block(recap: CustomRecap, *, client_safe: bool = False) -> ScopeBlock:
    event = recap.event
    lines = _header("single recap (custom)", recap, event, client_safe=client_safe)
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
        if client_safe and _BA_NAME_FIELD_RE.search(label):
            continue
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


def _event_block(event: Event, *, client_safe: bool = False) -> ScopeBlock:
    block = _event_block_text(event, client_safe=client_safe)
    if not client_safe:
        return block
    # Names from every recap on the event, shown or not, and everyone booked
    # on it: a needs-review recap's ambassador can still be named in notes.
    everyone = list(Recap.objects.filter(event_id=event.id).select_related(
        "ambassador__user"
    )) + list(
        CustomRecap.objects.filter(event_id=event.id).select_related(
            "ambassador__user"
        )
    )
    return _without_names(block, _ba_names(everyone, event))


def _event_block_text(event: Event, *, client_safe: bool = False) -> ScopeBlock:
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
    legacy_qs = Recap.objects.filter(event_id=event.id)
    custom_qs = CustomRecap.objects.filter(event_id=event.id)
    if client_safe:
        # A brand user's answer is written from approved recaps only.
        legacy_qs = _client_visible(legacy_qs)
        custom_qs = _client_visible(custom_qs)
    legacy = list(legacy_qs.order_by("id")[: MAX_EVENT_RECAPS + 1])
    custom = list(custom_qs.order_by("id")[: MAX_EVENT_RECAPS + 1])
    blocks = [
        _legacy_block(recap, client_safe=client_safe)
        for recap in legacy[:MAX_EVENT_RECAPS]
    ]
    blocks.extend(
        _custom_block(recap, client_safe=client_safe)
        for recap in custom[:MAX_EVENT_RECAPS]
    )
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
