"""The mobile RecapSubmitScreen sends the event's UUID as eventId; the web
sends relay global ids. _get_event_by_flexible_id must take both (plus plain
ints) — a uuid rejected by resolve_id_to_int surfaced as "Event not found."
on every mobile recap submit for template-less tenants (Feel Free)."""
import base64

import pytest
from graphql import GraphQLError

from events.models import Event
from events.tests.base import EventsGraphQLTestCase
from recaps.mutations import _get_event_by_flexible_id


@pytest.mark.django_db(transaction=True)
class TestFlexibleEventId(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Feel Free")
        etype = self.create_event_type(name="Field Sampling", tenant=self.tenant)
        status = self.create_event_status(name="Approved", tenant=self.tenant)
        self.event = Event.objects.create(
            name="Miami — Wynwood · 7/2", tenant=self.tenant,
            event_type=etype, status=status, created_by=self.system_user,
        )

    def test_resolves_by_uuid_string(self):
        assert _get_event_by_flexible_id(str(self.event.uuid)) == self.event

    def test_resolves_by_plain_int_and_digit_string(self):
        assert _get_event_by_flexible_id(self.event.id) == self.event
        assert _get_event_by_flexible_id(str(self.event.id)) == self.event

    def test_resolves_by_relay_global_id(self):
        gid = base64.b64encode(f"Event:{self.event.id}".encode()).decode()
        assert _get_event_by_flexible_id(gid) == self.event

    def test_unknown_raises_event_not_found(self):
        with pytest.raises(GraphQLError, match="Event not found."):
            _get_event_by_flexible_id("00000000-0000-0000-0000-000000000000")
        with pytest.raises(GraphQLError, match="Event not found."):
            _get_event_by_flexible_id("total-garbage")

    def test_respects_scoped_queryset(self):
        other = self.create_tenant(name="Other Co")
        with pytest.raises(GraphQLError, match="Event not found."):
            _get_event_by_flexible_id(
                str(self.event.uuid), Event.objects.filter(tenant=other)
            )
