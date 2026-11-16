"""GraphQL for the recap field → standard metric mapping. Ignite staff only.

Read, suggest and save all gate on the DB-backed staff check
(:func:`events.staffing_board._accessible_tenants`). A brand user reads null
and cannot write: the mapping decides numbers a client sees, so it is set by
staff alone.

``suggestRecapMetricMapping`` stores nothing — it returns a proposal for the
screen to show. Only ``saveRecapMetricMapping`` writes, and it writes what
the staff member chose.
"""

from __future__ import annotations

from datetime import datetime

import strawberry
from asgiref.sync import sync_to_async

from recaps import metric_mapping
from utils.graphql.mixins import resolve_id_to_int
from utils.graphql.permissions import StrictIsAuthenticated


@strawberry.type(name="RecapStandardMetric")
class RecapStandardMetricType:
    key: str
    label: str


@strawberry.type(name="RecapFieldMetric")
class RecapFieldMetricType:
    """One template field and what Spark counts it as.

    ``currentMetric`` is what the wording match gives today (null: nothing).
    ``confirmedMetric`` is staff's decision (null: not confirmed, the wording
    match applies; ``"none"``: confirmed as not a standard number).
    ``canCount`` is false for a field a count cannot be read from.
    """

    field_id: strawberry.ID
    name: str
    field_type: str
    section: str
    current_metric: str | None
    confirmed_metric: str | None
    pack_size: int | None
    confirmed_by: str
    confirmed_at: datetime | None
    can_count: bool


@strawberry.type(name="RecapTemplateMetricMapping")
class RecapTemplateMetricMappingType:
    template_id: strawberry.ID
    fields: list[RecapFieldMetricType]
    # Standard numbers with no field on this template: a client would see "—".
    missing_metrics: list[RecapStandardMetricType]
    metric_options: list[RecapStandardMetricType]


@strawberry.type(name="RecapMetricSuggestion")
class RecapMetricSuggestionType:
    field_id: strawberry.ID
    metric: str
    pack_size: int | None
    reason: str


@strawberry.type(name="RecapMetricSuggestionResult")
class RecapMetricSuggestionResultType:
    ok: bool
    reason: str
    suggestions: list[RecapMetricSuggestionType]


@strawberry.type(name="SaveRecapMetricMappingResult")
class SaveRecapMetricMappingResultType:
    ok: bool
    message: str
    mapping: RecapTemplateMetricMappingType | None


@strawberry.input
class RecapFieldMetricInput:
    field_id: strawberry.ID
    # Null sets the field back to "not confirmed".
    metric: str | None = None
    pack_size: int | None = None


def _mapping_type(payload: dict) -> RecapTemplateMetricMappingType:
    return RecapTemplateMetricMappingType(
        template_id=strawberry.ID(str(payload["template_id"])),
        fields=[
            RecapFieldMetricType(
                field_id=strawberry.ID(str(f["field_id"])),
                name=f["name"] or "",
                field_type=f["field_type"],
                section=f["section"],
                current_metric=f["current_metric"],
                confirmed_metric=f["confirmed_metric"],
                pack_size=f["pack_size"],
                confirmed_by=f["confirmed_by"],
                confirmed_at=f["confirmed_at"],
                can_count=f["can_count"],
            )
            for f in payload["fields"]
        ],
        missing_metrics=[
            RecapStandardMetricType(key=m["key"], label=m["label"])
            for m in payload["missing"]
        ],
        metric_options=[
            RecapStandardMetricType(key=key, label=label)
            for key, label in metric_mapping.METRIC_OPTIONS
        ],
    )


async def _is_staff(info: strawberry.Info) -> bool:
    from events.staffing_board import _accessible_tenants

    try:
        is_admin, _allowed = await _accessible_tenants(info.context.request.user)
    except Exception:
        return False
    return bool(is_admin)


def _template_pk(template_id) -> int | None:
    try:
        return int(resolve_id_to_int(str(template_id).strip()))
    except Exception:
        return None


def _field_pk(field_id) -> int | None:
    try:
        return int(resolve_id_to_int(str(field_id).strip()))
    except Exception:
        return None


@strawberry.type
class RecapMetricMappingQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def recap_template_metric_mapping(
        self, info: strawberry.Info, template_id: strawberry.ID
    ) -> RecapTemplateMetricMappingType | None:
        """What each field of a recap template counts as. Staff only: anyone
        else, and an unknown template, reads null."""
        if not await _is_staff(info):
            return None
        pk = _template_pk(template_id)
        if pk is None:
            return None
        payload = await sync_to_async(
            metric_mapping.template_mapping, thread_sensitive=True
        )(pk)
        return _mapping_type(payload) if payload else None


@strawberry.type
class RecapMetricMappingMutations:
    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def suggest_recap_metric_mapping(
        self, info: strawberry.Info, template_id: strawberry.ID
    ) -> RecapMetricSuggestionResultType:
        """Propose a metric per field from the field labels, types and choice
        options. Stores nothing and changes no number."""
        if not await _is_staff(info):
            return RecapMetricSuggestionResultType(
                ok=False, reason="Not allowed.", suggestions=[]
            )
        pk = _template_pk(template_id)
        if pk is None:
            return RecapMetricSuggestionResultType(
                ok=False, reason="Template not found.", suggestions=[]
            )
        try:
            result = await sync_to_async(
                metric_mapping.suggest_mapping, thread_sensitive=True
            )(pk)
        except Exception:
            result = {
                "ok": False,
                "reason": "Suggestions are not available right now. Choose each field by hand.",
                "suggestions": [],
            }
        return RecapMetricSuggestionResultType(
            ok=bool(result["ok"]),
            reason=result["reason"],
            suggestions=[
                RecapMetricSuggestionType(
                    field_id=strawberry.ID(str(s["field_id"])),
                    metric=s["metric"],
                    pack_size=s["pack_size"],
                    reason=s["reason"],
                )
                for s in result["suggestions"]
            ],
        )

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def save_recap_metric_mapping(
        self,
        info: strawberry.Info,
        template_id: strawberry.ID,
        fields: list[RecapFieldMetricInput],
    ) -> SaveRecapMetricMappingResultType:
        """Store the metrics a staff member confirmed for a template's fields."""
        if not await _is_staff(info):
            return SaveRecapMetricMappingResultType(
                ok=False, message="Not allowed.", mapping=None
            )
        pk = _template_pk(template_id)
        if pk is None:
            return SaveRecapMetricMappingResultType(
                ok=False, message="Template not found.", mapping=None
            )
        choices = [
            {
                "field_id": _field_pk(item.field_id),
                "metric": (item.metric or None),
                "pack_size": item.pack_size,
            }
            for item in fields
        ]
        user = info.context.request.user

        def _save():
            outcome = metric_mapping.save_mapping(pk, choices, user)
            return outcome, metric_mapping.template_mapping(pk)

        outcome, payload = await sync_to_async(_save, thread_sensitive=True)()
        return SaveRecapMetricMappingResultType(
            ok=bool(outcome["ok"]),
            message=outcome["message"],
            mapping=_mapping_type(payload) if payload else None,
        )
