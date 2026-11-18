"""No-product visits: recaps a deck leaves out of its demo count.

Kyle's standing rule: a visit where the store had no product (or the
ambassador had none and the demo was called off) is not a demo. It is filed
so the ambassador gets paid, it records no tasters and no sales, and counted
as a demo it drags every per-demo figure down. Leave it out, and say so.

What this module does, per approved recap in the window (the same recaps the
dashboard counts):

* **Left out** — nothing was recorded (no consumers sampled, nothing sold,
  no samples handed out, no engagements) *and* either the notes say there
  was no product, or there are no notes at all. A blank zero recap is left
  out because that is what it nearly always is; staff can count it back in
  (a blank recap whose photos show product on the shelf stays in).
* **Worth a look** — kept in the numbers, but listed for staff: a zero
  recap whose notes give some other reason (a manager refused the samples:
  not a no-product visit), or a recap with numbers whose notes still say
  there was no product.

Because a left-out visit recorded nothing, leaving it out never changes a
total: consumers sampled, products sold and the rest stay exactly the
dashboard's. Only the demo count, and the per-demo figure built on it,
move.

Bounded: at most :data:`MAX_SCAN_RECAPS` custom recaps are read, newest
event first. READ-ONLY. No ambassador name leaves this module; the evidence
line is cut from the recap's notes with names taken out.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date

from recaps import metric_mapping
from recaps.consumer_breakdown import _numbered, _store
from recaps.models import ConsumerEngagements, CustomFieldValue, CustomRecap, Recap
from recaps.numbers import parse_count
from recaps.scoped_ai import _hide_names
from recaps.tenant_overview import (
    _approved_only,
    _event_date_expr,
    _filter_event_window,
    _inclusive_dates_to_window,
    _kpi_gate,
)
from recaps.types import (
    _BA_NAME_FIELD_RE,
    _consumers_sampled_from_fields,
    _people_engaged_from_fields,
    _samples_given_from_fields,
    _sold_units_from_fields,
)

MAX_SCAN_RECAPS = 5000
# Rows kept in the stored facts; the counts are always exact.
MAX_LISTED_EXCLUDED = 300
MAX_LISTED_CHECK = 60
EVIDENCE_PAD = 70

REASON_NO_PRODUCT = "no_product"
REASON_NOTHING_RECORDED = "nothing_recorded"
REASON_CHECK_ZERO = "zero_other_reason"
REASON_CHECK_NUMBERS = "numbers_but_no_product_note"

REASON_LABELS = {
    REASON_NO_PRODUCT: "No product on hand",
    REASON_NOTHING_RECORDED: "Nothing recorded",
    REASON_CHECK_ZERO: "Nothing recorded, other reason given",
    REASON_CHECK_NUMBERS: "Has numbers, but the notes say no product",
}

# Phrases ambassadors use for a visit with no product. Only ever read
# together with the recap's numbers: on its own "out of stock" can mean the
# product sold out, which is the opposite.
NO_PRODUCT_RE = re.compile(
    r"\bno\s+product"
    r"|\bnot?\s+(?:any\s+)?product\s+(?:on\s+hand|available|in\s+(?:the\s+)?store|on\s+(?:the\s+)?shel(?:f|ves))"
    r"|\bproduct\s+(?:was\s+|is\s+)?(?:not|never)\s+(?:there|delivered|available|in\s+stock|on\s+(?:the\s+)?shel(?:f|ves))"
    r"|\bout\s+of\s+stock"
    r"|\bnot\s+(?:carried|stocked)\b"
    r"|\bdo(?:es)?\s*n[o']?t\s+carry\b"
    r"|\bdiscontinued\b"
    r"|\bunapproved\b"
    r"|\bnot\s+(?:yet\s+)?approved\s+(?:to\s+sell|for\s+sale|in\s+(?:this|the)\s+store)"
    r"|\bdid\s*n[o']?t\s+have\s+(?:any\s+)?(?:product|samples|inventory)"
    r"|\bno\s+(?:inventory|stock)\b"
    r"|\bno\s+samples\s+(?:were\s+)?(?:available|delivered|provided)"
    r"|\bcould\s*(?:n[o']?t|\s+not)\s+(?:be\s+)?complet"
    r"|\bsent\s+(?:me\s+)?home\b",
    re.IGNORECASE,
)

_MEDIA_RE = re.compile(r"^https?://|\.(?:jpe?g|png|webp|gif|heic|heif|pdf|mov|mp4)(?:\?|$)", re.I)


@dataclass
class Visit:
    key: str
    day: str
    store: str
    state: str
    reason: str
    evidence: str = ""

    @property
    def label(self) -> str:
        return REASON_LABELS.get(self.reason, self.reason)

    def as_dict(self) -> dict:
        return {**asdict(self), "label": self.label}


def _evidence(text: str, names: list[str]) -> str:
    match = NO_PRODUCT_RE.search(text)
    if not match:
        snippet = text[: EVIDENCE_PAD * 2]
        cut = len(text) > len(snippet)
    else:
        start = max(0, match.start() - EVIDENCE_PAD)
        stop = min(len(text), match.end() + EVIDENCE_PAD)
        snippet = text[start:stop]
        snippet = ("…" if start else "") + snippet
        cut = stop < len(text)
    snippet = " ".join(_hide_names(snippet, names).split())
    return snippet + ("…" if cut else "")


def _is_note(name: str | None, value: str | None) -> bool:
    """A free-text answer worth reading: not a count, a link or a name."""
    text = (value or "").strip()
    if len(text) < 3 or _MEDIA_RE.search(text):
        return False
    if name and _BA_NAME_FIELD_RE.search(str(name)):
        return False
    if parse_count(text) is not None and len(text) <= 12:
        return False
    if text.lower() in ("yes", "no", "n/a", "na", "none", "true", "false"):
        return False
    return True


def _has_sentence(notes: list[str]) -> bool:
    """True when one answer reads like a note (four words or more), not a
    store name or a picked option."""
    return any(len(n.split()) >= 4 for n in notes)


def classify(*, activity: int, notes: str, has_sentence: bool | None = None) -> str | None:
    """The reason for one recap, or None when it is an ordinary demo.

    ``notes`` is every free-text answer joined; ``has_sentence`` says whether
    one of them is an actual note (defaults to "notes is not blank")."""
    says_no_product = bool(NO_PRODUCT_RE.search(notes or ""))
    if activity > 0:
        return REASON_CHECK_NUMBERS if says_no_product else None
    if says_no_product:
        return REASON_NO_PRODUCT
    if has_sentence is None:
        has_sentence = bool((notes or "").strip())
    if not has_sentence:
        return REASON_NOTHING_RECORDED
    return REASON_CHECK_ZERO


EXCLUDED_REASONS = frozenset({REASON_NO_PRODUCT, REASON_NOTHING_RECORDED})


def no_product_visits(
    tenant_id: int, start: date | None, end: date | None, names: list[str] | None = None
) -> dict:
    """``{"excluded": [...], "check": [...], "excluded_count", "check_count",
    "scanned", "capped"}`` for a tenant and inclusive date range.

    Each listed visit is a dict: ``key`` ("C<id>" custom / "L<id>" legacy),
    ``day``, ``store``, ``state``, ``reason``, ``label`` and ``evidence``.
    """
    from events.retailer_aliases import accepted_alias_map
    from events.routing import extract_state_code

    names = names or []
    window = _inclusive_dates_to_window(start, end)
    aliases = accepted_alias_map(tenant_id)
    excluded: list[Visit] = []
    check: list[Visit] = []

    # ── Custom recaps ────────────────────────────────────────────────
    custom = (
        _approved_only(
            _filter_event_window(CustomRecap.objects.filter(tenant_id=tenant_id), "event__", window),
            "",
        )
        .annotate(_day=_event_date_expr("event__"))
        .order_by("-_day", "-id")
    )
    total_custom = custom.count()
    rows = list(
        custom.values_list(
            "id",
            "_day",
            "total_engagements",
            "retailer__name",
            "store_number",
            "event__retailer__name",
            "event__request__retailer__name",
            "event__request__retailer_name",
            "event__state__code",
            "state__code",
            "event__address",
        )[:MAX_SCAN_RECAPS]
    )
    placed: dict[int, dict] = {}
    for (
        recap_id,
        day,
        engagements,
        recap_retailer,
        store_number,
        retailer,
        req_retailer,
        req_name,
        state,
        recap_state,
        address,
    ) in rows:
        placed[recap_id] = {
            "day": day.date().isoformat() if day else "",
            "engagements": int(engagements or 0),
            "store": _store(
                _numbered(recap_retailer, store_number),
                retailer,
                req_retailer,
                req_name,
                aliases=aliases,
            ),
            "state": state or recap_state or extract_state_code(address) or "",
        }

    _gate, confirmed = _kpi_gate(tenant_id)
    pairs: dict[int, list] = {}
    notes: dict[int, list[str]] = {}
    ids = list(placed)
    for chunk_start in range(0, len(ids), 1000):
        chunk = ids[chunk_start : chunk_start + 1000]
        values = CustomFieldValue.objects.filter(custom_recap_id__in=chunk).values_list(
            "custom_recap_id", "custom_field_id", "custom_field__name", "value"
        )
        for recap_id, field_id, name, value in values.iterator():
            pairs.setdefault(recap_id, []).append(
                (metric_mapping.tag_name(name, field_id, confirmed), value)
            )
            if _is_note(name, value):
                notes.setdefault(recap_id, []).append(" ".join(str(value).split()))

    for recap_id, where in placed.items():
        fields = pairs.get(recap_id, [])
        activity = max(
            int(_consumers_sampled_from_fields(fields) or 0),
            int(_sold_units_from_fields(fields) or 0),
            int(_samples_given_from_fields(fields) or 0),
            int(_people_engaged_from_fields(fields) or 0),
            where["engagements"],
        )
        answers = notes.get(recap_id, [])
        text = " · ".join(answers)
        reason = classify(activity=activity, notes=text, has_sentence=_has_sentence(answers))
        if reason is None:
            continue
        visit = Visit(
            key=f"C{recap_id}",
            day=where["day"],
            store=where["store"],
            state=(where["state"] or "").upper(),
            reason=reason,
            evidence=_evidence(text, names) if text else "",
        )
        (excluded if reason in EXCLUDED_REASONS else check).append(visit)

    # ── Legacy recaps: numbers only, no free text to read ───────────
    legacy = (
        _approved_only(
            _filter_event_window(Recap.objects.filter(event__tenant_id=tenant_id), "event__", window),
            "",
        )
        .annotate(_day=_event_date_expr("event__"))
        .order_by("-_day", "-id")
    )
    legacy_rows = list(
        legacy.values_list(
            "id",
            "_day",
            "total_engagements",
            "products_sold",
            "total_cans_sold",
            "total_packs_sold",
            "event__retailer__name",
            "event__request__retailer__name",
            "event__request__retailer_name",
            "event__state__code",
            "event__address",
        )[:MAX_SCAN_RECAPS]
    )
    if legacy_rows:
        consumers: dict[int, int] = {}
        for recap_id, count in ConsumerEngagements.objects.filter(
            recap_id__in=[r[0] for r in legacy_rows]
        ).values_list("recap_id", "total_consumer"):
            consumers[recap_id] = consumers.get(recap_id, 0) + int(count or 0)
        for (
            recap_id,
            day,
            engagements,
            sold,
            cans,
            packs,
            retailer,
            req_retailer,
            req_name,
            state,
            address,
        ) in legacy_rows:
            activity = max(
                consumers.get(recap_id, 0),
                int(engagements or 0),
                int(sold or 0),
                int(cans or 0),
                int(packs or 0),
            )
            if activity > 0:
                continue
            excluded.append(
                Visit(
                    key=f"L{recap_id}",
                    day=day.date().isoformat() if day else "",
                    store=_store(retailer, req_retailer, req_name, aliases=aliases),
                    state=(state or extract_state_code(address) or "").upper(),
                    reason=REASON_NOTHING_RECORDED,
                )
            )

    excluded.sort(key=lambda v: (v.day, v.key), reverse=True)
    check.sort(key=lambda v: (v.day, v.key), reverse=True)
    return {
        "excluded": [v.as_dict() for v in excluded[:MAX_LISTED_EXCLUDED]],
        "check": [v.as_dict() for v in check[:MAX_LISTED_CHECK]],
        "excluded_count": len(excluded),
        "check_count": len(check),
        "scanned": len(rows) + len(legacy_rows),
        "capped": total_custom > MAX_SCAN_RECAPS or legacy.count() > MAX_SCAN_RECAPS,
    }


def excluded_keys(visits: dict | None, kept: list[str] | None = None) -> set[str]:
    """The visit keys left out, after the ones staff counted back in."""
    kept_set = {str(k) for k in kept or []}
    return {
        str(v.get("key"))
        for v in (visits or {}).get("excluded") or []
        if v.get("key") and str(v.get("key")) not in kept_set
    }


def excluded_custom_ids(visits: dict | None, kept: list[str] | None = None) -> set[int]:
    return {
        int(key[1:])
        for key in excluded_keys(visits, kept)
        if key.startswith("C") and key[1:].isdigit()
    }
