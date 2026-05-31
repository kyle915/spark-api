"""Tenant-wide activity overview for the freeform Q&A feature.

This is the client-level sibling of :mod:`recaps.report_service` (which
rolls up ONE :class:`events.models.Request` into a campaign report). Here
we summarize a *whole tenant's* program — every campaign, every event,
every recap — into a single compact plaintext block that
:func:`recaps.report_types.tenant_ai_answer` hands to Gemini as the only
source of truth for the model's answer.

Design rules:

* **Efficient ORM aggregation, never load every recap into Python.** The
  headline counts and the summable KPIs come from ``Count`` / ``Sum``
  annotations evaluated in the database. A tenant with 50k recaps does
  NOT pull 50k rows into the request process. The only rows we ever
  materialize are the small, hard-capped tails: the ten most recent
  events and (where they can't be summed in SQL) the KPI-relevant
  custom-field VALUE rows + the recent consumer quotes.
* **Mirror the per-campaign KPI math.** The same nine KPIs
  ``report_service`` sums per campaign (consumers_reached,
  samples_distributed, products_sold, cans_sold, packs_sold,
  total_engagements, first_time_consumers, brand_aware_consumers,
  willing_to_purchase) are summed here across BOTH recap shapes — legacy
  :class:`recaps.models.Recap` (typed columns + the consumer/sample
  children) and custom-template :class:`recaps.models.CustomRecap` (one
  typed column + free-text ``CustomFieldValue`` rows, label-matched with
  the exact rules ``report_service`` / ``recaps.types`` use) — so the
  tenant block agrees with the campaign reports.
* **Bounded output regardless of tenant size.** Counts and sums are O(1)
  lines. The recent-events and quotes sections are hard-capped
  (:data:`MAX_RECENT_EVENTS` / :data:`MAX_RECENT_QUOTES`), and each quote
  is length-trimmed, so the whole block stays a few dozen lines and the
  Gemini prompt stays small whether the tenant ran 3 events or 30,000.

Everything here is synchronous Django ORM — the GraphQL resolver wraps the
single entry point :func:`build_tenant_overview` in ``sync_to_async``.
"""

from __future__ import annotations

import re

from django.db.models import Max, Min, Sum

from events.models import Event, Request
from recaps.models import (
    ConsumerEngagements,
    ConsumerFeedback,
    CustomFieldValue,
    CustomRecap,
    CustomRecapProductSample,
    ProductSamples,
    Recap,
)
from recaps.report_service import _format_date_range, _leading_int
from recaps.types import _consumers_sampled_from_fields, _sold_units_from_fields
from tenants.models import Tenant

# Hard caps so the prompt stays small no matter how big the tenant is.
MAX_RECENT_EVENTS = 10
MAX_RECENT_QUOTES = 10

# Trim any single quote so one rambling note can't dominate the block.
MAX_QUOTE_CHARS = 240


def _sum(queryset, field: str) -> int:
    """Database-side ``Sum`` of one nullable integer column, coerced to int.

    Returns 0 for an empty queryset / all-null column (``Sum`` yields
    ``None`` there). The aggregation runs in Postgres — the rows never
    enter Python.
    """
    total = queryset.aggregate(_t=Sum(field))["_t"]
    return int(total or 0)


def _legacy_kpis(tenant_id: int) -> dict[str, int]:
    """Sum the legacy :class:`recaps.models.Recap` KPIs for one tenant.

    Recap has no direct tenant FK, so every queryset is scoped through the
    event (``…__event__tenant_id`` / ``event__tenant_id``) — the same join
    ``tenants.insights`` and the recap lists use. Each line is a single
    aggregate query; no Recap row is loaded into Python.
    """
    recaps = Recap.objects.filter(event__tenant_id=tenant_id)
    engagements = ConsumerEngagements.objects.filter(
        recap__event__tenant_id=tenant_id
    )
    samples = ProductSamples.objects.filter(recap__event__tenant_id=tenant_id)
    return {
        "total_engagements": _sum(recaps, "total_engagements"),
        "products_sold": _sum(recaps, "products_sold"),
        "cans_sold": _sum(recaps, "total_cans_sold"),
        "packs_sold": _sum(recaps, "total_packs_sold"),
        "consumers_reached": _sum(engagements, "total_consumer"),
        "first_time_consumers": _sum(engagements, "first_time_consumers"),
        "brand_aware_consumers": _sum(engagements, "brand_aware_consumers"),
        "willing_to_purchase": _sum(engagements, "willing_to_purchase_consumers"),
        "samples_distributed": _sum(samples, "quantity"),
    }


# Custom recaps keep most KPIs as free-text CustomFieldValue rows keyed by
# the field NAME. They can't be summed in SQL, so we pull ONLY the
# KPI-relevant value rows (filtered by a name regex in the DB) and parse
# them in Python — a bounded slice, not the full recap tree. The patterns
# mirror recaps.report_service._custom_engagement_totals +
# recaps.types._sold_units_from_fields / _consumers_sampled_from_fields.
_CUSTOM_KPI_NAME_RE = re.compile(
    r"consumers sampled|first time|knew about|willing to purchase|cans?|packs?",
    re.IGNORECASE,
)


def _custom_kpis(tenant_id: int) -> dict[str, int]:
    """Sum the custom-template :class:`recaps.models.CustomRecap` KPIs.

    ``total_engagements`` is a typed column → summed in the DB. The four
    consumer metrics + sold units live in free-text ``CustomFieldValue``
    rows; we fetch only the KPI-relevant rows (``custom_field__name``
    matched by :data:`_CUSTOM_KPI_NAME_RE` in SQL), grouped per recap, and
    apply the same label/parse rules the per-campaign report uses so the
    totals agree. We never load a CustomRecap object — only the matched
    (recap_id, name, value) value rows.
    """
    out = {
        "total_engagements": _sum(
            CustomRecap.objects.filter(tenant_id=tenant_id), "total_engagements"
        ),
        "consumers_reached": 0,
        "first_time_consumers": 0,
        "brand_aware_consumers": 0,
        "willing_to_purchase": 0,
        "products_sold": 0,
        "cans_sold": 0,
        "packs_sold": 0,
        "samples_distributed": 0,
    }

    # Structured custom samples sum cleanly in SQL.
    structured_samples = _sum(
        CustomRecapProductSample.objects.filter(
            custom_recap__tenant_id=tenant_id
        ),
        "quantity",
    )

    # Pull only the KPI-relevant free-text value rows, grouped by recap so
    # the per-recap "consumers sampled" fallback (sold units + samples)
    # matches the campaign report's per-recap accumulation.
    rows = (
        CustomFieldValue.objects.filter(
            custom_recap__tenant_id=tenant_id,
            custom_field__name__iregex=_CUSTOM_KPI_NAME_RE.pattern,
        )
        .values_list("custom_recap_id", "custom_field__name", "value")
        .order_by("custom_recap_id")
    )

    per_recap: dict[int, list[tuple[str | None, str | None]]] = {}
    for recap_id, name, value in rows.iterator():
        per_recap.setdefault(recap_id, []).append((name, value))

    sampled_total = 0
    for pairs in per_recap.values():
        for name, value in pairs:
            label = (name or "").lower()
            val = _leading_int(value)
            if val is None:
                continue
            if "first time" in label:
                out["first_time_consumers"] += val
            elif "knew about" in label:
                out["brand_aware_consumers"] += val
            elif "willing to purchase" in label and "not" not in label:
                out["willing_to_purchase"] += val

        consumers_sampled = _consumers_sampled_from_fields(pairs)
        if consumers_sampled is not None:
            out["consumers_reached"] += int(consumers_sampled)
            sampled_total += int(consumers_sampled)

        sold = _sold_units_from_fields(pairs)
        if sold is not None:
            out["products_sold"] += int(sold)
        for name, value in pairs:
            label = (name or "").lower()
            parsed = _leading_int(value)
            if parsed is None:
                continue
            if re.search(r"\bcans?\b", label):
                out["cans_sold"] += parsed
            elif re.search(r"\bpacks?\b", label):
                out["packs_sold"] += parsed

    # samplesDistributed prefers structured quantities; fall back to the
    # summed "consumers sampled" headline when no structured samples exist
    # (mirrors report_service._accumulate_custom).
    out["samples_distributed"] = structured_samples or sampled_total
    return out


def _combined_kpis(tenant_id: int) -> dict[str, int]:
    """Legacy + custom KPI totals, summed field-by-field."""
    legacy = _legacy_kpis(tenant_id)
    custom = _custom_kpis(tenant_id)
    keys = (
        "consumers_reached",
        "samples_distributed",
        "products_sold",
        "cans_sold",
        "packs_sold",
        "total_engagements",
        "first_time_consumers",
        "brand_aware_consumers",
        "willing_to_purchase",
    )
    return {k: int(legacy.get(k, 0)) + int(custom.get(k, 0)) for k in keys}


def _recent_event_lines(tenant_id: int) -> list[str]:
    """Up to :data:`MAX_RECENT_EVENTS` recent events as 'name · date · city, ST'.

    Ordered most-recent-first by event date (NULL dates last). Only the
    capped slice is materialized; ``select_related`` avoids per-row joins.
    """
    events = (
        Event.objects.filter(tenant_id=tenant_id)
        .select_related("location", "state")
        .order_by("-date", "-id")[:MAX_RECENT_EVENTS]
    )
    lines: list[str] = []
    for ev in events:
        name = (getattr(ev, "name", None) or "").strip() or "(event)"
        date_val = getattr(ev, "date", None)
        date_str = date_val.strftime("%b %-d, %Y") if date_val else "no date"
        city = getattr(getattr(ev, "location", None), "name", None)
        state = getattr(getattr(ev, "state", None), "name", None)
        where = ", ".join(p for p in (city, state) if p) or "location n/a"
        lines.append(f"- {name} · {date_str} · {where}")
    return lines


def _clean_quote(text: str | None) -> str | None:
    """Collapse whitespace + length-trim a single quote (None if empty)."""
    if not text:
        return None
    cleaned = " ".join(str(text).split()).strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_QUOTE_CHARS:
        cleaned = cleaned[: MAX_QUOTE_CHARS - 1].rstrip() + "…"
    return cleaned


def _recent_quote_lines(tenant_id: int) -> list[str]:
    """Up to :data:`MAX_RECENT_QUOTES` recent consumer quotes/highlights.

    Pulls only the ``quotes`` / ``positive_stories`` text columns from the
    most recent legacy :class:`recaps.models.ConsumerFeedback` rows (scoped
    through ``recap__event__tenant_id``), deduped on cleaned text. Custom
    recaps store highlights as free-text fields with no reliable typed
    column, so — to keep this bounded and cheap — the quotes section draws
    from legacy feedback only; the KPI sums above still cover both shapes.
    """
    rows = (
        ConsumerFeedback.objects.filter(recap__event__tenant_id=tenant_id)
        .order_by("-created_at", "-id")
        .values_list("quotes", "positive_stories")
    )
    lines: list[str] = []
    seen: set[str] = set()
    for quotes, stories in rows.iterator():
        for raw in (quotes, stories):
            cleaned = _clean_quote(raw)
            if not cleaned:
                continue
            key = cleaned.lower()
            if key in seen:
                continue
            seen.add(key)
            lines.append(f'- "{cleaned}"')
            if len(lines) >= MAX_RECENT_QUOTES:
                return lines
    return lines


def build_tenant_overview(tenant_id: int) -> str:
    """Render a compact plaintext summary of ONE tenant's whole dataset.

    Includes the brand/tenant name; headline totals (# campaigns, #
    events, # recaps, overall activity date range); the nine summed KPIs
    (same field set as the per-campaign report, across both recap shapes);
    up to ten most recent events; and up to ten recent consumer quotes.

    Every total is a database aggregate and the two list sections are
    hard-capped, so the output stays a few dozen lines regardless of how
    much activity the tenant has — keeping the downstream Gemini prompt
    small. Raises :class:`tenants.models.Tenant.DoesNotExist` if no tenant
    matches ``tenant_id`` (the resolver translates that to a degradation
    reason).
    """
    tenant = Tenant.objects.get(id=tenant_id)

    # Headline counts — each a single COUNT(*) in the DB.
    campaign_count = Request.objects.filter(
        tenant_id=tenant_id, deleted_at__isnull=True
    ).count()
    event_count = Event.objects.filter(tenant_id=tenant_id).count()
    legacy_recap_count = Recap.objects.filter(event__tenant_id=tenant_id).count()
    custom_recap_count = CustomRecap.objects.filter(tenant_id=tenant_id).count()
    recap_count = legacy_recap_count + custom_recap_count

    # Overall activity date range over the tenant's events (reuse the
    # campaign report's label formatter). Min/Max are computed in the DB —
    # a single aggregate query, no event rows pulled into Python.
    span = Event.objects.filter(tenant_id=tenant_id, date__isnull=False).aggregate(
        _lo=Min("date"), _hi=Max("date")
    )
    lo, hi = span["_lo"], span["_hi"]
    if lo and hi:
        # Synthesize the two endpoints into the shape _format_date_range
        # expects (objects exposing a `.date` datetime).
        class _D:
            def __init__(self, d):
                self.date = d

        date_range = _format_date_range([_D(lo), _D(hi)])
    else:
        date_range = None

    kpis = _combined_kpis(tenant_id)

    lines = [
        f"Brand: {tenant.name or 'N/A'}",
        f"Campaigns (requests): {campaign_count}",
        f"Events: {event_count}",
        f"Recaps: {recap_count}",
        f"Activity date range: {date_range or 'N/A'}",
        "",
        "Aggregate KPIs across all recaps:",
        f"- Consumers reached: {kpis['consumers_reached']}",
        f"- Samples distributed: {kpis['samples_distributed']}",
        f"- Products sold: {kpis['products_sold']}",
        f"- Cans sold: {kpis['cans_sold']}",
        f"- Packs sold: {kpis['packs_sold']}",
        f"- Total engagements: {kpis['total_engagements']}",
        f"- First-time consumers: {kpis['first_time_consumers']}",
        f"- Brand-aware consumers: {kpis['brand_aware_consumers']}",
        f"- Willing to purchase: {kpis['willing_to_purchase']}",
    ]

    event_lines = _recent_event_lines(tenant_id)
    if event_lines:
        lines.append("")
        lines.append(f"Most recent events (up to {MAX_RECENT_EVENTS}):")
        lines.extend(event_lines)

    quote_lines = _recent_quote_lines(tenant_id)
    if quote_lines:
        lines.append("")
        lines.append(f"Recent consumer quotes (up to {MAX_RECENT_QUOTES}):")
        lines.extend(quote_lines)

    return "\n".join(lines)
