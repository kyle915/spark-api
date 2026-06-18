"""deleteRequest soft-deletes a Request (sets deleted_at). Its events must then
disappear from EVENT-based views (Upcoming / Calendar / Today), not only from
the request-based Master Tracker.

Regression for Kyle's report: deleting a Liquid Death event from the Master
Tracker removed it there + 404'd its associated request, but it lingered in the
Upcoming list — because that list queries Event directly and nothing excluded
events whose parent request was soft-deleted.
"""

import pytest
from django.utils import timezone

from events import models as em
from events.queries import EventQueriesService
from events.tests.base import EventsGraphQLTestCase


@pytest.mark.django_db(transaction=True)
class TestEventsExcludeDeletedRequest(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Excl Tenant")
        self.sys = self.get_system_user()
        rt = em.RequestType.objects.create(
            name="Retail Sampling", tenant=self.tenant, created_by=self.sys
        )
        # Live request + event — must stay visible.
        self.live_req = em.Request.objects.create(
            name="Live", address="1 A St", request_type=rt,
            tenant=self.tenant, created_by=self.sys,
        )
        self.live_ev = self.create_event(
            name="Live Day", tenant=self.tenant, request=self.live_req
        )
        # Soft-deleted request + its event — must be hidden everywhere.
        self.del_req = em.Request.objects.create(
            name="Deleted", address="2 B St", request_type=rt,
            tenant=self.tenant, created_by=self.sys,
            deleted_at=timezone.now(),
        )
        self.del_ev = self.create_event(
            name="Deleted Day", tenant=self.tenant, request=self.del_req
        )

    def test_event_queryset_excludes_soft_deleted_request_events(self):
        ids = set(
            EventQueriesService()
            .get_queryset()
            .filter(tenant=self.tenant)
            .values_list("id", flat=True)
        )
        assert self.live_ev.id in ids, "live event must remain visible"
        assert self.del_ev.id not in ids, (
            "event under a soft-deleted request must be hidden from event views"
        )
