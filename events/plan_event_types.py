"""The program (EventType) a field-marketing plan's booked event runs.

Without this, a plan request materializes on the brand's default event type —
Retail Sampling for Torch — so an Event Activation plan's event opens the
retail recap form, and walk-up check-ins (keyed on event type) never join it.
"""

from __future__ import annotations

from events.models import EventType, FieldMarketingEvent
from recaps.recap_types import (
    RECAP_TYPE_EVENT,
    RECAP_TYPE_GUERILLA,
    RECAP_TYPE_SEEDING,
    recap_type_for_name,
)

_FM = FieldMarketingEvent
PLAN_RECAP_TYPES: dict[str, str] = {
    _FM.ACTIVITY_EVENT_ACTIVATION: RECAP_TYPE_EVENT,
    _FM.ACTIVITY_SPONSORSHIP: RECAP_TYPE_EVENT,
    _FM.ACTIVITY_GUERILLA: RECAP_TYPE_GUERILLA,
    _FM.ACTIVITY_FULL_CAN: RECAP_TYPE_GUERILLA,
    _FM.ACTIVITY_POUR: RECAP_TYPE_GUERILLA,
    _FM.ACTIVITY_PRODUCT_SEEDING: RECAP_TYPE_SEEDING,
}
# Torch has both "Event" and "Event Activation"; plans book the latter.
_PREFERRED_NAMES = {RECAP_TYPE_EVENT: "event activation"}


def plan_recap_type(plan) -> str | None:
    return PLAN_RECAP_TYPES.get(getattr(plan, "activity", None))


def event_type_fits_plan(event_type, plan) -> bool:
    target = plan_recap_type(plan)
    return target is not None and recap_type_for_name(getattr(event_type, "name", None)) == target


def event_type_for_plan(plan) -> EventType | None:
    target = plan_recap_type(plan)
    if target is None:
        return None
    candidates = [
        et
        for et in EventType.objects.filter(tenant_id=plan.tenant_id).order_by("id")
        if recap_type_for_name(et.name) == target
    ]
    preferred = _PREFERRED_NAMES.get(target)
    return next(
        (et for et in candidates if (et.name or "").strip().lower() == preferred),
        candidates[0] if candidates else None,
    )


def event_type_for_request(request) -> EventType | None:
    """The plan tactic's event type when ``request`` was booked from a plan."""
    plan = (
        _FM.objects.filter(request_id=request.id, deleted_at__isnull=True).order_by("id").first()
        if getattr(request, "id", None)
        else None
    )
    return event_type_for_plan(plan) if plan is not None else None
