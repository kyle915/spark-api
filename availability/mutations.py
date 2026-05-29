import datetime

import strawberry
from strawberry import relay
from asgiref.sync import sync_to_async
from django.db import transaction

from utils.graphql.permissions import StrictIsAuthenticated

from . import inputs, types


def _parse_hhmm(s: str):
    """'HH:MM' or 'HH:MM:SS' -> datetime.time. Raises ValueError on junk."""
    s = (s or "").strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.datetime.strptime(s, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"bad time {s!r}")


def _fmt(t) -> str:
    return t.strftime("%H:%M") if t else ""


@strawberry.type
class AvailabilityMutationsMobile:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def set_availability(
        self,
        info: strawberry.Info,
        input: inputs.SetAvailabilityInput,
    ) -> types.SetAvailabilityResponse:
        """Replace the calling BA's recurring-availability set with the
        provided slots (delete-then-insert, atomic). Empty slots clears
        everything. Validates weekday 0..6 and start < end per slot."""
        actor = info.context.request.user

        from ambassadors.models import Ambassador
        from .models import AmbassadorAvailability

        # Validate + normalize OUTSIDE the txn so a bad slot returns a
        # clean error without touching the DB.
        normalized: list[dict] = []
        for s in input.slots:
            if s.weekday is None or not (0 <= int(s.weekday) <= 6):
                return types.SetAvailabilityResponse(
                    success=False,
                    message="Each slot needs a weekday between 0 (Mon) and 6 (Sun).",
                    client_mutation_id=input.client_mutation_id,
                )
            try:
                start = _parse_hhmm(s.start_time)
                end = _parse_hhmm(s.end_time)
            except ValueError:
                return types.SetAvailabilityResponse(
                    success=False,
                    message="Times must be in HH:MM (24-hour) format.",
                    client_mutation_id=input.client_mutation_id,
                )
            if start >= end:
                return types.SetAvailabilityResponse(
                    success=False,
                    message="Each slot's start time must be before its end time.",
                    client_mutation_id=input.client_mutation_id,
                )
            normalized.append(
                {
                    "weekday": int(s.weekday),
                    "start_time": start,
                    "end_time": end,
                    "note": (s.note or None),
                }
            )

        def _save():
            try:
                amb = Ambassador.objects.get(user=actor)
            except Ambassador.DoesNotExist:
                return None, "Only ambassadors can set availability."
            with transaction.atomic():
                AmbassadorAvailability.objects.filter(
                    ambassador=amb, is_recurring=True
                ).delete()
                AmbassadorAvailability.objects.bulk_create(
                    [
                        AmbassadorAvailability(
                            ambassador=amb,
                            is_recurring=True,
                            date=None,
                            weekday=n["weekday"],
                            start_time=n["start_time"],
                            end_time=n["end_time"],
                            note=n["note"],
                            created_by=actor,
                            updated_by=actor,
                        )
                        for n in normalized
                    ]
                )
                rows = list(
                    AmbassadorAvailability.objects.filter(
                        ambassador=amb, is_recurring=True
                    ).order_by("weekday", "start_time")
                )
            return rows, "Availability saved."

        rows, msg = await sync_to_async(_save, thread_sensitive=True)()
        return types.SetAvailabilityResponse(
            success=rows is not None,
            message=msg,
            client_mutation_id=input.client_mutation_id,
            slots=(
                [
                    types.AvailabilitySlot(
                        uuid=str(r.uuid),
                        weekday=r.weekday if r.weekday is not None else 0,
                        start_time=_fmt(r.start_time),
                        end_time=_fmt(r.end_time),
                        note=r.note,
                    )
                    for r in rows
                ]
                if rows is not None
                else None
            ),
        )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def clear_availability(
        self,
        info: strawberry.Info,
        client_mutation_id: strawberry.ID | None = None,
    ) -> types.ClearAvailabilityResponse:
        """Delete all recurring availability for the calling BA."""
        actor = info.context.request.user

        from ambassadors.models import Ambassador
        from .models import AmbassadorAvailability

        def _clear():
            try:
                amb = Ambassador.objects.get(user=actor)
            except Ambassador.DoesNotExist:
                return None
            deleted, _ = AmbassadorAvailability.objects.filter(
                ambassador=amb, is_recurring=True
            ).delete()
            return deleted

        deleted = await sync_to_async(_clear, thread_sensitive=True)()
        if deleted is None:
            return types.ClearAvailabilityResponse(
                success=False,
                message="Only ambassadors can clear availability.",
                client_mutation_id=client_mutation_id,
                cleared_count=0,
            )
        return types.ClearAvailabilityResponse(
            success=True,
            message="Availability cleared.",
            client_mutation_id=client_mutation_id,
            cleared_count=int(deleted),
        )
