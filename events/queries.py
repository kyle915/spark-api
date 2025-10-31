import strawberry
from strawberry_django.permissions import IsAuthenticated
from asgiref.sync import sync_to_async
from typing import List

from .types import Event
from .models import Event as EventModel


@strawberry.type
class EventQueries:
    @strawberry.field(extensions=[IsAuthenticated()])
    async def events(
        self,
        info: strawberry.Info,
        limit: int = 10,
        offset: int = 0,
        tenant_id: strawberry.ID | None = None,
        q: str | None = None,
    ) -> List[Event]:
        """Get all events."""
        @sync_to_async
        def get_events():
            return list(EventModel.objects.all())

        return await get_events()

    @strawberry.field(extensions=[IsAuthenticated()])
    async def event(self, info: strawberry.Info, id: strawberry.ID) -> Event | None:
        """Get a single event."""
        try:
            event = await sync_to_async(EventModel.objects.get)(id=id)
            return event
        except EventModel.DoesNotExist:
            return None
