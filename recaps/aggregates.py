"""Keep ``CustomRecap.exclude_from_aggregates`` recaps out of rollups.

3rd-party / agency filings (e.g. Torch ``TH-AGENCY``) stay in Recaps —
listed, approvable, shareable, with their own numbers on detail — but must
never add to a cross-recap total: Insights, dashboards, list strips,
campaign / summary exports. Every aggregate queryset passes through
:func:`exclude_non_aggregate_recaps` (``recaps.tenant_overview.
_filter_event_window`` does it for the whole Insights family).
"""

from __future__ import annotations

from django.db.models import Exists, OuterRef, Q


def exclude_non_aggregate_recaps(queryset, prefix: str = ""):
    """Drop rows that belong to recaps kept out of aggregates.

    ``prefix`` is the ORM path from the row to its Event (the
    ``_filter_event_window`` convention):

    * ``CustomRecap`` (``"event__"``) — drop flagged recaps.
    * ``custom_recap__event__`` children (field values, product samples,
      files) — drop rows of flagged recaps.
    * ``Event`` (``""``) — drop events whose only recaps are flagged, so an
      agency-only walk-in doesn't count as an activation; an event shared
      with a counted recap stays.
    * Anything else (legacy ``Recap`` and its children) — unchanged; the
      flag only exists on custom recaps.
    """
    from events.models import Event
    from recaps.models import CustomRecap, Recap

    model = queryset.model
    if model is CustomRecap:
        return queryset.filter(exclude_from_aggregates=False)
    if prefix.startswith("custom_recap__"):
        return queryset.filter(custom_recap__exclude_from_aggregates=False)
    if model is Event and prefix == "":
        flagged = CustomRecap.objects.filter(
            event_id=OuterRef("pk"), exclude_from_aggregates=True
        )
        counted = CustomRecap.objects.filter(
            event_id=OuterRef("pk"), exclude_from_aggregates=False
        )
        legacy = Recap.objects.filter(event_id=OuterRef("pk"))
        return queryset.filter(~Exists(flagged) | Exists(counted) | Exists(legacy))
    return queryset


def aggregate_custom_recap_q(prefix: str = "") -> Q:
    """``Q`` keeping custom recaps that count toward aggregates.

    ``prefix`` is the ORM path to the CustomRecap (``""`` on CustomRecap,
    ``"custom_recap__"`` on its children).
    """
    return Q(**{f"{prefix}exclude_from_aggregates": False})



def client_counted_q(prefix: str = "") -> Q:
    """``Q`` keeping recaps that count toward a brand user's totals: approved.

    ``prefix`` is the ORM path to the Recap / CustomRecap (``""`` on the
    recap itself, ``"recap__"`` / ``"custom_recap__"`` on children,
    ``"recaps__"`` / ``"custom_recap__"`` from an Event). Recaps on a
    soft-deleted Request are dropped separately, at the Event level.

    The bar the client dashboard's Program-KPIs sections already use
    (``recaps.tenant_overview._approved_only``). An approved recap that was
    later archived still counts here, as it does there; the per-request
    campaign report leaves archived recaps out (``report_service``).
    """
    return Q(**{f"{prefix}approved": True})
