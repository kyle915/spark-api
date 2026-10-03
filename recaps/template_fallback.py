"""Last-resort recap template for an event whose event type matches none.

Shared by ``events.types.Event.custom_recap_template`` and the web check-in's
``resolve_template_for_event`` so app, desktop, and walk-up agree.
"""

from __future__ import annotations

import re

_ACTIVATION_RE = re.compile(r"\bactivation\b", re.I)


def is_activation_template(template) -> bool:
    """True when a template's event type or name is an Event Activation."""
    etype = getattr(template, "event_type", None)
    return bool(
        _ACTIVATION_RE.search(getattr(etype, "name", "") or "")
        or _ACTIVATION_RE.search(getattr(template, "name", "") or "")
    )


def fallback_template(tenant_qs):
    """The tenant's sole template; else its sole non-Event-Activation one.

    An event with no (or an unmatched) event type is a store/scheduled event,
    never an activation — so a brand that adds an Event Activation form next
    to its one store form (Torch) keeps serving the store form there instead
    of dropping to the legacy photo-only recap. 2+ candidates → ``None``.
    """
    templates = list(tenant_qs.select_related("event_type").order_by("id")[:50])
    if len(templates) == 1:
        return templates[0]
    candidates = [t for t in templates if not is_activation_template(t)]
    if len(candidates) == 1:
        return candidates[0]
    return None
