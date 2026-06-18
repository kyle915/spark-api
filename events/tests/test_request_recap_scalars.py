"""
Master Tracker perf: the Request type exposes recapsFiledCount /
recapEventUuid / eventsCount as flat scalars computed from the existing
event_set + event_set__recaps + event_set__custom_recap prefetch caches, so
the tracker LIST query can drop the heavy per-row events{recaps,customRecaps}
arrays. This verifies the values AND that reading them adds ZERO extra queries
when the request was prefetched (the no-N+1 guarantee the change relies on —
the shared prefetch is untouched, so /request/view still gets full recaps).
"""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from events import models as em
from events.types import Request as RequestGQL
from recaps import models as rm
from events.tests.base import EventsGraphQLTestCase


@pytest.mark.django_db
class TestRequestRecapScalars(EventsGraphQLTestCase):
    def _prefetched(self, tenant):
        # Mirror the recap-relevant prefetch the list resolver applies.
        return list(
            em.Request.objects.filter(tenant=tenant).prefetch_related(
                "event_set", "event_set__recaps", "event_set__custom_recap"
            )
        )

    def test_scalars_and_no_nplus1(self):
        sys = self.get_system_user()
        t = self.create_tenant(name="Perf Tenant")
        rt = em.RequestType.objects.create(
            name="Retail Sampling", tenant=t, created_by=sys
        )
        req = em.Request.objects.create(
            name="WF Burbank", address="1 A St", request_type=rt,
            tenant=t, created_by=sys,
        )
        ev_a = self.create_event(name="Day 1", tenant=t, request=req)
        self.create_event(name="Day 2", tenant=t, request=req)  # recap-less

        # event A: 2 legacy recaps + 1 custom recap; event B: none.
        rm.Recap.objects.create(name="r1", event=ev_a, created_by=sys, updated_by=sys)
        rm.Recap.objects.create(name="r2", event=ev_a, created_by=sys, updated_by=sys)
        et = self.create_event_type("Sampling", t)
        tpl = rm.CustomRecapTemplate.objects.create(
            name="tpl", event_type=et, tenant=t, created_by=sys
        )
        rm.CustomRecap.objects.create(
            name="cr", event=ev_a, tenant=t, custom_recap_template=tpl,
            created_by=sys, updated_by=sys,
        )

        rows = self._prefetched(t)
        assert len(rows) == 1
        r = rows[0]

        # Reading all three scalars off the prefetched request must not issue
        # a single extra query — this is the whole point of the refactor.
        with CaptureQueriesContext(connection) as ctx:
            events = RequestGQL._events_from_cache(r)
            total = sum(RequestGQL._recap_total_for_event(e) for e in events)
            recap_uuid = next(
                (
                    str(e.uuid)
                    for e in events
                    if RequestGQL._recap_total_for_event(e) > 0
                ),
                None,
            )
            n_events = len(events)
        assert len(ctx.captured_queries) == 0, ctx.captured_queries

        assert total == 3  # 2 legacy + 1 custom, both counted
        assert recap_uuid == str(ev_a.uuid)  # points at the event WITH a recap
        assert n_events == 2

    def test_no_events_counts_zero(self):
        sys = self.get_system_user()
        t = self.create_tenant(name="Empty Tenant")
        rt = em.RequestType.objects.create(name="RS", tenant=t, created_by=sys)
        em.Request.objects.create(
            name="No events", address="x", request_type=rt, tenant=t,
            created_by=sys,
        )
        r = self._prefetched(t)[0]
        events = RequestGQL._events_from_cache(r)
        assert events == []
        assert sum(RequestGQL._recap_total_for_event(e) for e in events) == 0
