import strawberry
from asgiref.sync import sync_to_async

from utils.graphql.permissions import StrictIsAuthenticated

from . import types


def _fmt(t) -> str:
    """TimeField -> 'HH:MM'."""
    return t.strftime("%H:%M") if t else ""


@strawberry.type
class AvailabilityQueryMobile:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def my_availability(
        self,
        info: strawberry.Info,
    ) -> list[types.AvailabilitySlot]:
        """The calling BA's recurring weekly availability, ordered by
        weekday then start time. Empty list for non-ambassador users or
        when nothing's been set."""
        user = info.context.request.user
        if not getattr(user, "is_authenticated", False):
            return []

        from ambassadors.models import Ambassador
        from .models import AmbassadorAvailability

        def _fetch() -> list:
            try:
                amb = Ambassador.objects.get(user=user)
            except Ambassador.DoesNotExist:
                return []
            rows = AmbassadorAvailability.objects.filter(
                ambassador=amb, is_recurring=True
            ).order_by("weekday", "start_time")
            return [
                types.AvailabilitySlot(
                    uuid=str(r.uuid),
                    weekday=r.weekday if r.weekday is not None else 0,
                    start_time=_fmt(r.start_time),
                    end_time=_fmt(r.end_time),
                    note=r.note,
                )
                for r in rows
            ]

        return await sync_to_async(_fetch, thread_sensitive=True)()
