"""
Goals service.

Centralizes logic for user goals: persistence, current-value computation from
events and ConsumerEngagements, and progress calculation for the dashboard.
"""
from dataclasses import dataclass
from datetime import date
from typing import Any

from django.db.models import Q, Sum

from events.models import Event
from recaps.models import ConsumerEngagements
from tenants.models import Goal, Tenant, TenantedUser
from utils.graphql.validation import clamp_percentage


# ---------------------------------------------------------------------------
# Goal progress (target + current -> percentage)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GoalProgressSpec:
    """Spec for one goal type: display name, goal model attribute, current-values key."""

    display_name: str
    goal_attr: str
    current_key: str


GOAL_PROGRESS_SPECS = (
    GoalProgressSpec("Events Target", "event_target_goal", "current_events_count"),
    GoalProgressSpec("Consumer Sampling", "consumer_sampling_goal", "current_consumer_sampling"),
    GoalProgressSpec("Brand Awareness", "brand_awareness_goal", "current_brand_awareness"),
    GoalProgressSpec("Purchase Intent", "purchase_intent_goal", "current_purchase_intent"),
    GoalProgressSpec("First-Time Buyers", "first_time_buyers_goal", "current_first_time_buyers"),
)


def build_goals_progress(goal: Goal, current_values: dict[str, int | float]) -> list[dict[str, Any]]:
    """
    Build progress items for each goal type that has a target set.

    Returns a list of dicts with keys: name, target, current, percentage_complete.
    Female participation is omitted (no current source).
    """
    result = []
    for spec in GOAL_PROGRESS_SPECS:
        target = getattr(goal, spec.goal_attr, None)
        if target is None or target <= 0:
            continue
        current = current_values.get(spec.current_key) or 0
        percentage = min(100.0, (float(current) / float(target)) * 100.0)
        result.append({
            "name": spec.display_name,
            "target": float(target),
            "current": float(current),
            "percentage_complete": round(percentage, 1),
        })
    return result


# ---------------------------------------------------------------------------
# Persistence (single source of truth for goal target field names)
# ---------------------------------------------------------------------------

GOAL_FIELD_DEFAULTS: dict[str, None] = {
    "event_target_goal": None,
    "consumer_sampling_goal": None,
    "brand_awareness_goal": None,
    "purchase_intent_goal": None,
    "female_participation_goal": None,
    "first_time_buyers_goal": None,
}
GOAL_TARGET_FIELDS = tuple(GOAL_FIELD_DEFAULTS.keys())


def get_goals(tenant_id: int, user_id: int, year: int) -> Goal | None:
    """Return the Goal for the given tenant, user, and year, or None."""
    return Goal.objects.select_related("user").filter(
        tenant_id=tenant_id,
        user_id=user_id,
        year=year,
    ).first()


def get_or_create_goal(tenant_id: int, user_id: int, year: int) -> tuple[Goal, bool]:
    """Get or create a Goal for the given tenant, user, and year. Returns (goal, created)."""
    return Goal.objects.get_or_create(
        tenant_id=tenant_id,
        user_id=user_id,
        year=year,
        defaults=dict(GOAL_FIELD_DEFAULTS),
    )


def extract_goal_updates(obj: Any) -> dict[str, int | float]:
    """Build a dict of non-None goal target values from an object with matching attribute names."""
    return {
        key: getattr(obj, key)
        for key in GOAL_TARGET_FIELDS
        if getattr(obj, key, None) is not None
    }


def upsert_goals(
    tenant_id: int,
    user_id: int,
    year: int,
    goal_updates: dict[str, int | float] | None = None,
) -> Goal:
    """Create or update a Goal; only provided target fields (in goal_updates) are updated."""
    goal, _ = get_or_create_goal(tenant_id, user_id, year)
    raw = goal_updates or {}
    updates = {
        k: v for k, v in raw.items()
        if k in GOAL_FIELD_DEFAULTS and v is not None
    }
    update_fields = ["updated_at"]
    for field_name, value in updates.items():
        setattr(goal, field_name, value)
        update_fields.append(field_name)
    goal.save(update_fields=update_fields)
    # Ensure related user is eagerly loaded so async GraphQL resolvers
    # don't trigger a synchronous lazy-load for goal.user.
    return Goal.objects.select_related("user").get(pk=goal.pk)


# ---------------------------------------------------------------------------
# Current values (from events + ConsumerEngagements)
# ---------------------------------------------------------------------------

def _event_date_range_q(start_date: date, end_date: date) -> Q:
    """Q object to filter events by date range (date, start_time, or request date)."""
    return (
        Q(date__date__gte=start_date, date__date__lte=end_date)
        | Q(start_time__date__gte=start_date, start_time__date__lte=end_date)
        | Q(request__date__date__gte=start_date, request__date__date__lte=end_date)
    )


def get_current_values_for_user(
    tenant_id: int,
    user_id: int,
    start_date: date,
    end_date: date,
) -> dict[str, int | float]:
    """
    Compute current (actual) values for a user in the given date range.

    Events: tenant + rmm_asigned=user + date in range.
    Consumer metrics: aggregated from ConsumerEngagements for recaps of those events.
    """
    date_q = _event_date_range_q(start_date, end_date)
    events_qs = Event.objects.filter(
        tenant_id=tenant_id,
        rmm_asigned_id=user_id,
    ).filter(date_q)
    events_with_recaps = events_qs.filter(recaps__isnull=False).distinct()

    current_events_count = events_qs.count()

    agg = ConsumerEngagements.objects.filter(
        recap__event__in=events_with_recaps
    ).aggregate(
        total_consumers=Sum("total_consumer", default=0),
        brand_aware=Sum("brand_aware_consumers", default=0),
        willing=Sum("willing_to_purchase_consumers", default=0),
        first_time=Sum("first_time_consumers", default=0),
    )
    total_consumers = agg["total_consumers"] or 0
    brand_aware = agg["brand_aware"] or 0
    willing = agg["willing"] or 0
    first_time = agg["first_time"] or 0

    brand_awareness_pct = clamp_percentage(
        (brand_aware / total_consumers * 100) if total_consumers else 0.0
    )
    purchase_intent_pct = clamp_percentage(
        (willing / total_consumers * 100) if total_consumers else 0.0
    )

    return {
        "current_events_count": current_events_count,
        "current_consumer_sampling": total_consumers,
        "current_brand_awareness": brand_awareness_pct,
        "current_purchase_intent": purchase_intent_pct,
        "current_first_time_buyers": first_time,
        "current_female_participation": None,
    }


# ---------------------------------------------------------------------------
# Bulk: ensure goals for all tenant users
# ---------------------------------------------------------------------------

BULK_GOAL_CREATE_BATCH_SIZE = 1000


def ensure_goals_for_tenant_users(tenant_id: int, year: int) -> int:
    """
    Ensure a Goal row exists for every active user in the tenant.
    Uses bulk_create for missing goals to avoid O(n) round-trips for large tenants.
    Returns the number of new goals created.
    """
    if not Tenant.objects.filter(id=tenant_id).exists():
        return 0

    active_user_ids = set(
        TenantedUser.objects.filter(
            tenant_id=tenant_id,
            is_active=True,
        ).values_list("user_id", flat=True).distinct()
    )
    if not active_user_ids:
        return 0

    existing_user_ids = set(
        Goal.objects.filter(
            tenant_id=tenant_id,
            year=year,
            user_id__in=active_user_ids,
        ).values_list("user_id", flat=True)
    )
    missing_user_ids = active_user_ids - existing_user_ids
    if not missing_user_ids:
        return 0

    total_created = 0
    missing_list = list(missing_user_ids)
    for i in range(0, len(missing_list), BULK_GOAL_CREATE_BATCH_SIZE):
        batch = missing_list[i : i + BULK_GOAL_CREATE_BATCH_SIZE]
        goals = [
            Goal(
                tenant_id=tenant_id,
                user_id=uid,
                year=year,
            )
            for uid in batch
        ]
        Goal.objects.bulk_create(goals)
        total_created += len(goals)
    return total_created
