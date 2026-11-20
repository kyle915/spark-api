"""A short proposal email + one-page pitch for a new Close lead.

Facts the model may use: the lead research (``research.research_lead``),
the anonymized comparable results (``comparables.pick_comparables``) and a
few fixed Ignite facts. The finished text must pass, or it is not kept:

* every number appears in those facts (``guards.numbers_grounded``);
* no other client's name and no exact figure tied to one
  (``guards.leaks``) — the model only ever sees rounded figures and generic
  descriptors, and the check runs anyway;
* none of Ignite's banned content: prices / rates, "42k", Girl Beer, kratom
  (``guards.banned``).

One retry, then the lead is recorded as failed with the reason.
"""

from __future__ import annotations

import json
import logging

from sales_assist.guards import (
    AiBudget,
    BudgetExhausted,
    banned,
    leaks,
    numbers_grounded,
    numbers_in,
)

logger = logging.getLogger(__name__)

# Fixed, approved Ignite facts. 257,000+ is Kyle's locked roster figure.
IGNITE_FACTS = {
    "agency": "Ignite Productions, a veteran-owned field marketing agency",
    "ambassador_network": "257,000+ brand ambassadors",
    "services": [
        "in-store demos and sampling",
        "brand ambassador staffing",
        "retail activations",
        "event and festival staffing",
    ],
    "reporting": (
        "Spark, Ignite's own platform: every demo gets a recap with consumers "
        "sampled, units sold, shopper feedback and photos, on a live brand "
        "dashboard"
    ),
    "ask": "a 15-minute call",
}

DRAFT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["subject", "email_body", "one_pager"],
    "properties": {
        "subject": {"type": "string"},
        "email_body": {"type": "string"},
        "one_pager": {"type": "string"},
    },
}

_SYSTEM = (
    "You draft outreach for Ignite Productions' sales rep to a NEW prospect "
    "brand. Use ONLY the FACTS. Rules: (1) Copy every number exactly as "
    "written in FACTS (keep the '+' and '%'); never write any other number, "
    "price, rate, fee or budget. (2) Ignite's past results appear only as "
    "the anonymized examples given; describe them exactly with their 'who' "
    "wording and NEVER name, hint at or guess which brand they are. (3) Do "
    "not claim anything about the prospect beyond PROSPECT facts; if unsure, "
    "leave it out. (4) No exclamation marks, no hype, no markdown symbols "
    "other than '- ' bullets in the one-pager. Write: subject (under 9 "
    "words); email_body (under 140 words, starts 'Hi {first_name},' when a "
    "first_name is given else 'Hi there,', one line on why them using the "
    "research, one or two anonymized results, a soft ask for a 15-minute "
    "call, no sign-off - the rep adds it); one_pager (plain text, under 260 "
    "words, sections: 'About {prospect}', 'What we would do', 'Results from "
    "similar programs', 'How you see results', 'Next step')."
)


def build_facts(research: dict, comparables: dict, *, first_name: str) -> dict:
    summary = research.get("summary") or {}
    prospect = {
        "name": research["lead"].get("brand_name") or research["lead"].get("lead_name") or "",
        "category": summary.get("category", ""),
        "what_they_sell": summary.get("what_they_sell", ""),
        "products": summary.get("products", []),
        "retailers": summary.get("retailers", []),
        "recent_news": summary.get("recent_news", []),
        "regions": summary.get("regions", []),
    }
    for key in ("year_founded", "employees", "total_funding", "market"):
        if research["lead"].get(key):
            prospect[key] = research["lead"][key]
    facts = {
        "first_name": first_name,
        "prospect": prospect,
        "ignite": IGNITE_FACTS,
        "anonymized_examples": comparables.get("examples") or [],
    }
    if comparables.get("rollup"):
        facts["category_rollup"] = comparables["rollup"]
    return facts


def check_text(text: str, facts: dict, *, forbidden_names: list[str], forbidden_numbers: list[int]) -> list[str]:
    problems: list[str] = []
    ok, missing = numbers_grounded(text, facts)
    if not ok:
        problems.append("ungrounded numbers: " + ", ".join(missing[:5]))
    problems += [f"leak {x}" for x in leaks(text, forbidden_names=forbidden_names, forbidden_numbers=forbidden_numbers)]
    problems += [f"banned: {x}" for x in banned(text)]
    return problems


def compose(facts: dict, budget: AiBudget, *, forbidden_names: list[str],
            forbidden_numbers: list[int]) -> tuple[dict | None, str]:
    """``(draft, "")`` or ``(None, reason)``."""
    from utils.ai_text import AiUnavailable, generate_json

    # A rounded figure that we deliberately show is not a leak even if it
    # happens to equal a raw figure exactly.
    shown = set(numbers_in(json.dumps(facts, ensure_ascii=False)))
    forbidden_numbers = [n for n in forbidden_numbers if str(int(n)) not in shown]
    user = "FACTS:\n" + json.dumps(facts, indent=2, ensure_ascii=False)
    last = "no draft"
    for _attempt in range(2):
        try:
            out = budget.call(generate_json, _SYSTEM, user, schema=DRAFT_SCHEMA, max_tokens=1500)
        except (AiUnavailable, BudgetExhausted) as exc:
            return None, str(exc)
        except Exception:  # noqa: BLE001
            logger.exception("sales-assist: prospect draft failed")
            return None, "model error"
        if not isinstance(out, dict):
            last = "model returned nothing usable"
            continue
        draft = {k: str(out.get(k) or "").strip() for k in ("subject", "email_body", "one_pager")}
        if not all(draft.values()):
            last = "model left a part empty"
            continue
        problems = check_text(
            "\n".join(draft.values()), facts,
            forbidden_names=forbidden_names, forbidden_numbers=forbidden_numbers,
        )
        if not problems:
            return draft, ""
        last = "; ".join(problems)[:280]
        logger.info("sales-assist: prospect draft rejected: %s", last)
    return None, last


def build_note(lead_name: str, research: dict, draft: dict, *, marker: str, has_email: bool) -> str:
    summary = research.get("summary") or {}
    lines = [
        "SPARK SALES ASSIST - DRAFT FOR REVIEW (nothing has been sent)",
        f"Prospect: {lead_name}",
        "",
        "Research (from the lead's own website and Close fields; verify before using):",
        f"- Category: {summary.get('category') or 'unclear'}",
        f"- Sells: {summary.get('what_they_sell') or 'unclear'}",
    ]
    if summary.get("retailers"):
        lines.append("- Retailers mentioned: " + ", ".join(summary["retailers"]))
    if summary.get("recent_news"):
        lines.append("- Recent: " + " | ".join(summary["recent_news"]))
    if research.get("sources"):
        lines.append("- Sources: " + ", ".join(research["sources"]))
    lines += [
        "",
        "Results are ANONYMIZED examples from other Ignite programs (rounded). "
        "Never name another client to this prospect.",
        "",
        "ONE-PAGE PITCH",
        draft["one_pager"],
        "",
        (
            "A draft email is in this lead's Emails (status: draft). Review, edit and send it yourself."
            if has_email
            else "No contact email on this lead, so no draft email was made. Email draft text:\n"
            + f"Subject: {draft['subject']}\n\n{draft['email_body']}"
        ),
        marker,
    ]
    return "\n".join(lines)
