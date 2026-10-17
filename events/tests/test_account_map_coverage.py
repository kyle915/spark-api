"""Coverage classification: sampled (1 filed visit) / repeat (>1) / never (0).

Counts come only from filed Recap/CustomRecap rows linked via
recap.retailer -> event.retailer -> request.retailer. Requests alone are
Planned, never Sampled.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from events import models as em
from events.admin_payloads import (
    compute_account_map_coverage,
    coverage_class_for,
    list_account_map_pins,
    scoped_requests,
)
from events.tests.base import EventsGraphQLTestCase
from recaps import models as rm


def test_coverage_class_thresholds():
    assert coverage_class_for(0) == "never_visited"
    assert coverage_class_for(1) == "sampled"
    assert coverage_class_for(2) == "repeat"
    assert coverage_class_for(5) == "repeat"


@pytest.mark.django_db(transaction=True)
class TestAccountMapCoverage(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Coverage Tenant")
        self.rt = em.RequestType.objects.create(
            name="Retail Sampling", tenant=self.tenant, created_by=self.sys
        )
        self.approved = em.RequestStatus.objects.create(
            name="Approved", tenant=self.tenant, created_by=self.sys
        )
        self.state = em.State.objects.create(name="Illinois", code="IL", created_by=self.sys)
        self.location = em.Location.objects.create(
            name="Chicago", code="CHI", zip="60601", state=self.state, created_by=self.sys
        )
        now = timezone.now()
        self.sampled = self._retailer("Sampled Store")
        self.repeat = self._retailer("Repeat Store")
        self.never = self._retailer("Never Store")
        # sampled: 1 filed event; repeat: 2 filed events; never: request only
        self._filed_visit(self.sampled, now - timedelta(days=3))
        self._filed_visit(self.repeat, now - timedelta(days=3))
        self._filed_visit(self.repeat, now - timedelta(days=1))
        self._request_only(self.never, now + timedelta(days=5))

    def _retailer(self, name):
        return em.Retailer.objects.create(
            name=name, address="1 Main St", store_contact="Mgr",
            location=self.location, tenant=self.tenant, created_by=self.sys,
        )

    def _filed_visit(self, retailer, when):
        req = em.Request.objects.create(
            name=f"req {retailer.name} {when:%f}", address=retailer.address,
            request_type=self.rt, tenant=self.tenant, created_by=self.sys,
            status=self.approved, retailer=retailer, location=self.location,
            state=self.state, date=when, coordinates=[41.9, -87.6],
        )
        ev = self.create_event(name="shift", tenant=self.tenant, request=req, date=when, address=retailer.address)
        # create_event may not set retailer; link explicitly for the chain.
        em.Event.objects.filter(pk=ev.pk).update(retailer=retailer)
        rm.Recap.objects.create(
            name="filed", event_id=ev.pk, created_by=self.sys, updated_by=self.sys,
            submited_at=when, total_engagements=10,
        )
        return req

    def _request_only(self, retailer, when):
        return em.Request.objects.create(
            name="planned", address=retailer.address, request_type=self.rt,
            tenant=self.tenant, created_by=self.sys, status=self.approved,
            retailer=retailer, location=self.location, state=self.state,
            date=when, coordinates=[42.0, -87.7],
        )

    def test_roster_counts_classify_by_filed_recaps(self):
        cov = compute_account_map_coverage(self.tenant.id)
        assert cov.roster_total == 3
        assert cov.sampled == 1
        assert cov.repeat == 1
        assert cov.never_visited == 1
        assert cov.planned >= 1  # future approved request for Never Store

    def test_pins_carry_coverage_not_request_status(self):
        pins = list_account_map_pins(scoped_requests(self.tenant.id))
        by_name = {}
        for p in pins:
            by_name.setdefault(p.name, p)
        assert by_name["Repeat Store"].coverage_class == "repeat"
        assert by_name["Repeat Store"].visit_count == 2
        assert by_name["Sampled Store"].coverage_class == "sampled"
        # Request alone (future/approved) must be never_visited + planned, not sampled.
        assert by_name["Never Store"].coverage_class == "never_visited"
        assert by_name["Never Store"].planned is True

    def test_empty_stub_recap_does_not_count(self):
        req = self._request_only(self.never, timezone.now() - timedelta(days=1))
        ev = self.create_event(name="stub shift", tenant=self.tenant, request=req, date=timezone.now(), address="x")
        em.Event.objects.filter(pk=ev.pk).update(retailer=self.never)
        rm.Recap.objects.create(name="stub", event_id=ev.pk, created_by=self.sys, updated_by=self.sys)
        cov = compute_account_map_coverage(self.tenant.id)
        assert cov.never_visited == 1
        assert cov.sampled == 1
