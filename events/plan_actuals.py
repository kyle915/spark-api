"""Recap actuals rolled up per field-marketing plan.

Only approved, non-archived attached recaps count — the same gate clients
see on ``Event.recaps``. Values are re-read from each recap on every call,
limited to the metrics ops ticked when attaching.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from django.utils import timezone

from recaps import plan_metrics as pm
from recaps.models import PlanRecapLink

RECAP_RELATED = (
    "event",
    "event__request",
    "event__request__request_type",
    "event__event_type",
    "event__custom_recap_template",
    "event__rmm_asigned",
    "event__state",
    "event__tenant",
    "ambassador__user",
)


def links_for_plans(plan_ids) -> list[PlanRecapLink]:
    return list(
        PlanRecapLink.objects.filter(plan_id__in=list(plan_ids))
        .select_related(
            *(f"recap__{path}" for path in RECAP_RELATED),
            *(f"custom_recap__{path}" for path in RECAP_RELATED),
            "custom_recap__custom_recap_template",
            "custom_recap__custom_recap_template__event_type",
        )
        .order_by("attached_at", "id")
    )


def link_recap(link: PlanRecapLink):
    if link.custom_recap_id:
        return link.custom_recap, pm.KIND_CUSTOM
    return link.recap, pm.KIND_LEGACY


def counts_toward_results(recap) -> bool:
    return bool(recap.approved) and recap.archived_at is None


def selected_metrics(link: PlanRecapLink) -> list[pm.RecapMetric]:
    recap, kind = link_recap(link)
    keys = set(link.metric_keys or [])
    return [m for m in pm.recap_metrics(recap, kind) if m.key in keys]


def recap_day(recap) -> date:
    event = getattr(recap, "event", None)
    when = None
    if event is not None:
        when = event.date or event.start_time
    when = when or recap.created_at or timezone.now()
    return timezone.localtime(when).date()


@dataclass
class Total:
    key: str
    label: str
    value: float | None = None
    texts: list[str] = field(default_factory=list)
    parts: dict[str, int] = field(default_factory=dict)


def sum_metrics(metric_lists) -> dict[str, Total]:
    out: dict[str, Total] = {}
    for metrics in metric_lists:
        for metric in metrics:
            total = out.setdefault(metric.key, Total(metric.key, metric.label))
            if metric.value is not None:
                total.value = (total.value or 0) + metric.value
            elif metric.text:
                for piece in metric.text.split(", "):
                    if piece not in total.texts:
                        total.texts.append(piece)
            for name, qty in metric.parts:
                total.parts[name] = total.parts.get(name, 0) + qty
    return out


def plan_actuals(plan_ids) -> dict[int, dict[str, Total]]:
    """{plan id: totals} over the counted attached recaps."""
    per_plan: dict[int, list[list[pm.RecapMetric]]] = {}
    for link in links_for_plans(plan_ids):
        recap, _kind = link_recap(link)
        if recap is None or not counts_toward_results(recap):
            continue
        per_plan.setdefault(link.plan_id, []).append(selected_metrics(link))
    return {plan_id: sum_metrics(lists) for plan_id, lists in per_plan.items()}


def result_counts(plan_ids) -> dict[int, tuple[int, int]]:
    """{plan id: (counted recaps, attached recaps still in review)}."""
    out: dict[int, list[int]] = {}
    rows = PlanRecapLink.objects.filter(plan_id__in=list(plan_ids)).values_list(
        "plan_id",
        "custom_recap_id",
        "recap__approved",
        "recap__archived_at",
        "custom_recap__approved",
        "custom_recap__archived_at",
    )
    for plan_id, custom_id, r_ok, r_arch, c_ok, c_arch in rows:
        approved, archived = (c_ok, c_arch) if custom_id else (r_ok, r_arch)
        bucket = out.setdefault(plan_id, [0, 0])
        if archived is not None:
            continue
        bucket[0 if approved else 1] += 1
    return {plan_id: (row[0], row[1]) for plan_id, row in out.items()}
