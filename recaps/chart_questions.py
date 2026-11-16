"""A question about one chart, answered from that chart's own figures.

The browser sends which chart it is drawing (tenant, dates, chart kind),
never the figures. The figures are worked out again here, by the same
function the chart's own query uses, so the answer is about exactly what
is on screen and nothing a browser sent is trusted.

Everything a reader is likely to ask (total, highest, lowest, each row's
share, week-on-week change) is computed here and handed to the model as
lines of fact. The model picks from them. Its answer is kept only when
every number in it is one of those lines' numbers; otherwise the reader
gets the plain summary built here. A question the chart cannot answer gets
one line saying so and pointing to Ask AI.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from django.utils import timezone

from recaps import explain_text
from recaps.explain_text import n, pct, plural

CHART_WEEK = "consumers_by_week"
CHART_STORE = "consumers_by_store"
CHART_STATE = "consumers_by_state"
CHART_PRODUCT = "samples_by_product"
CHART_KINDS = (CHART_WEEK, CHART_STORE, CHART_STATE, CHART_PRODUCT)

KIND = "chart_answer"
MAX_QUESTION_CHARS = 300
# New questions one person may ask in an hour. A repeat of a question whose
# answer is already stored does not count.
QUESTIONS_PER_HOUR = 20

CANNOT_ANSWER = (
    "This chart's figures cannot answer that. Ask AI can look across the "
    "whole program."
)
RATE_LIMITED = "That is a lot of questions in an hour. Try again a little later."
NO_DATA = "This chart has no figures for these dates."


@dataclass(frozen=True)
class ChartData:
    title: str
    measure: str
    noun: str
    rows: list[tuple[str, int]]
    # Rows that are not a named thing ("Other stores", "No store recorded").
    asides: list[tuple[str, int]]


def chart_data(tenant_id: int, chart: str, start: date | None, end: date | None) -> ChartData | None:
    """The labels and values ``chart`` draws, from the chart's own source."""
    if chart in (CHART_WEEK, CHART_STORE, CHART_STATE):
        from recaps.consumer_breakdown import (
            KIND_MISSING,
            KIND_NAMED,
            tenant_consumer_breakdown,
        )

        data = tenant_consumer_breakdown(tenant_id, start, end)
        if chart == CHART_WEEK:
            rows = [
                (f"Week of {_day(date.fromisoformat(w['week_start']))}", w["consumers"])
                for w in data["by_week"]
            ]
            return ChartData("Consumers sampled per week", "consumers sampled", "week", rows, [])
        split = data["by_store" if chart == CHART_STORE else "by_state"]
        noun = "store" if chart == CHART_STORE else "state"
        rows = [(r["label"], r["consumers"]) for r in split if r["kind"] == KIND_NAMED]
        asides = [
            (
                f"No {noun} recorded" if r["kind"] == KIND_MISSING else f"All other {noun}s",
                r["consumers"],
            )
            for r in split
            if r["kind"] != KIND_NAMED
        ]
        return ChartData(f"Consumers sampled by {noun}", "consumers sampled", noun, rows, asides)
    if chart == CHART_PRODUCT:
        from recaps.tenant_overview import tenant_sku_pulse

        pulse = tenant_sku_pulse(tenant_id, start, end)
        rows = [(i["product"], i["samples"]) for i in pulse["items"]]
        rest = pulse["total_samples"] - sum(v for _, v in rows)
        asides = [("All other products", rest)] if rest > 0 else []
        return ChartData("Samples handed out by product", "samples handed out", "product", rows, asides)
    return None


def _day(day: date) -> str:
    return f"{day:%B} {day.day}"


def summary(data: ChartData) -> str:
    """The plain sentence a reader gets without the model."""
    rows = data.rows
    total = sum(v for _, v in rows) + sum(v for _, v in data.asides)
    if not rows or total <= 0:
        return NO_DATA
    top_label, top_value = max(rows, key=lambda r: r[1])
    text = f"This chart shows {n(total)} {data.measure} across {plural(len(rows), data.noun)}."
    if len(rows) > 1:
        low_label, low_value = min(rows, key=lambda r: r[1])
        text += (
            f" The highest is {top_label} with {n(top_value)}; "
            f"the lowest is {low_label} with {n(low_value)}."
        )
    return text


def fact_lines(data: ChartData, start: date | None, end: date | None) -> str:
    """Every figure the model may use, already worked out."""
    rows = data.rows
    total = sum(v for _, v in rows) + sum(v for _, v in data.asides)
    lines = [f"CHART: {data.title}."]
    if start and end:
        lines.append(f"DATES: {_day(start)} {start.year} to {_day(end)} {end.year}.")
    lines.append(f"TOTAL {data.measure}: {n(total)}.")
    lines.append(f"NUMBER OF {data.noun.upper()}S SHOWN: {len(rows)}.")
    if rows:
        average = round(sum(v for _, v in rows) / len(rows))
        lines.append(f"AVERAGE PER {data.noun.upper()}: {n(average)}.")
        top = max(rows, key=lambda r: r[1])
        low = min(rows, key=lambda r: r[1])
        lines.append(f"HIGHEST: {top[0]} with {n(top[1])}.")
        lines.append(f"LOWEST: {low[0]} with {n(low[1])}.")
    lines.append("ROWS (label: value, share of total):")
    ranked = rows if data.noun == "week" else sorted(rows, key=lambda r: -r[1])
    for position, (label, value) in enumerate(ranked, start=1):
        share = pct(round(value / total * 100, 1)) if total else "0"
        rank = "" if data.noun == "week" else f" rank {position},"
        lines.append(f"- {label}:{rank} {n(value)}, {share}% of the total")
    for label, value in data.asides:
        share = pct(round(value / total * 100, 1)) if total else "0"
        lines.append(f"- {label}: {n(value)}, {share}% of the total")
    if data.noun == "week" and len(rows) > 1:
        lines.append("CHANGE FROM ONE WEEK TO THE NEXT:")
        for (_, before), (label, now) in zip(rows, rows[1:]):
            if before > 0:
                move = round((now - before) / before * 100)
                word = "up" if move > 0 else "down" if move < 0 else "level, change"
                lines.append(
                    f"- {label}: {word} {abs(move)}% on the week before "
                    f"({n(now)} against {n(before)})"
                )
            else:
                lines.append(f"- {label}: {n(now)}, after a week with none")
    return "\n".join(lines)


_SYSTEM = (
    "You answer one question about one chart on a dashboard. You are given "
    "the chart's figures as FACTS, already worked out. Answer in one or two "
    "plain sentences using only those facts. Write every number in digits "
    "exactly as it appears in the facts. Never calculate, estimate, add, "
    "subtract, average or compare beyond what the facts already state. "
    "Never give a reason, a forecast or advice. If the facts do not answer "
    "the question, set answerable to false and leave the answer empty."
)

_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answerable", "answer"],
    "properties": {
        "answerable": {"type": "boolean"},
        "answer": {"type": "string"},
    },
}


def over_limit(user_id: int | None) -> bool:
    from recaps.models import DashboardText

    if not user_id:
        return False
    since = timezone.now() - timedelta(hours=1)
    asked = DashboardText.objects.filter(
        created_by_id=user_id, kind=KIND, updated_at__gte=since
    ).count()
    return asked >= QUESTIONS_PER_HOUR


def answer_chart_question(
    tenant_id: int,
    chart: str,
    start: date | None,
    end: date | None,
    question: str,
    *,
    user_id: int | None = None,
) -> dict:
    """``{ok, answered, answer, reason}``. ``answered`` is False when the
    reply is the "this chart cannot answer that" line or the plain summary
    standing in for a model answer."""
    from utils.ai_text import AiUnavailable, generate_json

    question = " ".join((question or "").split())[:MAX_QUESTION_CHARS]
    if not question:
        return {"ok": False, "answered": False, "answer": "", "reason": "A question is required."}
    if chart not in CHART_KINDS:
        return {"ok": False, "answered": False, "answer": "", "reason": "Unknown chart."}
    data = chart_data(tenant_id, chart, start, end)
    if data is None or not data.rows:
        return {"ok": True, "answered": False, "answer": NO_DATA, "reason": None}

    facts = fact_lines(data, start, end)
    digest = explain_text.inputs_hash(chart, facts, question.casefold())
    window = f"{start.isoformat() if start else ''}:{end.isoformat() if end else ''}"
    scope_key = f"{chart}:{window}:{digest[:16]}"
    cached = explain_text.read_cached(tenant_id, KIND, scope_key, digest)
    if isinstance(cached, dict) and "answer" in cached:
        return {
            "ok": True,
            "answered": bool(cached.get("answered")),
            "answer": cached["answer"],
            "reason": None,
        }

    if over_limit(user_id):
        return {"ok": False, "answered": False, "answer": "", "reason": RATE_LIMITED}

    plain = summary(data)
    parsed = None
    try:
        parsed = generate_json(
            _SYSTEM,
            f"FACTS:\n{facts}\n\nQUESTION: {question}",
            schema=_SCHEMA,
            max_tokens=600,
            timeout=explain_text.MODEL_TIMEOUT_SECONDS,
        )
    except AiUnavailable:
        parsed = None
    except Exception:  # noqa: BLE001
        parsed = None

    source = explain_text.SOURCE_TEMPLATE
    answered = False
    if not isinstance(parsed, dict):
        # No model: the reader still gets what the chart says.
        answer = plain
    elif parsed.get("answerable") is not True:
        answer, source = CANNOT_ANSWER, explain_text.SOURCE_MODEL
    else:
        text = " ".join(str(parsed.get("answer") or "").split())
        # The question is not a source of numbers: only the facts are.
        if (
            text
            and len(text) <= explain_text.MAX_SENTENCE_CHARS
            and not explain_text.unsupported_numbers(text, facts)
        ):
            answer, source, answered = text, explain_text.SOURCE_MODEL, True
        else:
            answer = plain
    explain_text.store(
        tenant_id,
        KIND,
        scope_key,
        digest,
        {"answer": answer, "answered": answered},
        source,
        user_id=user_id,
    )
    return {"ok": True, "answered": answered, "answer": answer, "reason": None}
