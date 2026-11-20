"""Ignite's closest real results for a prospect — anonymized.

The rule (Kyle's): a client's numbers are used only with that client, or as
ANONYMIZED examples — "a national energy-drink brand at Kroger: 1,400+
consumers sampled, 20% conversion". A prospect is never told another
client's name.

How that is enforced here (and re-checked on the finished text by
``guards.leaks``):

* Each tenant is described only by :class:`SalesProfile.descriptor`, which
  is generic and is rejected if it contains any part of the tenant's name.
  Tenants with ``exclude_from_examples`` (or no descriptor) are never used.
* Figures are rounded down to two significant digits with a "+"
  ("1,445" -> "1,400+"); conversion is a whole percent. The raw figures are
  returned separately (``forbidden_numbers``) so the draft can be checked
  for them.
* A retailer is named only when it is a large national/regional chain (see
  :data:`NAMEABLE_CHAINS`) — never a single independent store, a store
  number or a city.
* A category roll-up ("across 4 beverage brands ...") is only given when at
  least :data:`K_MIN` different brands are in it.
* Window: the last 12 months, approved recaps, event date — the dashboard's
  own helpers.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from sales_assist.guards import AiBudget, BudgetExhausted, mentions_name, rounded

logger = logging.getLogger(__name__)

K_MIN = 3
LOOKBACK_DAYS = 365
MIN_CONSUMERS = 100
MAX_EXAMPLES = 2

NAMEABLE_CHAINS = (
    "Kroger", "Walmart", "Target", "Costco", "Sam's Club", "Whole Foods", "Trader Joe's",
    "Total Wine", "BevMo", "H-E-B", "Safeway", "Albertsons", "Vons", "Ralphs", "Publix",
    "Sprouts", "Wegmans", "Meijer", "Harris Teeter", "Food Lion", "Giant", "Stop & Shop",
    "Hy-Vee", "Fry's", "King Soopers", "Smith's", "Fred Meyer", "Jewel-Osco", "ShopRite",
    "Walgreens", "CVS", "7-Eleven", "Circle K", "Raley's", "WinCo", "Winn-Dixie",
    "Bristol Farms", "Gelson's", "Erewhon", "Natural Grocers", "Spec's", "ABC Fine Wine",
    "Binny's", "Lowes Foods", "Ingles", "Piggly Wiggly", "Hannaford", "Tops", "Price Chopper",
    "Giant Eagle", "Schnucks", "Dierbergs", "Festival Foods", "Kowalski's", "Lunds",
    "Cub", "Hornbacher's", "Stater Bros", "Smart & Final", "Grocery Outlet", "Bashas'",
    "Dillons", "Pick 'n Save", "Mariano's", "QFC", "Market Basket", "Big Y", "Weis",
    "Acme", "Shaw's", "Star Market", "Tom Thumb", "Randalls", "Central Market",
)
_CHAIN_KEYS = {re.sub(r"[^a-z0-9]", "", c.lower()): c for c in NAMEABLE_CHAINS}
_CHAIN_ALIASES = {"heb": "H-E-B", "samsclub": "Sam's Club", "wholefoodsmarket": "Whole Foods",
                  "totalwinemore": "Total Wine", "totalwineandmore": "Total Wine"}

PROFILE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["category", "descriptor"],
    "properties": {"category": {"type": "string"}, "descriptor": {"type": "string"}},
}

_PROFILE_SYSTEM = (
    "You classify a consumer brand for an agency's anonymized case studies. "
    "From the DATA (brand name, product names, website text) return category "
    "(2-4 plain lowercase words, e.g. 'energy drink', 'thc beverage', "
    "'artisan bread', 'hard seltzer') and descriptor: how to describe the "
    "brand WITHOUT naming it, e.g. 'a national energy-drink brand', 'a "
    "regional craft bakery', 'an emerging hemp-THC beverage brand'. The "
    "descriptor must not contain the brand's name, any product name, a city "
    "or a person. If unclear, use 'a consumer brand'."
)


def chain_of(store_label: str) -> str:
    """The nameable chain in a store label ("Kroger #412 Ann Arbor" -> "Kroger")."""
    text = re.sub(r"[^a-z0-9 ]", "", (store_label or "").lower())
    squashed = text.replace(" ", "")
    for key, alias in _CHAIN_ALIASES.items():
        if squashed.startswith(key):
            return alias
    best = ""
    for key, chain in _CHAIN_KEYS.items():
        if squashed.startswith(key) and len(key) > len(re.sub(r"[^a-z0-9]", "", best.lower())):
            best = chain
    return best


def descriptor_is_safe(descriptor: str, tenant_name: str, product_names: list[str]) -> bool:
    d = (descriptor or "").strip()
    if not d or len(d) > 120:
        return False
    return not any(mentions_name(d, name) for name in [tenant_name, *product_names] if name)


def ensure_profile(tenant, budget: AiBudget | None, *, allow_ai: bool):
    """The tenant's SalesProfile; filled once by the small model when empty."""
    from events.models import Product
    from sales_assist.models import SalesProfile

    profile, _ = SalesProfile.objects.get_or_create(tenant=tenant)
    if profile.descriptor or profile.exclude_from_examples or not allow_ai or budget is None:
        return profile
    from utils.ai_text import AiUnavailable, generate_json

    products = list(Product.objects.filter(tenant=tenant).values_list("name", flat=True)[:30])
    site_text = ""
    try:
        from product_knowledge.models import KnowledgeSource

        site_text = " ".join(
            KnowledgeSource.objects.filter(tenant=tenant).values_list("text", flat=True)[:3]
        )[:1500]
    except Exception:  # noqa: BLE001
        site_text = ""
    user = (
        "DATA:\n<<<\n"
        + f"brand: {tenant.name}\nproducts: {', '.join(products)}\nsite: {site_text}\n>>>"
    )
    try:
        out = budget.call(generate_json, _PROFILE_SYSTEM, user, schema=PROFILE_SCHEMA, max_tokens=300, small=True)
    except (AiUnavailable, BudgetExhausted):
        return profile
    except Exception:  # noqa: BLE001
        logger.exception("sales-assist: profile failed tenant=%s", tenant.id)
        return profile
    if not isinstance(out, dict):
        return profile
    category = str(out.get("category") or "").strip().lower()[:120]
    descriptor = str(out.get("descriptor") or "").strip()[:160]
    if not descriptor_is_safe(descriptor, tenant.name, products):
        descriptor = ""
    profile.category = category
    profile.descriptor = descriptor
    profile.source = SalesProfile.SOURCE_AI
    profile.save()
    return profile


@dataclass
class TenantResult:
    tenant_id: int
    tenant_name: str
    category: str
    descriptor: str
    consumers: int
    events: int
    conversion_pct: float | None
    chains: dict = field(default_factory=dict)  # chain -> consumers

    def raw_numbers(self) -> list[int]:
        nums = [self.consumers, self.events, *self.chains.values()]
        if self.conversion_pct is not None:
            nums.append(int(round(self.conversion_pct)))
        return [n for n in nums if n]


def build_library(today: date, *, budget: AiBudget | None, allow_ai: bool) -> list[TenantResult]:
    """Every tenant's last-12-month results that may serve as an example."""
    from recaps.consumer_breakdown import consumer_rows
    from recaps.tenant_overview import (
        _inclusive_dates_to_window,
        _tenant_event_recap_counts_window,
        tenant_conversion_kpis,
    )
    from tenants.models import Tenant

    start = today - timedelta(days=LOOKBACK_DAYS)
    out: list[TenantResult] = []
    for tenant in Tenant.objects.all().order_by("id"):
        try:
            rows = consumer_rows(tenant.id, start, today, include_archived=False)
        except Exception:  # noqa: BLE001
            logger.exception("sales-assist: library rows failed tenant=%s", tenant.id)
            continue
        consumers = sum(int(r.get("consumers") or 0) for r in rows)
        if consumers < MIN_CONSUMERS:
            continue
        profile = ensure_profile(tenant, budget, allow_ai=allow_ai)
        if profile.exclude_from_examples or not profile.descriptor:
            continue
        chains: dict[str, int] = {}
        for r in rows:
            chain = chain_of(r.get("store") or "")
            if chain:
                chains[chain] = chains.get(chain, 0) + int(r.get("consumers") or 0)
        events, _recaps = _tenant_event_recap_counts_window(
            tenant.id, _inclusive_dates_to_window(start, today)
        )
        conv = tenant_conversion_kpis(tenant.id, start, today)
        pct = conv.get("pct") if conv.get("sold") else None
        out.append(
            TenantResult(
                tenant_id=tenant.id,
                tenant_name=tenant.name,
                category=profile.category,
                descriptor=profile.descriptor,
                consumers=consumers,
                events=int(events),
                conversion_pct=float(pct) if pct is not None else None,
                chains=chains,
            )
        )
    return out


def _tokens(text: str) -> set[str]:
    words = {w for w in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(w) > 2}
    return {w[:-1] if w.endswith("s") and len(w) > 4 else w for w in words}


def similarity(result: TenantResult, category: str, retailers: list[str]) -> float:
    cat = _tokens(category)
    score = 0.0
    if cat:
        overlap = len(cat & _tokens(result.category))
        score += 3.0 * overlap / max(len(cat), 1)
    wanted = {chain_of(r) or r for r in retailers}
    score += 1.0 * len(wanted & set(result.chains))
    return score


def _example(result: TenantResult, retailers: list[str]) -> dict | None:
    wanted = {chain_of(r) or r for r in retailers}
    shared = [c for c in sorted(result.chains, key=lambda c: -result.chains[c]) if c in wanted]
    chains = (shared or sorted(result.chains, key=lambda c: -result.chains[c]))[:2]
    consumers = rounded(result.consumers)
    if not consumers:
        return None
    example = {
        "who": result.descriptor,
        "where": chains,
        "consumers_sampled": consumers,
        "in_store_events": rounded(result.events) or None,
    }
    if result.conversion_pct is not None and result.conversion_pct > 0:
        example["conversion_percent"] = f"{int(round(result.conversion_pct))}%"
    return {k: v for k, v in example.items() if v}


def pick_comparables(library: list[TenantResult], *, category: str, retailers: list[str],
                     exclude_names: list[str] | None = None) -> dict:
    """``{"examples": [...], "rollup": {...}|None, "forbidden_numbers": [...],
    "forbidden_names": [...]}``. Examples carry only rounded figures."""
    exclude = {n.casefold() for n in (exclude_names or [])}
    pool = [r for r in library if r.tenant_name.casefold() not in exclude]
    ranked = sorted(pool, key=lambda r: (-similarity(r, category, retailers), -r.consumers))
    examples = []
    used: list[TenantResult] = []
    for r in ranked:
        if len(examples) >= MAX_EXAMPLES:
            break
        ex = _example(r, retailers)
        if ex:
            examples.append(ex)
            used.append(r)
    rollup = None
    cat = _tokens(category)
    if cat:
        same = [r for r in pool if cat & _tokens(r.category)]
        if len({r.tenant_id for r in same}) >= K_MIN:
            total = sum(r.consumers for r in same)
            rollup = {
                "brands": f"{len(same)} brands",
                "category_words": " ".join(sorted(cat)),
                "consumers_sampled": rounded(total),
            }
    forbidden = sorted({n for r in used for n in r.raw_numbers()} | (
        {sum(r.consumers for r in pool if cat & _tokens(r.category))} if rollup else set()
    ))
    return {
        "examples": examples,
        "rollup": rollup,
        "forbidden_numbers": forbidden,
        "used_tenant_ids": [r.tenant_id for r in used],
    }
