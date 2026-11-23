"""OpenAI (+ Places) enrichment for 3rd-party typed store → chain mapping.

Heuristic maybe-matches live in
``ambassadors.checkin_web.suggest_store_matches`` and are stamped onto
``CustomRecap.store_suggestions`` at walk-up filing time. That path stays
offline and fast.

This module adds network enrichment when heuristics are weak (address-only
typing, odd chain spellings) and a staff-only refresh that persists the
merged list back onto the recap. Suggestions are never auto-applied —
admin confirms via ``mapCustomRecapStore``.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Heuristic scores at/above this skip Places + AI (already a strong maybe).
_STRONG_HEURISTIC = 80
# Cap how many catalog names we send the model (token + cost).
_AI_CATALOG_CAP = 220
_NON_ANSWERS = {
    "null",
    "none",
    "unknown",
    "n a",
    "na",
    "not specified",
    "unspecified",
    "not provided",
    "not available",
    "no retailer",
    "undefined",
    "tbd",
}

_AI_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["matches"],
    "properties": {
        "matches": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["retailer", "confidence", "reason", "kind"],
                "properties": {
                    "retailer": {"type": ["string", "null"]},
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                    "reason": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["chain", "store"],
                    },
                },
            },
        }
    },
}

_AI_SYSTEM = (
    "You map a free-typed retail store name and/or street address to the "
    "national or regional account/chain Spark already has in its catalog. "
    "Return only retailers from the provided catalog, using each name "
    "exactly as listed. Prefer the national chain banner when the typed "
    "text is a chain location (for example Total Wine, Publix, H-E-B) and "
    "no single store row clearly matches the address. Prefer a specific "
    "store row when the address clearly matches that row. If nothing in "
    "the catalog fits, return an empty matches array. Keep each reason to "
    "one short sentence. Never invent a retailer that is not in the catalog."
)


def confidence_for_score(score: int) -> str:
    """Map a 0–100 heuristic/enriched score to high | medium | low."""
    if score >= 80:
        return "high"
    if score >= 55:
        return "medium"
    return "low"


def score_for_confidence(confidence: str) -> int:
    """Inverse of :func:`confidence_for_score` for AI / Places rows."""
    c = (confidence or "").strip().lower()
    if c == "high":
        return 86
    if c == "medium":
        return 68
    return 48


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower())).strip()


def annotate_hits(hits: list[dict]) -> list[dict]:
    """Ensure each hit has confidence (+ preserve existing fields)."""
    out: list[dict] = []
    for raw in hits or []:
        if not isinstance(raw, dict) or not raw.get("uuid"):
            continue
        row = dict(raw)
        score = int(row.get("score") or 0)
        row["score"] = score
        if not row.get("confidence"):
            row["confidence"] = confidence_for_score(score)
        out.append(row)
    return out


def _merge_hit(scored: dict[str, dict], hit: dict) -> None:
    key = str(hit.get("uuid") or "")
    if not key:
        return
    score = int(hit.get("score") or 0)
    prev = scored.get(key)
    if prev and int(prev.get("score") or 0) >= score:
        return
    scored[key] = {
        "uuid": key,
        "name": hit.get("name") or "",
        "address": hit.get("address") or "",
        "kind": hit.get("kind") or "store",
        "isNational": bool(hit.get("isNational")),
        "score": score,
        "confidence": hit.get("confidence") or confidence_for_score(score),
        "reason": hit.get("reason") or "",
        "source": hit.get("source") or "heuristic",
    }


def _catalog_rows(tenant) -> list[dict[str, Any]]:
    from events.models import Retailer

    rows = []
    for r in Retailer.objects.filter(tenant=tenant).order_by("-is_national", "name", "id"):
        rows.append(
            {
                "uuid": str(r.uuid),
                "name": r.name or "",
                "address": r.address or "",
                "isNational": bool(r.is_national),
                "kind": "chain" if r.is_national or not (r.address or "").strip() else "store",
            }
        )
    return rows


def _resolve_catalog_name(
    name: str, catalog: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Exact normalized name, else longest catalog name contained in ``name``."""
    target = _norm(name)
    if not target:
        return None
    by_norm = {_norm(r["name"]): r for r in catalog if _norm(r["name"])}
    if target in by_norm:
        return by_norm[target]
    # Prefer longer catalog names so "Total Wine & More" beats "Total".
    contained = [
        r
        for r in catalog
        if (n := _norm(r["name"])) and len(n) >= 4 and (n in target or target in n)
    ]
    if not contained:
        return None
    contained.sort(key=lambda r: (-len(_norm(r["name"])), -int(r.get("isNational") or 0)))
    return contained[0]


def places_enrich(
    address: str, catalog: list[dict[str, Any]]
) -> list[dict]:
    """Google Places business at ``address``, mapped onto catalog rows."""
    from recaps.retailer_proposals import places_lookup

    if not (address or "").strip() or len(address.strip()) < 8:
        return []
    suggestion = places_lookup(address)
    if suggestion is None or not suggestion.name:
        return []
    matched = _resolve_catalog_name(suggestion.name, catalog)
    if matched is None:
        return []
    from ambassadors.checkin_web import addresses_fuzzy_match

    same_street = bool(
        matched.get("address")
        and addresses_fuzzy_match(address, matched["address"])
    )
    if same_street:
        score = 90
        reason = f"Maps lists {suggestion.name} here — matches this store"
        kind = "store"
        confidence = "high"
    elif matched.get("isNational") or matched.get("kind") == "chain":
        score = 78
        reason = f"Maps lists {suggestion.name} here — maybe the national account"
        kind = "chain"
        confidence = "high"
    else:
        score = 70
        reason = f"Maps lists {suggestion.name} here — maybe this regional chain"
        kind = matched.get("kind") or "chain"
        confidence = "medium"
    return [
        {
            "uuid": matched["uuid"],
            "name": matched["name"],
            "address": matched.get("address") or "",
            "kind": kind,
            "isNational": bool(matched.get("isNational")),
            "score": score,
            "confidence": confidence,
            "reason": reason,
            "source": "places",
        }
    ]


def ai_enrich(
    name: str,
    address: str,
    catalog: list[dict[str, Any]],
    *,
    limit: int = 3,
) -> list[dict]:
    """Ask the small model which catalog retailers fit the typed store."""
    from utils.ai_text import AiUnavailable, generate_json

    if not catalog:
        return []
    if not (name or "").strip() and not (address or "").strip():
        return []

    # Prefer nationals + distinct banner names so the model sees chains first.
    seen_names: set[str] = set()
    listed: list[dict[str, Any]] = []
    for row in catalog:
        n = (row.get("name") or "").strip()
        key = _norm(n)
        if not key or key in seen_names:
            continue
        seen_names.add(key)
        listed.append(row)
        if len(listed) >= _AI_CATALOG_CAP:
            break

    catalog_lines = []
    for row in listed:
        tag = "national" if row.get("isNational") else ("chain" if row.get("kind") == "chain" else "store")
        addr = (row.get("address") or "").strip()
        catalog_lines.append(
            f"- {row['name']} [{tag}]" + (f" · {addr}" if addr else "")
        )

    user = (
        f"Typed store name: {(name or '').strip() or '(none)'}\n"
        f"Typed address: {(address or '').strip() or '(none)'}\n\n"
        "Catalog retailers for this brand:\n"
        + "\n".join(catalog_lines)
        + "\n\nReturn up to 3 matches from that catalog, best first."
    )
    try:
        out = generate_json(
            _AI_SYSTEM, user, schema=_AI_SCHEMA, max_tokens=500, small=True
        )
    except AiUnavailable:
        return []
    except Exception:  # noqa: BLE001 — never break filing / admin UI
        logger.info("store automap: AI call failed", exc_info=True)
        return []

    matches = (out or {}).get("matches") if isinstance(out, dict) else None
    if not isinstance(matches, list):
        return []

    hits: list[dict] = []
    for raw in matches:
        if not isinstance(raw, dict):
            continue
        retailer_name = ((raw.get("retailer") or "") if raw else "").strip().strip("\"'")
        if not retailer_name or _norm(retailer_name) in _NON_ANSWERS:
            continue
        matched = _resolve_catalog_name(retailer_name, catalog)
        if matched is None:
            continue
        confidence = (raw.get("confidence") or "low").strip().lower()
        if confidence not in ("high", "medium", "low"):
            confidence = "low"
        # Model "high" alone is capped unless Places/heuristic also agree later.
        if confidence == "high":
            confidence = "medium"
        kind = (raw.get("kind") or matched.get("kind") or "chain").strip().lower()
        if kind not in ("chain", "store"):
            kind = "chain"
        reason = ((raw.get("reason") or "") if raw else "").strip()[:300]
        if not reason:
            reason = "AI reading of the typed store / address"
        hits.append(
            {
                "uuid": matched["uuid"],
                "name": matched["name"],
                "address": matched.get("address") or "",
                "kind": kind,
                "isNational": bool(matched.get("isNational")),
                "score": score_for_confidence(confidence),
                "confidence": confidence,
                "reason": reason,
                "source": "ai",
            }
        )
        if len(hits) >= limit:
            break
    return hits


def enrich_store_suggestions(
    tenant,
    name: str,
    address: str,
    *,
    use_network: bool = True,
    limit: int = 8,
) -> list[dict]:
    """Heuristic matches, optionally enriched with Places + OpenAI.

    Always returns annotated dicts safe to store on
    ``CustomRecap.store_suggestions``. Never auto-maps.
    """
    from ambassadors.checkin_web import suggest_store_matches

    if tenant is None:
        return []

    scored: dict[str, dict] = {}
    for hit in annotate_hits(
        suggest_store_matches(tenant, name or "", address or "", limit=limit)
    ):
        hit = dict(hit)
        hit.setdefault("source", "heuristic")
        _merge_hit(scored, hit)

    top = max((int(h.get("score") or 0) for h in scored.values()), default=0)
    if use_network and top < _STRONG_HEURISTIC:
        catalog = _catalog_rows(tenant)
        for hit in places_enrich(address or "", catalog):
            _merge_hit(scored, hit)
        # Re-check: Places may already have produced a strong hit.
        top = max((int(h.get("score") or 0) for h in scored.values()), default=0)
        if top < _STRONG_HEURISTIC:
            for hit in ai_enrich(name or "", address or "", catalog):
                _merge_hit(scored, hit)

    return sorted(scored.values(), key=lambda x: -int(x.get("score") or 0))[:limit]


def refresh_custom_recap_suggestions(recap, *, use_network: bool = True) -> list[dict]:
    """Recompute + persist store suggestions on an unmatched 3rd-party recap."""
    if recap is None:
        return []
    if not getattr(recap, "is_third_party", False):
        return list(getattr(recap, "store_suggestions", None) or [])
    if getattr(recap, "store_mapping_status", "") == "mapped":
        return annotate_hits(list(getattr(recap, "store_suggestions", None) or []))

    tenant = getattr(recap, "tenant", None)
    if tenant is None and getattr(recap, "tenant_id", None):
        from tenants.models import Tenant

        tenant = Tenant.objects.filter(id=recap.tenant_id).first()

    hits = enrich_store_suggestions(
        tenant,
        getattr(recap, "typed_store_name", "") or "",
        getattr(recap, "typed_store_address", "") or "",
        use_network=use_network,
    )
    recap.store_suggestions = hits
    recap.save(update_fields=["store_suggestions", "updated_at"])
    return hits
