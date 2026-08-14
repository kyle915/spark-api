"""Start a draft recap when a BA clocks out, if none exists yet.

Best-effort: clock-out must never fail because of this hook. Prefers a
CustomRecap when the event/tenant has a template (same fallback as
events.types.Event.custom_recap_template); otherwise a legacy Recap.
"""

from __future__ import annotations

import logging

from django.db import transaction

logger = logging.getLogger(__name__)


def _resolve_template(event):
    from recaps.models import CustomRecap, CustomRecapTemplate

    if getattr(event, "custom_recap_template_id", None):
        return CustomRecapTemplate.objects.filter(
            id=event.custom_recap_template_id
        ).first()
    if not getattr(event, "tenant_id", None):
        return None
    existing_tpl_id = (
        CustomRecap.objects.filter(event_id=event.id)
        .order_by("-id")
        .values_list("custom_recap_template_id", flat=True)
        .first()
    )
    if existing_tpl_id:
        tpl = CustomRecapTemplate.objects.filter(id=existing_tpl_id).first()
        if tpl:
            return tpl
    tenant_qs = CustomRecapTemplate.objects.filter(tenant_id=event.tenant_id)
    if getattr(event, "event_type_id", None):
        typed = (
            tenant_qs.filter(event_type_id=event.event_type_id)
            .order_by("id")
            .first()
        )
        if typed:
            return typed
    return tenant_qs.order_by("id").first()


def start_recap_on_clock_out(attendance_id: int):
    """Create a draft recap for this clock-out if the shift has none."""
    from ambassadors.models import Attendance
    from recaps.models import CustomRecap, Recap

    att = (
        Attendance.objects.select_related(
            "event",
            "event__retailer",
            "event__location",
            "event__state",
            "event__timezone",
            "ambassador",
            "ambassador__user",
        )
        .filter(id=attendance_id)
        .first()
    )
    if att is None:
        return None
    event = att.event
    ambassador = att.ambassador
    if event is None or ambassador is None:
        return None
    actor = getattr(ambassador, "user", None)
    if actor is None:
        return None

    if CustomRecap.objects.filter(event=event, ambassador=ambassador).exists():
        return None
    if Recap.objects.filter(event=event, ambassador=ambassador).exists():
        return None

    name = (getattr(event, "name", None) or "Recap").strip() or "Recap"
    retailer = getattr(event, "retailer", None)
    location = getattr(event, "location", None) or getattr(
        retailer, "location", None
    )
    state = getattr(event, "state", None) or getattr(location, "state", None)
    timezone = getattr(event, "timezone", None)
    template = _resolve_template(event)

    with transaction.atomic():
        if template and getattr(event, "tenant_id", None):
            return CustomRecap.objects.create(
                name=name,
                event=event,
                ambassador=ambassador,
                tenant_id=event.tenant_id,
                custom_recap_template=template,
                retailer=retailer,
                location=location,
                state=state,
                timezone=timezone,
                created_by=actor,
                updated_by=actor,
                approved=False,
            )
        return Recap.objects.create(
            name=name,
            event=event,
            ambassador=ambassador,
            retailer=retailer,
            location=location,
            state=state,
            timezone=timezone,
            created_by=actor,
            updated_by=actor,
            approved=False,
        )
