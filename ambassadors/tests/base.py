"""
Base test class for ambassadors app tests.

This module provides helper methods for creating ambassador-related models
for testing.
"""
from events.tests.base import EventsGraphQLTestCase
from ambassadors.models import Ambassador


class AmbassadorsGraphQLTestCase(EventsGraphQLTestCase):
    """
    Base test class for ambassador-related queries and mutations.

    Extends EventsGraphQLTestCase to reuse all event-related helper methods
    and adds methods for creating ambassador-related models.
    """

    def create_ambassador(
        self,
        user,
        address: str | None = None,
        coordinates: list[float] | None = None,
        is_active: bool = True,
        **kwargs,
    ) -> Ambassador:
        """Create an Ambassador instance."""
        system_user = self.get_system_user()
        return Ambassador.objects.create(
            user=user,
            address=address,
            coordinates=coordinates or [],
            is_active=is_active,
            created_by=kwargs.get("created_by", system_user),
            updated_by=kwargs.get("updated_by", system_user),
            **{k: v for k, v in kwargs.items() if k not in ["created_by", "updated_by"]},
        )

