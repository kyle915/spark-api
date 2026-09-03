"""Proactive "what's notable" insights for one tenant — DETERMINISTIC.

The dashboard surfaces a short list of headline observations about a client's
activation program — consumers sampled, samples handed out (when distinct),
sales, new audience, and momentum — WITHOUT the user asking. This module owns
the computation of those buckets.

This used to ask OpenAI for 3–6 free-form themes. That was inconsistent
run-to-run and, worse, once dramatized the CURRENT/empty month (a month that
hasn't started yet) as a scary "-100% collapse". It has been replaced with
FIXED, deterministic, templated buckets computed straight from the shared
:mod:`recaps.tenant_overview` aggregates — no AI call, no token cost, and the
same numbers the ``tenantKpis`` charts show.

* :func:`build_insight_buckets` — the deterministic builder. Returns ``[]`` for
  a tenant with no activity, otherwise the fixed buckets:
  ``reach`` (consumers sampled), optional ``sampling`` only when sample units
  differ from consumers, ``sales``, ``new_audience``, ``momentum`` — except
  ``momentum`` is omitted when the tenant has fewer than one *active* month
  (so we never emit a misleading card). Each bucket is a dict
  ``{key, title, detail, sentiment, metric}`` with every number formatted with
  thousands separators; a number is NEVER fabricated. The same headcount is
  NEVER labeled both "consumers reached" and "samples handed out".
* :func:`build_tenant_insights` — thin back-compat wrapper that returns
  :func:`build_insight_buckets` (so the snapshot/cron path keeps working
  without any AI code). It never raises; any error degrades to ``[]``.
* :func:`get_or_refresh_tenant_insights` — the snapshot front door, retained
  so the cron command and any cached path keep a stable signature. Buckets are
  cheap and deterministic, so it simply computes them live and (when there is
  something to show) persists a snapshot; it NEVER raises.

Design rules (mirroring the rest of the report surface):

* **Reuse, don't re-aggregate.** Every number comes from
  :func:`recaps.tenant_overview.tenant_kpi_totals`,
  :func:`recaps.tenant_overview.tenant_event_recap_counts`, and
  :func:`recaps.tenant_overview.tenant_monthly_trend`, so the buckets agree
  with the ``tenantKpis`` chart and the text overview.
* **Never invent a number, never dramatize an empty month.** The Momentum
  bucket compares only months that actually have activity, so the empty
  current/future month can never become a "-100%" card. Absurd MoM % swings
  are capped or suppressed (collection-method / sparse-base honesty).
* **One definition per metric.** Consumers sampled ≠ samples handed out;
  when the API mirrors one into the other, Insights shows a single card.

Everything here is synchronous Django ORM — the GraphQL resolver computes the
buckets live and the cron command calls the entry points directly.
"""

from __future__ import annotations

from datetime import datetime

from django.utils import timezone

from recaps.tenant_overview import (
    tenant_event_recap_counts,
    tenant_kpi_totals,
    tenant_monthly_trend,
)

# Sentiments the frontend knows how to render. Buckets only ever set one of
# these three; anything else would be normalised to "neutral" downstream.
_VALID_SENTIMENTS = frozenset({"positive", "neutral", "attention"})

# A month-over-month drop at least this steep flips Momentum to "attention"
# (a meaningful decline worth a callout) rather than plain "neutral".
_MOMENTUM_ATTENTION_PCT = -25

# Absolute MoM % beyond this is not trustworthy as organic growth (collection
# method change / bulk import / sparse prior). Cap the chip and annotate.
_ABSURD_PCT_THRESHOLD = 500

# Prior-period engagement floor: below this AND a large ratio → suppress %.
_SPARSE_PREV_MAX = 20
_SPARSE_RATIO_THRESHOLD = 10

# Abbreviated month names indexed 1..12 (index 0 unused), for "2026-04" -> "Apr".
_MONTH_ABBR = (
    "",
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def _format_delta(latest: int, prior: int) -> str | None:
    """Human-readable month-over-month delta string, or None when not useful.

    Returns e.g. ``"+42% (1,200 -> 1,704)"`` or ``"-15% (200 -> 170)"``. When
    the prior month is zero we can't compute a percent, so we report the raw
    move (``"+170 (0 -> 170)"``); when both months are zero there's nothing to
    say and we return None.

    Absurd swings (``|pct| > 500``) or sparse priors are annotated rather than
    printed as fake-precision 2000%+ figures — same honesty gate as Momentum
    and the front-end period comparison chips.

    Retained from the original AI-prompt module per request as a generic,
    reusable month-over-month delta formatter (e.g. for any future caller that
    wants this verbose ``"+42% (a -> b)"`` form). The Momentum bucket computes
    its own compact ``"▲ 12% vs Apr"`` figure inline because it needs the
    arrow + absolute-percent rendering this verbose form doesn't produce.
    """
    if latest == 0 and prior == 0:
        return None
    if prior == 0:
        sign = "+" if latest >= 0 else ""
        return f"{sign}{latest:,} ({prior:,} -> {latest:,})"
    pct = round((latest - prior) / prior * 100)
    ratio = latest / prior if prior else 0
    if prior <= _SPARSE_PREV_MAX and ratio >= _SPARSE_RATIO_THRESHOLD and latest > prior:
        return (
            f"n/a ({prior:,} -> {latest:,}; base period sparse / collection changed)"
        )
    if abs(pct) > _ABSURD_PCT_THRESHOLD:
        sign = "+" if pct >= 0 else "-"
        return (
            f"{sign}>{_ABSURD_PCT_THRESHOLD}% ({prior:,} -> {latest:,}; "
            f"large change — may reflect new collection, not organic growth)"
        )
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct}% ({prior:,} -> {latest:,})"


def _short_month(month: str) -> str:
    """Shorten a ``"YYYY-MM"`` trend key to a month abbreviation (``"Apr"``).

    Falls back to the raw input if it can't be parsed, so a malformed key
    never raises — it just renders verbatim.
    """
    try:
        _year, mm = month.split("-")
        idx = int(mm)
    except (ValueError, AttributeError):
        return month
    if 1 <= idx <= 12:
        return _MONTH_ABBR[idx]
    return month


def _momentum_bucket(trend: list) -> dict | None:
    """Build the Momentum bucket from the monthly trend, or None to skip it.

    THE fix for the old "-100% halted" bug: we only ever look at months that
    actually have activity (any of recaps / engagements / samples > 0), so the
    empty current/future month at the tail of the trend can never be compared
    against a real prior month and dramatized as a collapse.

    * ``>= 2`` active months — compare the latest active month's engagements
      against the previous active month's: ``▲``/``▼``/``▬`` + percent vs the
      prior active month's short name; detail names the latest active month,
      its engagements, and the direction. Sentiment is ``positive`` when up,
      ``attention`` on a meaningful drop (<= -25%), else ``neutral``.
      Absurd / sparse swings are capped or suppressed (never 2000%+ chips).
    * ``== 1`` active month — no comparison to make; report it as the peak so
      far. Sentiment ``neutral``.
    * ``0`` active months — return None so no (misleading) card is emitted.
    """
    active = [m for m in trend if m.recaps or m.engagements or m.samples]
    if not active:
        return None

    latest = active[-1]
    latest_short = _short_month(latest.month)

    if len(active) == 1:
        return {
            "key": "momentum",
            "title": "Momentum",
            "metric": f"Peak: {latest_short}",
            "detail": (
                f"Strongest month so far: {latest.month} with "
                f"{latest.engagements:,} engagements."
            ),
            "sentiment": "neutral",
        }

    prior = active[-2]
    prior_short = _short_month(prior.month)
    latest_eng = latest.engagements
    prior_eng = prior.engagements

    # Percent move on engagements, latest active month vs prior active month.
    # When the prior active month had zero engagements we can't express a
    # percent, so fall back to a flat "▬ vs <prior>" marker rather than a
    # bogus number — and never a negative one (the empty-month guard above
    # already prevents the latest from being the empty tail).
    if prior_eng == 0:
        return {
            "key": "momentum",
            "title": "Momentum",
            "metric": f"▬ vs {prior_short}",
            "detail": (
                f"Engagements in {latest.month}: {latest_eng:,} "
                f"(prior active month {prior.month} had none logged)."
            ),
            "sentiment": "neutral",
        }

    pct = round((latest_eng - prior_eng) / prior_eng * 100)
    ratio = latest_eng / prior_eng

    # Sparse prior + huge jump → don't invent a growth rate.
    if (
        prior_eng <= _SPARSE_PREV_MAX
        and ratio >= _SPARSE_RATIO_THRESHOLD
        and latest_eng > prior_eng
    ):
        return {
            "key": "momentum",
            "title": "Momentum",
            "metric": f"n/a vs {prior_short}",
            "detail": (
                f"Engagements {latest_eng:,} in {latest.month} vs "
                f"{prior_eng:,} in {prior.month} — base period sparse / "
                f"collection changed; % not shown."
            ),
            "sentiment": "neutral",
        }

    # Absurd swing off a solid prior → cap the chip, annotate the detail.
    if abs(pct) > _ABSURD_PCT_THRESHOLD:
        arrow = "▲" if pct > 0 else "▼"
        direction = "up" if pct > 0 else "down"
        sentiment = (
            "attention"
            if pct <= _MOMENTUM_ATTENTION_PCT
            else ("positive" if pct > 0 else "neutral")
        )
        return {
            "key": "momentum",
            "title": "Momentum",
            "metric": f"{arrow} >{_ABSURD_PCT_THRESHOLD}% vs {prior_short}",
            "detail": (
                f"Engagements {direction} sharply in {latest.month} "
                f"({latest_eng:,} vs {prior_eng:,} in {prior.month}). "
                f"Large change — may reflect new collection, not organic growth."
            ),
            "sentiment": sentiment,
        }

    if pct > 0:
        arrow = "▲"
        direction = "up"
        sentiment = "positive"
    elif pct < 0:
        arrow = "▼"
        direction = "down"
        sentiment = "attention" if pct <= _MOMENTUM_ATTENTION_PCT else "neutral"
    else:
        arrow = "▬"
        direction = "flat"
        sentiment = "neutral"

    metric = f"{arrow} {abs(pct)}% vs {prior_short}"
    detail = (
        f"Engagements {direction} {abs(pct)}% in {latest.month} "
        f"({latest_eng:,} vs {prior_eng:,} in {prior.month})."
    )
    return {
        "key": "momentum",
        "title": "Momentum",
        "metric": metric,
        "detail": detail,
        "sentiment": sentiment,
    }


def build_insight_buckets(tenant_id: int) -> list[dict]:
    """Deterministic proactive-insight buckets for one tenant (or ``[]``).

    Pulls the headline counts, the nine summable KPIs, and the monthly trend
    from the shared :mod:`recaps.tenant_overview` helpers (so every figure
    matches the ``tenantKpis`` chart) and assembles the fixed buckets.

    Returns ``[]`` when the tenant has NO activity at all (no events, no
    recaps, and every KPI total zero). Otherwise returns the buckets in this
    order — ``reach`` (consumers sampled), optional ``sampling`` only when
    samples handed out is a DISTINCT count, ``sales``, ``new_audience``,
    ``momentum`` — each a dict ``{key, title, detail, sentiment, metric}`` with
    every number formatted with thousands separators. ``momentum`` is omitted
    when the tenant has fewer than one active month (see
    :func:`_momentum_bucket`), so the card count is three to five.

    Synchronous Django ORM; callers wrap it as needed. Numbers are only ever
    read from the aggregates — never fabricated.
    """
    event_count, recap_count = tenant_event_recap_counts(tenant_id)
    k = tenant_kpi_totals(tenant_id)
    trend = tenant_monthly_trend(tenant_id)

    # No activity anywhere -> nothing to surface (avoids a wall of zero cards).
    if (
        event_count == 0
        and recap_count == 0
        and not any(
            (
                k.consumers_reached,
                k.samples_distributed,
                k.products_sold,
                k.cans_sold,
                k.packs_sold,
                k.total_engagements,
                k.first_time_consumers,
                k.brand_aware_consumers,
                k.willing_to_purchase,
            )
        )
    ):
        return []

    buckets: list[dict] = []

    # Consumers sampled (people). The API often mirrors this into
    # samples_distributed when no separate sample-unit count exists ("kyle's
    # rule"). Showing the same number twice as "reach" AND "samples handed
    # out" is a lie — collapse to one consumers-sampled card unless the
    # sample-unit total is actually distinct.
    consumers = k.consumers_reached or k.samples_distributed
    samples = k.samples_distributed
    samples_distinct = samples > 0 and samples != consumers

    reach_detail = (
        f"{consumers:,} consumers sampled across "
        f"{event_count:,} events"
    )
    if k.total_engagements > 0 and k.total_engagements != consumers:
        reach_detail += f" · {k.total_engagements:,} engagements"
    reach_detail += "."
    buckets.append(
        {
            "key": "reach",
            "title": "Consumers sampled",
            "metric": f"{consumers:,}",
            "detail": reach_detail,
            "sentiment": "positive" if consumers > 0 else "neutral",
        }
    )

    # Samples handed out (units) — only when distinct from consumers sampled.
    if samples_distinct:
        sampling_detail = f"{samples:,} samples handed out"
        if event_count > 0:
            avg = round(samples / event_count)
            sampling_detail += f", ~{avg:,}/event"
        sampling_detail += "."
        buckets.append(
            {
                "key": "sampling",
                "title": "Samples handed out",
                "metric": f"{samples:,}",
                "detail": sampling_detail,
                "sentiment": "positive",
            }
        )

    # 3) Sales.
    sales_detail = f"{k.products_sold:,} products sold"
    if k.cans_sold > 0 or k.packs_sold > 0:
        sales_detail += f" ({k.cans_sold:,} cans · {k.packs_sold:,} packs)"
    sales_detail += "."
    buckets.append(
        {
            "key": "sales",
            "title": "Sales",
            "metric": f"{k.products_sold:,}",
            "detail": sales_detail,
            "sentiment": "positive" if k.products_sold > 0 else "neutral",
        }
    )

    # 4) New audience.
    buckets.append(
        {
            "key": "new_audience",
            "title": "New audience",
            "metric": f"{k.first_time_consumers:,}",
            "detail": (
                f"{k.first_time_consumers:,} first-time consumers · "
                f"{k.brand_aware_consumers:,} brand-aware · "
                f"{k.willing_to_purchase:,} willing to purchase."
            ),
            "sentiment": "positive" if k.first_time_consumers > 0 else "neutral",
        }
    )

    # 5) Momentum — only when there is at least one active month to describe.
    momentum = _momentum_bucket(trend)
    if momentum is not None:
        buckets.append(momentum)

    return buckets


def build_tenant_insights(tenant_id: int) -> list[dict]:
    """Back-compat entry point — now the deterministic buckets, never AI.

    Kept so the snapshot/cron path keeps a stable name. Delegates to
    :func:`build_insight_buckets` and returns ``[]`` on ANY error (matching
    the original never-raise contract) so a single tenant's data hiccup can't
    abort a batch refresh.
    """
    try:
        return build_insight_buckets(tenant_id)
    except Exception:
        return []


def get_or_refresh_tenant_insights(
    tenant_id: int, max_age_hours: int = 24
) -> tuple[list[dict], datetime | None]:
    """Compute (and snapshot) the deterministic insight buckets for a tenant.

    Retained as the snapshot front door so the daily cron command keeps the
    same call. Now that the buckets are deterministic and cheap there is no AI
    call to amortise, so this simply:

    * computes the buckets live via :func:`build_tenant_insights`;
    * persists a fresh :class:`tenants.models.TenantInsightSnapshot` when there
      is something to show (so the cron keeps producing the historical record
      / cache rows other code may read), and returns those items + timestamp;
    * when there are no buckets, returns ``([], now)`` and writes nothing.

    ``max_age_hours`` is accepted for signature compatibility but no longer
    gates a network call (deterministic compute is always fresh). NEVER raises:
    any DB/compute error degrades to ``([], None)``.
    """
    # Imported lazily so this module stays importable without Django apps
    # loaded (e.g. for unit-testing the pure bucket/helper functions).
    from tenants.models import TenantInsightSnapshot

    try:
        items = build_tenant_insights(tenant_id)
        if items:
            snapshot = TenantInsightSnapshot.objects.create(
                tenant_id=tenant_id, items=items
            )
            return snapshot.items, snapshot.generated_at
        return [], timezone.now()
    except Exception:
        return [], None
