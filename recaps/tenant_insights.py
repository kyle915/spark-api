"""Proactive "what's notable" AI insights for one tenant, cached server-side.

The dashboard surfaces a short list of auto-generated headline observations
about a client's program — wins, trends, standouts, things needing attention —
WITHOUT the user asking. This module owns:

* :func:`build_tenant_insights` — assemble a COMPACT numeric context from the
  shared :mod:`recaps.tenant_overview` aggregates (headline KPIs, totals, and
  the latest-month-vs-prior deltas / recent trend) and ask OpenAI, via
  :func:`utils.ai_text.generate_json` with a strict insights schema, for the
  3–6 most notable items. Returns the parsed ``insights`` list, or ``[]`` on
  any failure.
* :func:`get_or_refresh_tenant_insights` — the cache front door: serve the
  latest :class:`tenants.models.TenantInsightSnapshot` if it's younger than
  ``max_age_hours``; otherwise generate a fresh one, persist it, and return it.
  On generation failure it falls back to the most recent existing snapshot
  (even if stale). It NEVER raises.

Design rules (mirroring the rest of the report surface):

* **Reuse, don't re-aggregate.** All numbers come from
  :func:`recaps.tenant_overview.tenant_kpi_totals`,
  :func:`recaps.tenant_overview.tenant_event_recap_counts`, and
  :func:`recaps.tenant_overview.tenant_monthly_trend`, so the insights agree
  with the ``tenantKpis`` chart and the text overview.
* **OpenAI only.** The single AI call goes through
  :func:`utils.ai_text.generate_json` (OpenAI structured outputs). No Gemini.
* **The model never invents numbers.** The system prompt pins it to the
  provided figures; ``metric`` is an OPTIONAL short figure the model lifts
  from the context, never fabricated.

Everything here is synchronous Django ORM — the GraphQL resolver and the cron
command wrap the entry points in ``sync_to_async`` / call them directly.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from django.utils import timezone

from recaps.tenant_overview import (
    tenant_event_recap_counts,
    tenant_kpi_totals,
    tenant_monthly_trend,
)
from utils.ai_text import generate_json

# Strict JSON Schema for the proactive-insights response (OpenAI "structured
# outputs"). The model MUST return ``{"insights": [ {title, detail,
# sentiment, metric} ]}``. ``strict`` mode requires every object to set
# ``additionalProperties: false`` and list ALL of its properties in
# ``required``; the optional ``metric`` figure expresses "absent" via a
# ``["string", "null"]`` union rather than by omission.
_INSIGHTS_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["insights"],
    "properties": {
        "insights": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "detail", "sentiment", "metric"],
                "properties": {
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "sentiment": {
                        "type": "string",
                        "enum": ["positive", "neutral", "attention"],
                    },
                    "metric": {"type": ["string", "null"]},
                },
            },
        },
    },
}

# Sentiments the frontend knows how to render. Anything else the model emits is
# normalised to "neutral" so a stray value can't break the badge.
_VALID_SENTIMENTS = frozenset({"positive", "neutral", "attention"})

_INSIGHTS_SYSTEM_PROMPT = (
    "You are a field-marketing analyst surfacing the most notable things "
    "about ONE client's activation program for their dashboard, using ONLY "
    "the numbers provided. Pick the 3-6 MOST notable items — wins, trends, "
    "standouts, and anything needing attention. Each item is a short title "
    "plus a single-sentence detail. Set `sentiment` to `positive` for a good "
    "result, `attention` for something that needs attention, and `neutral` "
    "otherwise. `metric` is an OPTIONAL short figure like \"+42% MoM\" or "
    "\"12,400 samples\" drawn straight from the provided numbers, or null "
    "when no single figure fits. NEVER invent or estimate numbers that are "
    "not present in the data. Return between 3 and 6 insights."
)


def _format_delta(latest: int, prior: int) -> str | None:
    """Human-readable month-over-month delta string, or None when not useful.

    Returns e.g. ``"+42% (1,200 -> 1,704)"`` or ``"-15% (200 -> 170)"``. When
    the prior month is zero we can't compute a percent, so we report the raw
    move (``"+170 (0 -> 170)"``); when both months are zero there's nothing to
    say and we return None so the prompt stays tight.
    """
    if latest == 0 and prior == 0:
        return None
    if prior == 0:
        sign = "+" if latest >= 0 else ""
        return f"{sign}{latest:,} ({prior:,} -> {latest:,})"
    pct = round((latest - prior) / prior * 100)
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct}% ({prior:,} -> {latest:,})"


def _compose_insights_prompt(tenant_id: int) -> str:
    """Render the compact numeric context the model reasons over.

    Pulls the headline counts, the nine summable KPIs, and the monthly trend
    from the shared :mod:`recaps.tenant_overview` helpers (so the figures match
    the ``tenantKpis`` chart and the text overview), then renders: the totals,
    the latest-month-vs-prior deltas for the charted metrics, and a short tail
    of the recent monthly trend. Bounded by construction — a handful of KPI
    lines plus at most the last few trend months — so the prompt stays small
    regardless of tenant size.
    """
    event_count, recap_count = tenant_event_recap_counts(tenant_id)
    k = tenant_kpi_totals(tenant_id)
    trend = tenant_monthly_trend(tenant_id)

    lines = [
        "Client program totals (all campaigns, events, and recaps):",
        f"- Events: {event_count}",
        f"- Recaps: {recap_count}",
        f"- Consumers reached: {k.consumers_reached}",
        f"- Samples distributed: {k.samples_distributed}",
        f"- Products sold: {k.products_sold}",
        f"- Cans sold: {k.cans_sold}",
        f"- Packs sold: {k.packs_sold}",
        f"- Total engagements: {k.total_engagements}",
        f"- First-time consumers: {k.first_time_consumers}",
        f"- Brand-aware consumers: {k.brand_aware_consumers}",
        f"- Willing to purchase: {k.willing_to_purchase}",
    ]

    # Latest-month-vs-prior deltas for the three charted activity metrics.
    if len(trend) >= 2:
        latest, prior = trend[-1], trend[-2]
        delta_lines = []
        for label, attr in (
            ("Recaps", "recaps"),
            ("Engagements", "engagements"),
            ("Samples", "samples"),
        ):
            delta = _format_delta(getattr(latest, attr), getattr(prior, attr))
            if delta is not None:
                delta_lines.append(f"- {label}: {delta}")
        if delta_lines:
            lines.append("")
            lines.append(
                f"Latest month ({latest.month}) vs prior ({prior.month}):"
            )
            lines.extend(delta_lines)

    # A short tail of the monthly trend so the model can spot a direction.
    recent = [m for m in trend if m.recaps or m.engagements or m.samples][-6:]
    if recent:
        lines.append("")
        lines.append("Recent monthly activity (month: recaps/engagements/samples):")
        lines.extend(
            f"- {m.month}: {m.recaps}/{m.engagements}/{m.samples}" for m in recent
        )

    return "\n".join(lines)


def _clean_insight(item: object) -> dict | None:
    """Coerce one model-supplied insight dict into a clean dict, or None.

    Defensive on purpose — even with strict structured outputs we never trust
    the shape blindly. Requires a non-empty ``title`` and ``detail``; clamps
    ``sentiment`` to a known value (defaulting to ``"neutral"``); and keeps
    ``metric`` only when it's a non-empty string (else null). Anything that
    can't produce a usable title+detail yields None and is dropped.
    """
    if not isinstance(item, dict):
        return None

    title = item.get("title")
    detail = item.get("detail")
    if not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(detail, str) or not detail.strip():
        return None

    sentiment = item.get("sentiment")
    if sentiment not in _VALID_SENTIMENTS:
        sentiment = "neutral"

    metric = item.get("metric")
    metric = metric.strip() if isinstance(metric, str) and metric.strip() else None

    return {
        "title": title.strip(),
        "detail": detail.strip(),
        "sentiment": sentiment,
        "metric": metric,
    }


def build_tenant_insights(tenant_id: int) -> list[dict]:
    """Generate the proactive insights list for one tenant (or ``[]``).

    Assembles the compact numeric context (see :func:`_compose_insights_prompt`)
    and asks OpenAI for the 3-6 most notable items via
    :func:`utils.ai_text.generate_json` with :data:`_INSIGHTS_JSON_SCHEMA`.
    Returns the cleaned list of insight dicts
    (``{title, detail, sentiment, metric}``), or ``[]`` on ANY failure — an
    unconfigured key, an upstream/model error, or an unparseable response.

    Synchronous Django ORM + one HTTP call; callers wrap it as needed.
    """
    try:
        user_prompt = _compose_insights_prompt(tenant_id)
        result = generate_json(
            _INSIGHTS_SYSTEM_PROMPT,
            user_prompt,
            schema=_INSIGHTS_JSON_SCHEMA,
        )
    except Exception:
        # generate_json raises AiUnavailable only when the key is missing;
        # any aggregation hiccup also lands here. Either way: degrade to [].
        return []

    if not isinstance(result, dict):
        return []

    raw = result.get("insights")
    if not isinstance(raw, list):
        return []

    cleaned: list[dict] = []
    for item in raw:
        insight = _clean_insight(item)
        if insight is not None:
            cleaned.append(insight)
    return cleaned


def get_or_refresh_tenant_insights(
    tenant_id: int, max_age_hours: int = 24
) -> tuple[list[dict], datetime | None]:
    """Serve cached insights for a tenant, refreshing when stale.

    The cache front door for the dashboard:

    * If the newest :class:`tenants.models.TenantInsightSnapshot` for the
      tenant is younger than ``max_age_hours``, return its items + timestamp
      (a fast read, no AI call).
    * Otherwise generate a fresh set via :func:`build_tenant_insights`, persist
      a new snapshot, and return it. ``max_age_hours=0`` forces a refresh — the
      mode the daily cron uses to precompute.
    * If generation fails (returns ``[]``), fall back to the most recent
      existing snapshot even if it's stale, so the dashboard keeps showing the
      last good insights rather than going blank.
    * If there's nothing to serve at all, return ``([], None)``.

    NEVER raises: any error (DB hiccup, AI failure) degrades to the best
    available value. ``datetime`` is the served snapshot's ``generated_at`` (or
    None when there are no insights to show).
    """
    # Imported lazily so this module stays importable without Django apps
    # loaded (e.g. for unit-testing the pure prompt/clean helpers).
    from tenants.models import TenantInsightSnapshot

    try:
        latest = (
            TenantInsightSnapshot.objects.filter(tenant_id=tenant_id)
            .order_by("-generated_at")
            .first()
        )

        # Fresh enough to serve straight from cache.
        if latest is not None and max_age_hours > 0:
            cutoff = timezone.now() - timedelta(hours=max_age_hours)
            if latest.generated_at >= cutoff:
                items = latest.items if isinstance(latest.items, list) else []
                return items, latest.generated_at

        # Stale (or forced refresh): try to generate a new set.
        items = build_tenant_insights(tenant_id)
        if items:
            snapshot = TenantInsightSnapshot.objects.create(
                tenant_id=tenant_id, items=items
            )
            return snapshot.items, snapshot.generated_at

        # Generation produced nothing — fall back to the last good snapshot
        # (even if stale) so the dashboard doesn't go blank.
        if latest is not None:
            stale_items = latest.items if isinstance(latest.items, list) else []
            return stale_items, latest.generated_at

        return [], None
    except Exception:
        # Belt-and-suspenders: never let a DB / unexpected error escape. Try
        # one more time to surface whatever snapshot already exists.
        try:
            fallback = (
                TenantInsightSnapshot.objects.filter(tenant_id=tenant_id)
                .order_by("-generated_at")
                .first()
            )
            if fallback is not None:
                items = fallback.items if isinstance(fallback.items, list) else []
                return items, fallback.generated_at
        except Exception:
            pass
        return [], None
