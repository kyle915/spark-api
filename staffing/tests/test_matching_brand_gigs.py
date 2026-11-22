"""Brand experience must count only past worked (clocked-in) shifts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as _tz

import pytest

from ambassadors.models import AmbassadorEvent, Attendance, Source
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from staffing.matching import compute_smart_ba_matches


@pytest.mark.django_db(transaction=True)
class TestBrandGigWorkedOnly(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Brand Gig Tenant")
        self.now = datetime.now(_tz.utc)
        self.ba_user = self.create_user(
            username="brand-gig-ba",
            email="brand-gig-ba@test.com",
            role=self.roles["ambassador"],
        )
        self.ba = self.create_ambassador(user=self.ba_user)
        self.create_tenanted_user(self.ba_user, self.tenant)
        self.clock_in_source, _ = Source.objects.get_or_create(
            name="clock_in",
            defaults={
                "created_by": self.system_user,
                "updated_by": self.system_user,
            },
        )

    def _past_event(self, name: str):
        start = self.now - timedelta(days=3)
        return self.create_event(
            name=name,
            tenant=self.tenant,
            date=start,
            start_time=start,
            end_time=start + timedelta(hours=4),
        )

    def _future_event(self, name: str):
        start = self.now + timedelta(days=3)
        return self.create_event(
            name=name,
            tenant=self.tenant,
            date=start,
            start_time=start,
            end_time=start + timedelta(hours=4),
        )

    def test_future_approved_rows_do_not_count_as_brand_gigs(self):
        past = self._past_event("Past worked")
        future = self._future_event("Future approved")
        for event in (past, future):
            AmbassadorEvent.objects.create(
                ambassador=self.ba,
                event=event,
                tenant=self.tenant,
                is_approved=True,
                created_by=self.system_user,
                updated_by=self.system_user,
            )
        Attendance.objects.create(
            ambassador=self.ba,
            event=past,
            source=self.clock_in_source,
            clock_time=past.start_time + timedelta(minutes=5),
            created_by=self.system_user,
            updated_by=self.system_user,
        )

        rows = compute_smart_ba_matches(
            tenant_id=self.tenant.id,
            event_id=None,
            limit=10,
            now=self.now,
        )
        by = {r["ba_numeric_id"]: r for r in rows}
        assert self.ba.id in by
        assert by[self.ba.id]["brand_gigs"] == 1
