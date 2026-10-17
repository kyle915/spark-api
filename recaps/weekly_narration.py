"""Weekly ops narration grounded in deterministic insight buckets.

The model writes sentences only. Every number in the stored paragraph has
to appear in the fact packet (week-scoped insight buckets + filed recap
count). A sentence that invents a figure is discarded and replaced with
the bucket text itself. This module never sends email.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Literal

from django.utils import timezone

from recaps.filed import custom_filed_q, legacy_filed_q
from recaps.models import CustomRecap, Recap
from recaps.tenant_insights import build_insight_buckets_for_week
from recaps.tenant_overview import _filter_event_window, _inclusive_dates_to_window
from utils.ai_text import generate_summary

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?%?")

_SYSTEM = (
    "You write a short weekly field-marketing narration for Ignite ops. "
    "Use ONLY numbers that appear in FACTS. Copy each figure with the label "
    "already attached to it. Filed recap count is not consumers sampled, "
    "not reach, and not samples handed out. Mention samples handed out only "
    "when a bucket is titled that way. Mention conversion only when a bucket "
    "detail already states a conversion percent — conversion is Retail + "
    "On-Premise only. Write 3 to 5 sentences. Do not invent figures. Do not "
    "approve recaps or suggest editing metrics. If FACTS cannot support a "
    "point, leave it out."
)


def previous_complete_week(today: date | None = None) -> tuple[date, date]:
    """Monday–Sunday of the last finished week."""
    anchor = today or timezone.localdate()
    this_monday = anchor - timedelta(days=anchor.weekday())
    week_end = this_monday - timedelta(days=1)
    week_start = week_end - timedelta(days=6)
    return week_start, week_end


def filed_recap_count(tenant_id: int, start: date, end: date) -> int:
    """Submitted recaps in the week. Empty clock-out rows do not count."""
    window = _inclusive_dates_to_window(start, end)
    legacy = _filter_event_window(
        Recap.objects.filter(event__tenant_id=tenant_id).filter(legacy_filed_q()),
        "event__",
        window,
    )
    custom = _filter_event_window(
        CustomRecap.objects.filter(
            tenant_id=tenant_id, exclude_from_aggregates=False
        ).filter(custom_filed_q()),
        "event__",
        window,
    )
    return legacy.count() + custom.count()


def narration_facts(tenant_id: int, start: date, end: date) -> dict:
    """Bucket cards + filed count. Numbers come from the query helpers only."""
    buckets = build_insight_buckets_for_week(tenant_id, start, end)
    filed = filed_recap_count(tenant_id, start, end)
    return {
        "week_start": start.isoformat(),
        "week_end": end.isoformat(),
        "filed_recaps": filed,
        "buckets": [
            {
                "key": item.get("key"),
                "title": item.get("title"),
                "metric": item.get("metric"),
                "detail": item.get("detail"),
            }
            for item in buckets
            if isinstance(item, dict)
        ],
        "labels": (
            "filed_recaps is a count of submitted recaps. "
            "Consumers sampled, samples handed out, and reach stay on their "
            "own buckets and must not be swapped. Conversion, when present, "
            "is Retail + On-Premise only."
        ),
    }


def _norm_number(token: str) -> str:
    return token.replace(",", "").replace("%", "")


def numbers_are_grounded(text: str, facts: dict) -> bool:
    """Every number in ``text`` appears in the fact packet."""
    allowed = {_norm_number(token) for token in _NUMBER.findall(json.dumps(facts))}
    for token in _NUMBER.findall(text or ""):
        if _norm_number(token) not in allowed:
            return False
    blob = json.dumps(facts).lower()
    lowered = (text or "").lower()
    if "conversion" in lowered and "conversion" not in blob:
        return False
    if "samples handed out" in lowered and "samples handed out" not in blob:
        return False
    return True


def fallback_narration(facts: dict) -> str:
    """Sentences built only from bucket detail lines and the filed count."""
    start = facts.get("week_start")
    end = facts.get("week_end")
    filed = int(facts.get("filed_recaps") or 0)
    sentences = [f"Week of {start} to {end}: {filed} filed recaps."]
    for bucket in facts.get("buckets") or []:
        detail = (bucket or {}).get("detail")
        if isinstance(detail, str) and detail.strip():
            sentences.append(detail.strip())
    return " ".join(sentences)


def compose_narration(facts: dict, *, generate=generate_summary) -> tuple[str, Literal["model", "fallback"]]:
    """Ask the model, then keep the reply only when every number is in FACTS."""
    if int(facts.get("filed_recaps") or 0) == 0 and not facts.get("buckets"):
        return fallback_narration(facts), "fallback"
    try:
        text = generate(_SYSTEM, "FACTS:\n" + json.dumps(facts, indent=2))
    except Exception:
        return fallback_narration(facts), "fallback"
    if text and numbers_are_grounded(text, facts):
        return text.strip(), "model"
    return fallback_narration(facts), "fallback"


def upsert_narration(tenant_id: int, start: date, end: date, *, apply: bool) -> dict:
    """Dry-run returns the fact packet. Apply stores one row and sends no mail."""
    facts = narration_facts(tenant_id, start, end)
    summary = {
        "tenant_id": tenant_id,
        "week_start": start.isoformat(),
        "week_end": end.isoformat(),
        "filed_recaps": facts["filed_recaps"],
        "bucket_keys": [b.get("key") for b in facts["buckets"]],
        "emails_sent": 0,
        "written": False,
    }
    if not apply:
        summary["mode"] = "dry_run"
        return summary
    body, source = compose_narration(facts)
    from tenants.models import TenantWeeklyNarration

    row, _created = TenantWeeklyNarration.objects.update_or_create(
        tenant_id=tenant_id,
        week_start=start,
        defaults={
            "week_end": end,
            "body": body,
            "facts": facts,
            "filed_recaps": facts["filed_recaps"],
            "source": source,
        },
    )
    summary["written"] = True
    summary["mode"] = "apply"
    summary["source"] = row.source
    return summary
