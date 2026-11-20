""""What we did for you this quarter": one client's own Spark numbers.

The facts are the brand dashboard's own roll-ups over the quarter, by event
date, approved recaps only — the same helpers the weekly / monthly client
summary uses (:func:`recaps.weekly_summary.week_facts`):
``_tenant_event_recap_counts_window``, ``_tenant_kpi_totals_window``,
``tenant_consumer_breakdown`` and ``tenant_conversion_kpis``. Visits with no
product (Kyle's rule, ``recap_decks.no_product``) record nothing, so they
never change a total; they only reduce the demo count, and the facts say how
many were left out.

The model writes the narrative paragraph from those facts and nothing else.
A paragraph holding a number that is not in the facts is retried once and
then dropped — the note is still made, from the deterministic sections
alone. The headline numbers, stores, quotes and photo links are written by
code, not by the model.

These numbers are only ever used with THIS client (its own Close lead).
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta

from sales_assist.guards import AiBudget, BudgetExhausted, banned, numbers_grounded

logger = logging.getLogger(__name__)

MAX_STORES = 5
MAX_QUOTES = 3
MAX_PHOTOS = 4

_SYSTEM = (
    "You write one short paragraph for an Ignite Productions account rep to "
    "send to a client brand: what Ignite's in-store sampling program did for "
    "them this quarter. Use ONLY what is in FACTS. Copy every number exactly "
    "as written there, with its label; never add, subtract, average, round, "
    "estimate or compare to anything not in FACTS, and never write a number "
    "that is not in FACTS. Plain, warm, specific, under 110 words. No "
    "greeting, no sign-off, no headings, no bullet points, no markdown, no "
    "exclamation marks. Do not name any brand ambassador or shopper. Do not "
    "mention prices, rates, budgets or contract terms."
)


def last_complete_quarter(today: date) -> tuple[date, date]:
    q_start_month = 3 * ((today.month - 1) // 3) + 1
    this_q_start = date(today.year, q_start_month, 1)
    prev_end = this_q_start - timedelta(days=1)
    prev_start = date(prev_end.year, 3 * ((prev_end.month - 1) // 3) + 1, 1)
    return prev_start, prev_end


def quarter_label(start: date, end: date) -> str:
    if start.day == 1 and (end + timedelta(days=1)).day == 1 and start.month % 3 == 1:
        q = (start.month - 1) // 3 + 1
        months = (end.year - start.year) * 12 + end.month - start.month
        if months == 2:
            return f"Q{q} {start.year}"
    return f"{start:%b} {start.day}, {start.year} to {end:%b} {end.day}, {end.year}"


def quarter_facts(tenant_id: int, start: date, end: date) -> dict:
    """Everything the renewal note may say. Deterministic; no model."""
    from recap_decks.no_product import no_product_visits
    from recaps.consumer_breakdown import KIND_NAMED, tenant_consumer_breakdown
    from recaps.tenant_overview import (
        _inclusive_dates_to_window,
        _tenant_event_recap_counts_window,
        _tenant_kpi_totals_window,
        tenant_conversion_kpis,
    )
    from tenants.models import Tenant

    window = _inclusive_dates_to_window(start, end)
    events, recaps = _tenant_event_recap_counts_window(tenant_id, window)
    kpis = _tenant_kpi_totals_window(tenant_id, window)
    breakdown = tenant_consumer_breakdown(tenant_id, start, end)

    facts: dict = {
        "brand": Tenant.objects.filter(id=tenant_id).values_list("name", flat=True).first() or "",
        "period": quarter_label(start, end),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "events_run": int(events),
        "approved_recaps": int(recaps),
        "consumers_sampled": int(breakdown["total"]),
    }
    for label, value in (
        ("units_sold", kpis.products_sold),
        ("first_time_consumers", kpis.first_time_consumers),
        ("willing_to_purchase", kpis.willing_to_purchase),
    ):
        if value:
            facts[label] = int(value)
    # samplesDistributed is NOT consumers (loaves for bread tenants): it is
    # never headlined, and only carried when it is not the consumer count.

    try:
        visits = no_product_visits(tenant_id, start, end)
        left_out = int(visits.get("excluded_count") or 0)
    except Exception:  # noqa: BLE001 — the totals stand without it
        logger.exception("sales-assist: no-product scan failed tenant=%s", tenant_id)
        left_out = 0
    if left_out and recaps:
        facts["no_product_visits_left_out"] = left_out
        facts["demos_with_product"] = max(int(recaps) - left_out, 0)

    conversion = tenant_conversion_kpis(tenant_id, start, end)
    if conversion.get("pct") is not None and conversion.get("sold"):
        facts["conversion"] = {
            "conversion_percent": round(float(conversion["pct"]), 1),
            "units_sold": int(conversion["sold"]),
            "sampled_at_those_events": int(conversion["engagements"]),
            "note": "Conversion counts retail and on-premise events only.",
        }

    stores = [r for r in breakdown["by_store"] if r["kind"] == KIND_NAMED][:MAX_STORES]
    if stores:
        facts["top_stores"] = [
            {"store": r["label"], "consumers_sampled": int(r["consumers"])} for r in stores
        ]
        facts["store_count"] = int(breakdown["store_count"])
    states = [r for r in breakdown["by_state"] if r["kind"] == KIND_NAMED]
    if states:
        facts["states"] = [
            {"state": r["label"], "consumers_sampled": int(r["consumers"])} for r in states
        ]
        facts["state_count"] = len(states)
    facts["quotes"] = _quotes(tenant_id, start, end)
    facts["photos"] = _photos(tenant_id, start, end)
    return facts


def _quotes(tenant_id: int, start: date, end: date) -> list[str]:
    from recaps.feedback_themes import KIND_GENERAL, gather_comments

    try:
        comments, _cut = gather_comments(tenant_id, start, end)
    except Exception:  # noqa: BLE001
        logger.exception("sales-assist: quotes failed tenant=%s", tenant_id)
        return []
    picked: list[str] = []
    for c in comments:
        text = " ".join((c.text or "").split())
        if c.kind != KIND_GENERAL or not (20 <= len(text) <= 220):
            continue
        if banned(text):
            continue
        picked.append(text)
        if len(picked) >= MAX_QUOTES:
            break
    return picked


def _photos(tenant_id: int, start: date, end: date) -> list[dict]:
    from recaps.photo_gallery import tenant_photo_gallery

    try:
        page = tenant_photo_gallery(tenant_id, start=start, end=end, picks_only=True, first=MAX_PHOTOS)
    except Exception:  # noqa: BLE001
        logger.exception("sales-assist: photos failed tenant=%s", tenant_id)
        return []
    return [
        {"url": p["url"], "store": p.get("store") or "", "date": p.get("event_date") or ""}
        for p in page.get("photos") or []
    ]


def _model_facts(facts: dict) -> dict:
    """What the model sees: no photo URLs, no quotes (code adds those)."""
    return {k: v for k, v in facts.items() if k not in ("photos", "quotes")}


def narrative(facts: dict, budget: AiBudget) -> tuple[str, str]:
    """``(paragraph, source)``; source is "model", or "none" with the
    paragraph empty when the model is off, out of budget or ungrounded."""
    from utils.ai_text import AiUnavailable, generate_summary

    model_facts = _model_facts(facts)
    user = "FACTS:\n" + json.dumps(model_facts, indent=2, ensure_ascii=False)
    for _attempt in range(2):
        try:
            text = budget.call(generate_summary, _SYSTEM, user, max_tokens=400)
        except (AiUnavailable, BudgetExhausted) as exc:
            return "", f"none: {exc}"
        except Exception:  # noqa: BLE001
            logger.exception("sales-assist: renewal narrative failed")
            return "", "none: model error"
        text = " ".join((text or "").replace("**", "").split())
        ok, _missing = numbers_grounded(text, model_facts)
        if text and ok and not banned(text):
            return text, "model"
    return "", "none: ungrounded"


def _n(value) -> str:
    return f"{int(value):,}"


def headline_lines(facts: dict) -> list[str]:
    lines = [f"Events run: {_n(facts['events_run'])}"]
    if facts.get("no_product_visits_left_out"):
        lines.append(
            f"Demos with product: {_n(facts['demos_with_product'])} "
            f"({_n(facts['no_product_visits_left_out'])} no-product visits left out)"
        )
    lines.append(f"Consumers sampled: {_n(facts['consumers_sampled'])}")
    if facts.get("units_sold"):
        lines.append(f"Units sold: {_n(facts['units_sold'])}")
    conv = facts.get("conversion")
    if conv:
        lines.append(
            f"Conversion (retail + on-premise): {conv['conversion_percent']}% "
            f"({_n(conv['units_sold'])} sold / {_n(conv['sampled_at_those_events'])} sampled)"
        )
    if facts.get("first_time_consumers"):
        lines.append(f"First-time consumers: {_n(facts['first_time_consumers'])}")
    if facts.get("store_count"):
        lines.append(
            f"Stores: {_n(facts['store_count'])}"
            + (f" across {_n(facts['state_count'])} states" if facts.get("state_count", 0) > 1 else "")
        )
    return lines


def build_texts(facts: dict, paragraph: str, *, first_name: str, marker: str, opportunity: str = "") -> dict:
    """The Close note and the draft email, from the facts (code) + paragraph."""
    brand = facts.get("brand") or "the brand"
    period = facts["period"]
    heads = headline_lines(facts)
    stores = [f"{s['store']}: {_n(s['consumers_sampled'])} sampled" for s in facts.get("top_stores") or []]
    states = ", ".join(f"{s['state']} ({_n(s['consumers_sampled'])})" for s in facts.get("states") or [])
    quotes = [f'"{q}"' for q in facts.get("quotes") or []]
    photos = [f"{p['url']}" + (f"  ({p['store']}, {p['date']})" if p.get("store") else "") for p in facts.get("photos") or []]

    note = [
        "SPARK SALES ASSIST - DRAFT FOR REVIEW (nothing has been sent)",
        f"{brand}: {period} results ({facts['period_start']} to {facts['period_end']})",
        "Numbers: Spark dashboard roll-ups, approved recaps, by event date.",
    ]
    if opportunity:
        note.append(f"Opportunity: {opportunity}")
    note += ["", "Headline numbers:"] + [f"- {x}" for x in heads]
    if paragraph:
        note += ["", "Suggested summary (AI-written, every number checked against Spark):", paragraph]
    if stores:
        note += ["", "Top stores:"] + [f"- {x}" for x in stores]
    if states:
        note += ["", f"States: {states}"]
    if quotes:
        note += ["", "What shoppers said:"] + [f"- {q}" for q in quotes]
    if photos:
        note += ["", "Best photos:"] + [f"- {p}" for p in photos]
    note += ["", "A draft email with this summary is in the lead's Emails (status: draft). Review, edit and send it yourself.", marker]

    greeting = f"Hi {first_name}," if first_name else "Hi there,"
    body = [greeting, "", f"Here is a quick look at what your in-store program did in {period}."]
    if paragraph:
        body += ["", paragraph]
    # The no-product line is for Ignite's own review; the client email leaves it out.
    client_heads = [x for x in heads if not x.startswith("Demos with product")]
    body += ["", "The headline numbers:"] + [f"- {x}" for x in client_heads]
    if stores:
        body += ["", "Top stores by consumers sampled:"] + [f"- {x}" for x in stores[:3]]
    if quotes:
        body += ["", "A few things shoppers told our team:"] + [f"- {q}" for q in quotes]
    body += ["", "Happy to walk through the full recap and talk about next quarter whenever works for you."]
    return {
        "note": "\n".join(note),
        "subject": f"Your {period} in-store results",
        "body": "\n".join(body),
    }
