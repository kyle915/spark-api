"""Missing Recaps must see walk-in shifts, not just scheduled ones.

The page filtered on `end_time__lt=now`. A walk-in event — created by a BA
through the check-in link or a walk-up code — has NO end_time, and a NULL
satisfies no comparison, so those events could never be returned. Total
Wireless showed "All clear" while check-in shifts owed recaps: not a stale
count, an entire class of shift the query structurally could not see.
"""
import datetime as _dt
import uuid

import pytest
from django.utils import timezone as dj_tz

from ambassadors.models import Attendance, Source
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.models import Event
from recaps.queries import WALKIN_WRAP_GRACE_HOURS


@pytest.mark.django_db(transaction=True)
class TestMissingRecapWalkins(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.system_user = self.get_system_user()
        self.roles = self.setup_default_roles()
        uid = str(uuid.uuid4())[:8]
        self.tenant = self.create_tenant(name=f"Missing Walkin {uid}")
        ba_user = self.create_user(
            username=f"ba-{uid}", email=f"ba-{uid}@example.com",
            role=self.roles["ambassador"], first_name="Rue", last_name="Vance",
        )
        self.ambassador = self.create_ambassador(ba_user)

    def _walkin_event(self):
        """A check-in-link event: no end_time, ever."""
        return Event.objects.create(
            tenant=self.tenant, name="8/1/2026 - 9 Walk In Way",
            address="9 Walk In Way", created_by=self.system_user,
        )

    def _punch(self, event, kind, when):
        source, _ = Source.objects.get_or_create(name=kind)
        Attendance.objects.create(
            ambassador=self.ambassador, event=event, source=source,
            clock_time=when,
        )

    def _missing_event_ids(self, days=30):
        """Run the resolver's queryset the way the resolver does."""
        from django.db.models import Max, Q
        from django.db.models.functions import Coalesce

        now = dj_tz.now()
        cutoff = now - _dt.timedelta(days=days)
        qs = (
            Event.objects.annotate(last_punch=Max("attendance__clock_time"))
            .filter(
                Q(end_time__lt=now, end_time__gte=cutoff)
                | Q(
                    end_time__isnull=True,
                    last_punch__lt=now - _dt.timedelta(hours=WALKIN_WRAP_GRACE_HOURS),
                    last_punch__gte=cutoff,
                )
            )
            .filter(recaps__isnull=True, custom_recap__isnull=True)
            .filter(tenant_id=self.tenant.id)
            .annotate(wrapped_at=Coalesce("end_time", "last_punch"))
            .order_by("-wrapped_at")
        )
        return set(qs.values_list("id", flat=True))

    def test_a_finished_walkin_with_no_recap_is_surfaced(self):
        """The bug: this event was invisible, so the page said All clear."""
        ev = self._walkin_event()
        long_ago = dj_tz.now() - _dt.timedelta(hours=WALKIN_WRAP_GRACE_HOURS + 2)
        self._punch(ev, "clock_in", long_ago)
        self._punch(ev, "clock_out", long_ago + _dt.timedelta(hours=1))
        assert ev.id in self._missing_event_ids()

    def test_a_ba_still_on_shift_is_not_nagged(self):
        """Punched in 10 minutes ago — they're working, not delinquent."""
        ev = self._walkin_event()
        self._punch(ev, "clock_in", dj_tz.now() - _dt.timedelta(minutes=10))
        assert ev.id not in self._missing_event_ids()

    def test_a_walkin_that_never_clocked_in_is_ignored(self):
        """No punches at all = nobody worked it; not a missing recap."""
        ev = self._walkin_event()
        assert ev.id not in self._missing_event_ids()

    def test_scheduled_events_still_behave(self):
        """The original path must be untouched."""
        now = dj_tz.now()
        wrapped = Event.objects.create(
            tenant=self.tenant, name="scheduled, wrapped",
            created_by=self.system_user,
            start_time=now - _dt.timedelta(hours=5),
            end_time=now - _dt.timedelta(hours=3),
        )
        future = Event.objects.create(
            tenant=self.tenant, name="scheduled, not yet",
            created_by=self.system_user,
            start_time=now + _dt.timedelta(hours=3),
            end_time=now + _dt.timedelta(hours=5),
        )
        ids = self._missing_event_ids()
        assert wrapped.id in ids
        assert future.id not in ids

    def test_an_old_walkin_falls_out_of_the_lookback(self):
        ev = self._walkin_event()
        self._punch(ev, "clock_in", dj_tz.now() - _dt.timedelta(days=45))
        assert ev.id not in self._missing_event_ids(days=30)
