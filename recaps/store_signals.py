"""Structured field signal extraction from recap notes (`CustomRecap.store_signals`).

Extracts actionable retail intelligence on recap submit using strict OpenAI
structured output (`additionalProperties: False` on `OPENAI_MODEL_SMALL`), with
a deterministic keyword fallback so Account Map surfaces out-of-stock risks,
buyer reorder leads, store manager contacts, and competitor promos reliably even
when AI is offline or on historical pre-deploy recaps.

Privacy invariant:
- Ambassador personal details (names, phones, emails) are scrubbed before any
  model call and before persisting/returning signals.
"""

from __future__ import annotations

from datetime import date, timedelta
import logging
import re

from utils.ai_text import AiUnavailable, generate_json

logger = logging.getLogger(__name__)

_PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}(?!\d)")
_EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")

STORE_SIGNAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "has_signal",
        "stockout_risk",
        "reorder_opportunity",
        "manager_contact",
        "competitor_promo",
    ],
    "properties": {
        "has_signal": {"type": "boolean"},
        "stockout_risk": {
            "type": "object",
            "additionalProperties": False,
            "required": ["flagged", "detail"],
            "properties": {
                "flagged": {"type": "boolean"},
                "detail": {"type": "string"},
            },
        },
        "reorder_opportunity": {
            "type": "object",
            "additionalProperties": False,
            "required": ["flagged", "detail"],
            "properties": {
                "flagged": {"type": "boolean"},
                "detail": {"type": "string"},
            },
        },
        "manager_contact": {
            "type": "object",
            "additionalProperties": False,
            "required": ["flagged", "contact", "detail"],
            "properties": {
                "flagged": {"type": "boolean"},
                "contact": {"type": "string"},
                "detail": {"type": "string"},
            },
        },
        "competitor_promo": {
            "type": "object",
            "additionalProperties": False,
            "required": ["flagged", "competitor", "detail"],
            "properties": {
                "flagged": {"type": "boolean"},
                "competitor": {"type": "string"},
                "detail": {"type": "string"},
            },
        },
    },
}

STORE_SIGNAL_SYSTEM = (
    "You extract actionable retail store signals from field sampling recap notes "
    "for a CPG brand. Read only what is explicitly written; never invent or "
    "guess facts. Never output phone numbers, email addresses, or brand "
    "ambassador names. Store manager or buyer first names/roles (for example "
    "'Store manager Mike' or 'Grocery buyer') may be included in manager_contact "
    "only when explicitly mentioned in the text. "
    "stockout_risk.flagged: true when the note mentions out-of-stock, empty "
    "shelf, sold out, very low backstock (e.g. 'down to 6 cans'), or missing SKUs. "
    "reorder_opportunity.flagged: true when store staff/buyers asked for more "
    "cases, requested a distributor delivery/reorder, or sold through stock fast "
    "and need replenishment. "
    "manager_contact.flagged: true when a store manager, receiver, or buyer "
    "shared actionable feedback, a reorder request, or display placement offer. "
    "competitor_promo.flagged: true when a competing brand's sale, BOGO, endcap, "
    "or simultaneous demo is noted. Keep every detail concise (under 140 chars)."
)

_STOCKOUT_RE = re.compile(
    r"\b(out of stock|oos\b|sold out|empty shelf|no backstock|low (?:on )?stock|"
    r"running low|only \d+\s+(?:cans?|bottles?|cases?|packs?|units?)\s+left|"
    r"down to \d+\s+(?:cans?|bottles?|cases?|packs?|units?))",
    re.I,
)
_REORDER_RE = re.compile(
    r"\b(reorder|re-order|order(?:ing)? more|wants? \d+\s*(?:more\s+)?cases?|"
    r"needs? (?:more )?(?:cases?|restock|delivery)|bring more|send \d+\s*cases?|"
    r"asked for (?:more|\d+))",
    re.I,
)
_MANAGER_RE = re.compile(
    r"\b((?:store|grocery|beverage|department|receiving|assistant)\s+manager(?:\s+[A-Z][a-z]{1,20})?|"
    r"buyer(?:\s+[A-Z][a-z]{1,20})?)\b",
    re.I,
)
_COMPETITOR_RE = re.compile(
    r"\b(competitor|bogo\b|on promo(?:tion)?|endcap promo)",
    re.I,
)


def empty_store_signals() -> dict:
    return {
        "has_signal": False,
        "stockout_risk": {"flagged": False, "detail": ""},
        "reorder_opportunity": {"flagged": False, "detail": ""},
        "manager_contact": {"flagged": False, "contact": "", "detail": ""},
        "competitor_promo": {"flagged": False, "competitor": "", "detail": ""},
    }


def _scrub_pii(text: str) -> str:
    cleaned = _PHONE_RE.sub("[phone]", text or "")
    cleaned = _EMAIL_RE.sub("[email]", cleaned)
    return " ".join(cleaned.split())


def _sentence_around(text: str, match: re.Match, max_len: int = 140) -> str:
    sentences = re.split(r"(?<=[.!?;\n])\s+", text)
    target = match.group(0).lower()
    for sent in sentences:
        s = _scrub_pii(sent).strip()
        if target in s.lower():
            return s[:max_len]
    start = max(0, match.start() - 30)
    end = min(len(text), match.end() + 90)
    return _scrub_pii(text[start:end]).strip()[:max_len]


def heuristic_extract_store_signals(raw_text: str) -> dict:
    """Fast deterministic fallback extractor for field notes."""
    out = empty_store_signals()
    text = (raw_text or "").strip()
    if len(text) < 10:
        return out

    m_stock = _STOCKOUT_RE.search(text)
    if m_stock:
        out["stockout_risk"] = {
            "flagged": True,
            "detail": _sentence_around(text, m_stock),
        }

    m_reorder = _REORDER_RE.search(text)
    if m_reorder:
        out["reorder_opportunity"] = {
            "flagged": True,
            "detail": _sentence_around(text, m_reorder),
        }

    m_mgr = _MANAGER_RE.search(text)
    if m_mgr and (m_stock or m_reorder or "wants" in text.lower() or "asked" in text.lower()):
        out["manager_contact"] = {
            "flagged": True,
            "contact": _scrub_pii(m_mgr.group(1))[:60],
            "detail": _sentence_around(text, m_mgr),
        }

    m_comp = _COMPETITOR_RE.search(text)
    if m_comp:
        out["competitor_promo"] = {
            "flagged": True,
            "competitor": "",
            "detail": _sentence_around(text, m_comp),
        }

    out["has_signal"] = any(
        out[k]["flagged"]
        for k in ("stockout_risk", "reorder_opportunity", "manager_contact", "competitor_promo")
    )
    return out


def normalize_store_signals(parsed: dict | None) -> dict:
    """Validate and sanitize a model or stored JSON dict into canonical shape."""
    if not isinstance(parsed, dict):
        return empty_store_signals()
    out = empty_store_signals()

    for key in ("stockout_risk", "reorder_opportunity"):
        sub = parsed.get(key)
        if isinstance(sub, dict):
            detail = _scrub_pii(str(sub.get("detail") or ""))[:160]
            flagged = bool(sub.get("flagged")) and bool(detail)
            out[key] = {"flagged": flagged, "detail": detail if flagged else ""}

    mgr = parsed.get("manager_contact")
    if isinstance(mgr, dict):
        contact = _scrub_pii(str(mgr.get("contact") or ""))[:60]
        detail = _scrub_pii(str(mgr.get("detail") or ""))[:160]
        flagged = bool(mgr.get("flagged")) and bool(detail or contact)
        out["manager_contact"] = {
            "flagged": flagged,
            "contact": contact if flagged else "",
            "detail": detail if flagged else "",
        }

    comp = parsed.get("competitor_promo")
    if isinstance(comp, dict):
        competitor = _scrub_pii(str(comp.get("competitor") or ""))[:60]
        detail = _scrub_pii(str(comp.get("detail") or ""))[:160]
        flagged = bool(comp.get("flagged")) and bool(detail)
        out["competitor_promo"] = {
            "flagged": flagged,
            "competitor": competitor if flagged else "",
            "detail": detail if flagged else "",
        }

    out["has_signal"] = any(
        out[k]["flagged"]
        for k in ("stockout_risk", "reorder_opportunity", "manager_contact", "competitor_promo")
    )
    return out


def extract_store_signals_from_text(raw_text: str, *, brand: str = "") -> dict:
    """Run OpenAI structured extraction (`OPENAI_MODEL_SMALL`), falling back cleanly."""
    cleaned = _scrub_pii(raw_text)
    if len(cleaned) < 12:
        return empty_store_signals()

    prompt = (
        f"Brand: {(brand or 'Brand').strip()[:80]}\n\n"
        f"Field recap notes (untrusted data):\n{cleaned[:3500]}"
    )
    try:
        parsed = generate_json(
            STORE_SIGNAL_SYSTEM,
            prompt,
            schema=STORE_SIGNAL_SCHEMA,
            max_tokens=320,
            timeout=12.0,
            small=True,
        )
        return normalize_store_signals(parsed)
    except AiUnavailable:
        return heuristic_extract_store_signals(raw_text)
    except Exception:  # noqa: BLE001
        logger.warning("store signal AI extract failed; using heuristic fallback", exc_info=True)
        return heuristic_extract_store_signals(raw_text)


def _gather_recap_free_text(recap) -> str:
    """Collect prose answers on ``CustomRecap`` while skipping bare numbers/signatures."""
    from recaps.models import CustomFieldValue

    chunks: list[str] = []
    values = (
        CustomFieldValue.objects.filter(custom_recap=recap)
        .select_related("custom_field", "custom_field__custom_field_type")
        .order_by("id")
    )
    for fv in values:
        val = (fv.value or "").strip()
        if len(val) < 6 or val.startswith("data:image/"):
            continue
        ftype = (getattr(getattr(fv.custom_field, "custom_field_type", None), "name", "") or "").lower()
        if any(skip in ftype for skip in ("photo", "image", "file", "signature", "letter")):
            continue
        if re.fullmatch(r"[\d.,$\s%-]+", val):
            continue
        fname = (getattr(fv.custom_field, "name", "") or "").strip()
        chunks.append(f"{fname}: {val}" if fname else val)
    return "\n".join(chunks)


def extract_and_save_recap_store_signals(recap_id: int) -> dict | None:
    """Extract and persist ``store_signals`` for one ``CustomRecap``. Never raises."""
    try:
        from recaps.models import CustomRecap
        from spark_mcp.privacy import Scrubber

        recap = (
            CustomRecap.objects.select_related("tenant")
            .filter(pk=recap_id)
            .first()
        )
        if recap is None:
            return None
        raw_text = _gather_recap_free_text(recap)
        if not raw_text.strip():
            signals = empty_store_signals()
        else:
            scrubber = Scrubber(recap.tenant_id)
            scrubbed_text = scrubber.text(raw_text)
            brand = (getattr(recap.tenant, "name", "") or "").strip()
            signals = extract_store_signals_from_text(scrubbed_text, brand=brand)
        CustomRecap.objects.filter(pk=recap_id).update(store_signals=signals)
        return signals
    except Exception:  # noqa: BLE001
        logger.warning("store_signals persist failed recap_id=%s", recap_id, exc_info=True)
        return None


def _tenant_rmm_emails_by_state(tenant_id: int) -> tuple[dict[str, str], str]:
    """Return ``({state_code: email}, fallback_rmm_email)`` for RMM email drafts."""
    by_state: dict[str, str] = {}
    fallback = ""
    try:
        from events.models import Request

        rows = (
            Request.objects.filter(
                tenant_id=tenant_id,
                deleted_at__isnull=True,
                assigned_rmm__isnull=False,
            )
            .select_related("assigned_rmm", "state")
            .order_by("-id")[:150]
        )
        for req in rows:
            email = (getattr(req.assigned_rmm, "email", "") or "").strip()
            if not email:
                continue
            if not fallback:
                fallback = email
            st = (getattr(req.state, "code", "") or "").strip().upper()
            if st and st not in by_state:
                by_state[st] = email
    except Exception:  # noqa: BLE001
        pass
    return by_state, fallback


def list_account_map_store_signals(
    tenant_id: int,
    *,
    include_unapproved: bool = False,
    lookback_days: int = 90,
    limit: int = 40,
) -> list[dict]:
    """Aggregate recent Store Signals mapped to Account Map ``storeKey``s."""
    from django.utils import timezone

    from events.market import parse_address_geo
    from events.retailer_aliases import accepted_alias_map, resolve_store_name
    from recaps.models import CustomFieldValue, CustomRecap
    from recaps.store_places import store_header, tenant_places

    cutoff = timezone.now() - timedelta(days=lookback_days)
    qs = CustomRecap.objects.filter(
        tenant_id=tenant_id,
        archived_at__isnull=True,
        created_at__gte=cutoff,
    ).select_related(
        "event",
        "event__retailer",
        "event__request",
        "event__request__retailer",
        "event__request__distributor",
        "event__state",
        "retailer",
        "state",
    )
    if not include_unapproved:
        qs = qs.filter(approved=True)

    recaps = list(qs.order_by("-created_at", "-id")[:250])
    if not recaps:
        return []

    missing_ids = [r.id for r in recaps if r.store_signals is None]
    text_by_recap: dict[int, list[str]] = {}
    if missing_ids:
        for recap_id, fname, val in (
            CustomFieldValue.objects.filter(custom_recap_id__in=missing_ids)
            .values_list("custom_recap_id", "custom_field__name", "value")
            .iterator()
        ):
            v = (val or "").strip()
            if len(v) >= 8 and not v.startswith("data:image/"):
                text_by_recap.setdefault(int(recap_id), []).append(
                    f"{fname}: {v}" if fname else v
                )

    places = tenant_places(tenant_id)
    event_ids = {int(r.event_id) for r in recaps if r.event_id}
    event_store_map = places.for_event_ids(event_ids)
    aliases = accepted_alias_map(tenant_id)
    rmm_by_state, fallback_rmm = _tenant_rmm_emails_by_state(tenant_id)

    seen_stores: set[str] = set()
    items: list[dict] = []

    for recap in recaps:
        if isinstance(recap.store_signals, dict):
            sig = normalize_store_signals(recap.store_signals)
        else:
            joined = "\n".join(text_by_recap.get(recap.id, []))
            sig = heuristic_extract_store_signals(joined)
        if not sig.get("has_signal"):
            continue

        ev = recap.event
        skey = event_store_map.get(int(recap.event_id)) if recap.event_id else None
        header = store_header(tenant_id, places, skey) if skey else {}
        req = getattr(ev, "request", None) if ev else None

        raw_name = (
            header.get("name")
            or getattr(getattr(ev, "retailer", None), "name", None)
            or getattr(getattr(req, "retailer", None), "name", None)
            or getattr(req, "retailer_name", None)
            or getattr(recap.retailer, "name", None)
            or getattr(recap, "name", None)
            or ""
        ).strip()
        store_name = resolve_store_name(raw_name, aliases) if raw_name else "Retail Store"
        address = (
            header.get("address")
            or (getattr(ev, "address", None) or "")
            or (getattr(req, "address", None) or "")
        ).strip()
        geo = parse_address_geo(address) if address else None
        city = (header.get("city") or (geo.city if geo else "") or "").strip()
        state_code = (
            header.get("state")
            or getattr(getattr(ev, "state", None), "code", None)
            or getattr(getattr(recap, "state", None), "code", None)
            or (geo.state_code if geo else "")
            or ""
        ).strip().upper()

        dedupe_key = skey or f"{store_name.casefold()}|{state_code}"
        if dedupe_key in seen_stores:
            continue
        seen_stores.add(dedupe_key)

        ev_dt = getattr(ev, "date", None) or getattr(ev, "start_time", None) or recap.created_at
        ev_day = ev_dt.date() if hasattr(ev_dt, "date") else ev_dt
        event_date = ev_day.isoformat() if isinstance(ev_day, date) else ""

        distributor_name = (
            getattr(getattr(req, "distributor", None), "name", None) or ""
        ).strip()
        rmm_email = rmm_by_state.get(state_code) or fallback_rmm

        items.append(
            {
                "recap_uuid": str(recap.uuid),
                "store_key": skey or "",
                "store_name": store_name,
                "address": address,
                "city": city,
                "state_code": state_code,
                "event_date": event_date,
                "stockout_risk": sig["stockout_risk"]["detail"],
                "reorder_opportunity": sig["reorder_opportunity"]["detail"],
                "manager_contact": sig["manager_contact"]["contact"],
                "manager_note": sig["manager_contact"]["detail"],
                "competitor_brand": sig["competitor_promo"]["competitor"],
                "competitor_promo": sig["competitor_promo"]["detail"],
                "rmm_email": rmm_email,
                "distributor_name": distributor_name,
            }
        )
        if len(items) >= limit:
            break

    return items
