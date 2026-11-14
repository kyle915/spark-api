"""Suggest the retailer for a recap whose event has none.

Reports group recaps by retailer; an event with no retailer lands under
"Unknown". This module works out a *suggestion* from what Spark already
holds about the event, in this order:

1. the staffing sheet's store name for the event (``SheetShift``),
2. the store name typed on a recap (3rd-party link) or entered in a
   store-like recap field,
3. the event's own name and address text,
4. a Google Places lookup of the address (when a Maps key is set), and
5. the AI text model, asked to read the same evidence.

Steps 1-3 need no network: a known tenant retailer whose name appears in the
evidence wins outright. Nothing here changes an event, a recap or a retailer.
A suggestion is stored as a pending :class:`recaps.models.RetailerProposal`
and only :func:`apply_proposal` (called when staff accept it, on its own or
while approving the recap) writes the retailer.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

SOURCE_LABELS = {
    "sheet": "the staffing sheet",
    "typed": "the store typed on the recap",
    "field": "a recap answer",
    "event": "the event name",
    "address": "the event address",
    "places": "a Google Maps lookup of the address",
    "ai": "AI reading of the event details",
}

# Recap fields whose answer names the store.
_STORE_FIELD_RE = re.compile(r"\b(store|retailer|account|venue|chain|banner)\b", re.I)
# ...but not ones that merely mention a store ("store manager name").
_NOT_STORE_FIELD_RE = re.compile(
    r"\b(manager|contact|feedback|traffic|comment|note|photo|number|#|hours|"
    r"employee|staff|buy.?in|sold|stock|inventory|price|display|address|city|state)\b",
    re.I,
)
_STORE_PLACE_TYPES = {
    "supermarket", "grocery_store", "liquor_store", "convenience_store",
    "department_store", "warehouse_store", "discount_store", "store",
    "drugstore", "pharmacy", "gas_station", "market", "food_store",
    "shopping_mall", "wholesaler", "bar", "restaurant", "night_club",
    "stadium", "event_venue",
}
_MIN_NAME = 3
_PLACES_URL = "https://places.googleapis.com/v1/places:searchText"


def _norm(text: str | None) -> str:
    """Lower-case, punctuation-free, single-spaced."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower())).strip()


def _house_number(text: str | None) -> str:
    m = re.search(r"\b(\d{2,6})\b", text or "")
    return m.group(1) if m else ""


@dataclass
class Evidence:
    """What Spark knows about where an event happened. (source, text) pairs,
    most trustworthy first."""

    items: list[tuple[str, str]] = field(default_factory=list)
    address: str = ""

    def add(self, source: str, text: str | None) -> None:
        text = (text or "").strip()
        if text and (source, text) not in self.items:
            self.items.append((source, text))

    def as_json(self) -> list[dict]:
        return [{"source": s, "text": t[:300]} for s, t in self.items]


@dataclass
class Suggestion:
    name: str
    source: str
    confidence: str  # high | medium | low
    reason: str
    retailer_id: int | None = None


def event_has_retailer(event) -> bool:
    """True when reports would already show a retailer for this event (the
    same chain the ``eventRetailer`` resolver walks)."""
    if event is None:
        return True
    if getattr(event, "retailer_id", None):
        return True
    request = getattr(event, "request", None)
    if request is not None:
        if getattr(request, "retailer_id", None):
            return True
        if (getattr(request, "retailer_name", None) or "").strip():
            return True
    return False


def gather_evidence(event) -> Evidence:
    from events.models import SheetShift
    from recaps.models import CustomFieldValue, CustomRecap

    ev = Evidence(address=(getattr(event, "address", None) or "").strip())

    for name in (
        SheetShift.objects.filter(event_id=event.id)
        .exclude(store_name="")
        .values_list("store_name", flat=True)
        .distinct()[:5]
    ):
        ev.add("sheet", name)

    for name in (
        CustomRecap.objects.filter(event_id=event.id)
        .exclude(typed_store_name="")
        .values_list("typed_store_name", flat=True)
        .distinct()[:5]
    ):
        ev.add("typed", name)

    for field_name, value in (
        CustomFieldValue.objects.filter(custom_recap__event_id=event.id)
        .exclude(value="")
        .values_list("custom_field__name", "value")[:400]
    ):
        label = field_name or ""
        if not _STORE_FIELD_RE.search(label) or _NOT_STORE_FIELD_RE.search(label):
            continue
        value = (value or "").strip()
        # A store name is short text; skip numbers, JSON lists and essays.
        if not value or len(value) > 80 or value[0] in "[{" or not re.search(r"[A-Za-z]{2}", value):
            continue
        ev.add("field", value)

    ev.add("event", getattr(event, "name", None))
    request = getattr(event, "request", None)
    if request is not None:
        ev.add("event", getattr(request, "name", None))
        ev.add("address", getattr(request, "retailer_address", None))
    ev.add("address", ev.address)
    return ev


def match_known_retailer(evidence: Evidence, retailers: list[tuple[int, str]]) -> Suggestion | None:
    """A tenant retailer whose whole name appears in the evidence. The first
    (most trusted) source that names one wins; within it, the longest name."""
    known = sorted(
        ((rid, name, _norm(name)) for rid, name in retailers if len(_norm(name)) >= _MIN_NAME),
        key=lambda r: -len(r[2]),
    )
    for source, text in evidence.items:
        hay = f" {_norm(text)} "
        for rid, name, norm in known:
            if f" {norm} " in hay:
                exact = hay.strip() == norm
                confidence = "high" if source in ("sheet", "typed", "field") or exact else "medium"
                return Suggestion(
                    name=name,
                    source=source,
                    confidence=confidence,
                    reason=f"“{text[:120]}” names {name}, a retailer already in Spark.",
                    retailer_id=rid,
                )
    return None


def unlisted_store_name(evidence: Evidence) -> Suggestion | None:
    """A store name someone wrote down that is not a known retailer yet: the
    sheet's store column, the typed store, or a store field on the recap."""
    for source, text in evidence.items:
        if source not in ("sheet", "typed", "field"):
            continue
        name = re.sub(r"\s+", " ", text).strip(" -–,.")
        # Drop a trailing store number or address fragment: "Kroger #412".
        name = re.sub(r"\s*(#|no\.?|store)\s*\d+.*$", "", name, flags=re.I).strip()
        if _MIN_NAME <= len(name) <= 60 and not re.match(r"^\d", name):
            return Suggestion(
                name=name,
                source=source,
                confidence="medium",
                reason=f"Written as “{text[:120]}” on {SOURCE_LABELS[source]}; not a retailer in Spark yet.",
            )
    return None


def places_lookup(address: str, *, timeout: float = 6.0) -> Suggestion | None:
    """The business Google Maps lists at this street address, or None. Never
    raises; returns None when no key is set or the API is not enabled."""
    from utils.map_matching import _google_maps_api_key

    key = _google_maps_api_key()
    number = _house_number(address)
    if not key or not number or len(address) < 8:
        return None
    try:
        import requests

        resp = requests.post(
            _PLACES_URL,
            json={"textQuery": f"store at {address}", "maxResultCount": 5},
            headers={
                "X-Goog-Api-Key": key,
                "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.types",
            },
            timeout=timeout,
        )
        if resp.status_code != 200:
            logger.info("retailer proposal: Places lookup returned %s", resp.status_code)
            return None
        places = (resp.json() or {}).get("places") or []
    except Exception:  # noqa: BLE001 — a lookup must never break the caller
        logger.info("retailer proposal: Places lookup failed", exc_info=True)
        return None

    for place in places:
        name = ((place.get("displayName") or {}).get("text") or "").strip()
        formatted = place.get("formattedAddress") or ""
        types = set(place.get("types") or [])
        # Same building only, and a business rather than the address itself.
        if not name or _house_number(formatted) != number:
            continue
        if not (types & _STORE_PLACE_TYPES) or _norm(name) == _norm(formatted.split(",")[0]):
            continue
        return Suggestion(
            name=name[:120],
            source="places",
            confidence="medium",
            reason=f"Google Maps lists {name} at {formatted}.",
        )
    return None


def places_status(*, timeout: float = 6.0) -> str:
    """One plain sentence on whether the Maps key can look places up, for the
    command's output. Never raises and never prints the key."""
    from utils.map_matching import _google_maps_api_key

    key = _google_maps_api_key()
    if not key:
        return "Google Maps lookup: off (no GOOGLE_MAPS_API_KEY set)."
    try:
        import requests

        resp = requests.post(
            _PLACES_URL,
            json={"textQuery": "Walmart Supercenter Bentonville AR", "maxResultCount": 1},
            headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": "places.displayName"},
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        return f"Google Maps lookup: could not reach Google ({type(exc).__name__})."
    if resp.status_code == 200:
        return "Google Maps lookup: working."
    try:
        detail = ((resp.json() or {}).get("error") or {}).get("message") or ""
    except Exception:  # noqa: BLE001
        detail = ""
    detail = detail.replace(key, "[key]")[:240]
    return f"Google Maps lookup: not available (HTTP {resp.status_code}). {detail}".strip()


_AI_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["retailer", "confidence", "reason"],
    "properties": {
        "retailer": {"type": ["string", "null"]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "reason": {"type": "string"},
    },
}

_AI_SYSTEM = (
    "You identify which retail store or venue a field-marketing event took place at. "
    "You are given text Spark holds about the event and the list of retailers this "
    "brand already uses. Answer with the retailer's banner name only (for example "
    "\"Kroger\", not \"Kroger #412 Houston\"). If a listed retailer fits, return its "
    "name exactly as listed. Only name a retailer the text itself supports; a street "
    "address alone is not evidence. If the text does not identify one, return null. "
    "Keep the reason to one short sentence quoting the words you relied on."
)


def ai_suggestion(evidence: Evidence, retailers: list[tuple[int, str]]) -> Suggestion | None:
    """Ask the text model to read the evidence. None when AI is not
    configured, fails, or declines to name a retailer."""
    from utils.ai_text import AiUnavailable, generate_json

    texts = [(s, t) for s, t in evidence.items if s != "address"]
    if not texts:
        return None
    names = [name for _rid, name in retailers][:300]
    user = (
        "Event details:\n"
        + "\n".join(f"- {SOURCE_LABELS.get(s, s)}: {t[:300]}" for s, t in evidence.items)
        + "\n\nRetailers this brand already uses:\n"
        + (", ".join(names) if names else "(none yet)")
    )
    try:
        out = generate_json(_AI_SYSTEM, user, schema=_AI_SCHEMA, max_tokens=300)
    except AiUnavailable:
        return None
    except Exception:  # noqa: BLE001
        logger.info("retailer proposal: AI call failed", exc_info=True)
        return None
    name = ((out or {}).get("retailer") or "").strip()
    if not name or len(name) > 120:
        return None
    by_norm = {_norm(n): (rid, n) for rid, n in retailers}
    rid, listed = by_norm.get(_norm(name), (None, name))
    confidence = (out or {}).get("confidence") or "low"
    # The model's own "high" is capped: only a written-down store name earns it.
    if confidence == "high":
        confidence = "medium"
    return Suggestion(
        name=listed,
        source="ai",
        confidence=confidence,
        reason=((out or {}).get("reason") or "").strip()[:300] or "Read from the event details.",
        retailer_id=rid,
    )


def suggest_for_event(event, *, use_network: bool = True) -> tuple[Suggestion | None, Evidence]:
    from events.models import Retailer

    evidence = gather_evidence(event)
    retailers = list(
        Retailer.objects.filter(tenant_id=event.tenant_id)
        .exclude(name="")
        .values_list("id", "name")
        .order_by("id")
    )
    suggestion = match_known_retailer(evidence, retailers) or unlisted_store_name(evidence)
    if suggestion is None and use_network:
        suggestion = places_lookup(evidence.address)
        if suggestion is not None:
            rid = {_norm(n): r for r, n in retailers}.get(_norm(suggestion.name))
            suggestion.retailer_id = rid
        else:
            suggestion = ai_suggestion(evidence, retailers)
    return suggestion, evidence


def propose_for_event(event, *, use_network: bool = True, dry_run: bool = False):
    """Store a pending suggestion for an event with no retailer.

    Returns the proposal row, a :class:`Suggestion` on a dry run, or None when
    the event already has a retailer or nothing could be suggested. An
    accepted or dismissed proposal is never reopened.
    """
    from recaps.models import RetailerProposal

    if event_has_retailer(event):
        return None
    existing = RetailerProposal.objects.filter(event_id=event.id).first()
    if existing is not None and existing.status != RetailerProposal.STATUS_NONE:
        return existing if existing.status == RetailerProposal.STATUS_PENDING else None

    suggestion, evidence = suggest_for_event(event, use_network=use_network)
    if dry_run:
        return suggestion

    defaults = {
        "tenant_id": event.tenant_id,
        "evidence": evidence.as_json(),
        "status": RetailerProposal.STATUS_NONE,
        "proposed_name": "",
        "retailer_id": None,
        "source": "",
        "confidence": "",
        "reason": "",
    }
    if suggestion is not None:
        defaults.update(
            status=RetailerProposal.STATUS_PENDING,
            proposed_name=suggestion.name[:255],
            retailer_id=suggestion.retailer_id,
            source=suggestion.source,
            confidence=suggestion.confidence,
            reason=suggestion.reason[:500],
        )
    proposal, _ = RetailerProposal.objects.update_or_create(event_id=event.id, defaults=defaults)
    return proposal if suggestion is not None else None


def pending_proposal_for_event(event_id: int | None):
    from recaps.models import RetailerProposal

    if not event_id:
        return None
    return (
        RetailerProposal.objects.select_related("retailer")
        .filter(event_id=event_id, status=RetailerProposal.STATUS_PENDING)
        .first()
    )


def apply_proposal(proposal, *, actor, name_override: str | None = None):
    """Staff accepted the suggestion (or corrected its name): give the event
    its retailer. Creates the tenant retailer when it does not exist yet.
    Never replaces a retailer an event or recap already has."""
    from events.models import Event, Retailer
    from recaps.models import CustomRecap, Recap, RetailerProposal

    name = re.sub(r"\s+", " ", (name_override or "").strip()) or proposal.proposed_name
    if not name:
        raise ValueError("A retailer name is required.")

    with transaction.atomic():
        retailer = None
        if not (name_override or "").strip() and proposal.retailer_id:
            retailer = proposal.retailer
        if retailer is None:
            retailer = (
                Retailer.objects.filter(tenant_id=proposal.tenant_id, name__iexact=name)
                .order_by("id")
                .first()
            )
        if retailer is None:
            retailer = Retailer.objects.create(
                tenant_id=proposal.tenant_id, name=name[:255], created_by=actor
            )

        Event.objects.filter(id=proposal.event_id, retailer__isnull=True).update(
            retailer=retailer, updated_by=actor, updated_at=timezone.now()
        )
        CustomRecap.objects.filter(event_id=proposal.event_id, retailer__isnull=True).update(
            retailer=retailer
        )
        Recap.objects.filter(event_id=proposal.event_id, retailer__isnull=True).update(
            retailer=retailer
        )

        proposal.status = RetailerProposal.STATUS_ACCEPTED
        proposal.retailer = retailer
        proposal.proposed_name = retailer.name
        proposal.decided_by = actor
        proposal.decided_at = timezone.now()
        proposal.save()
    return retailer


def dismiss_proposal(proposal, *, actor) -> None:
    from recaps.models import RetailerProposal

    proposal.status = RetailerProposal.STATUS_DISMISSED
    proposal.decided_by = actor
    proposal.decided_at = timezone.now()
    proposal.save(update_fields=["status", "decided_by", "decided_at", "updated_at"])
