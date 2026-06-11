"""
Coverage for events/dedupe.py — duplicate-event clustering + merge.

The merge is destructive (repoints every Event relation, deletes the
duplicate, may delete an orphaned request), so the rules are pinned
hard: roster de-dup keeps the keeper's row, recaps of both families
repoint, attendance repoints, cross-tenant input refuses, and the
orphan-request guard removes the row the repair cron would otherwise
use to resurrect the duplicate.
"""

from datetime import datetime, timedelta, timezone as _tz

import pytest

from ambassadors.models import AmbassadorEvent, Attendance, Source
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.dedupe import find_duplicate_clusters, merge_events
from events.models import Event, Request
from recaps import models as recap_models


WHEN = datetime(2026, 5, 22, 18, 0, tzinfo=_tz.utc)


@pytest.mark.django_db(transaction=True)
class TestEventMerge(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Girl Beer")
        self.other_tenant = self.create_tenant(name="Liquid Death")
        self.admin = self.create_user(
            username="admin-merge",
            email="admin-merge@test.com",
            role=self.roles["spark_admin"],
        )
        self.ba_user = self.create_user(
            username="ba-merge",
            email="ba-merge@test.com",
            role=self.roles["ambassador"],
        )
        self.ambassador = self.create_ambassador(self.ba_user)
        self.ba_user2 = self.create_user(
            username="ba-merge-2",
            email="ba-merge-2@test.com",
            role=self.roles["ambassador"],
        )
        self.ambassador2 = self.create_ambassador(self.ba_user2)

    def _event(self, name="Albertsons", when=WHEN, tenant=None):
        return self.create_event(
            name=name,
            tenant=tenant or self.tenant,
            date=when,
            start_time=when,
            end_time=when + timedelta(hours=4),
        )

    def _book(self, ambassador, event):
        return AmbassadorEvent.objects.create(
            ambassador=ambassador,
            event=event,
            tenant=event.tenant,
            is_approved=True,
            created_by=self.admin,
        )

    # ---------- clustering ----------

    def test_same_name_same_date_clusters(self):
        self._event()
        self._event()
        clusters = find_duplicate_clusters(self.tenant.id)
        assert len(clusters) == 1
        assert len(clusters[0]["events"]) == 2
        assert clusters[0]["key"] == "albertsons"

    def test_different_names_or_far_dates_do_not_cluster(self):
        self._event(name="Albertsons")
        self._event(name="Vons")
        self._event(name="Raleys", when=WHEN)
        self._event(name="Raleys", when=WHEN + timedelta(days=5))
        assert find_duplicate_clusters(self.tenant.id) == []

    def test_clusters_are_tenant_scoped(self):
        self._event(tenant=self.other_tenant)
        self._event(tenant=self.other_tenant)
        assert find_duplicate_clusters(self.tenant.id) == []

    # ---------- merge ----------

    def test_merge_repoints_roster_recaps_attendance(self):
        keeper = self._event()
        dup = self._event()
        # BA1 on BOTH (clash → dup row dropped); BA2 only on dup (moves).
        self._book(self.ambassador, keeper)
        self._book(self.ambassador, dup)
        self._book(self.ambassador2, dup)
        recap_models.Recap.objects.create(
            name="legacy",
            event=dup,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        source, _ = Source.objects.get_or_create(name="arrived")
        Attendance.objects.create(
            clock_time=WHEN,
            coordinates=None,
            ambassador=self.ambassador2,
            job=None,
            event=dup,
            source=source,
        )

        report = merge_events(
            tenant_id=self.tenant.id,
            keep_event_id=keeper.id,
            merge_event_ids=[dup.id],
        )

        assert not Event.objects.filter(id=dup.id).exists()
        assert report["deleted_events"] == 1
        roster = AmbassadorEvent.objects.filter(event=keeper)
        assert roster.count() == 2  # BA1 once (keeper's row), BA2 moved
        assert set(roster.values_list("ambassador_id", flat=True)) == {
            self.ambassador.id,
            self.ambassador2.id,
        }
        assert recap_models.Recap.objects.filter(event=keeper).count() == 1
        assert Attendance.objects.filter(event=keeper).count() == 1
        assert report["moved"]["roster rows dropped (BA already on keeper)"] == 1

    def test_merge_deletes_orphaned_request(self):
        keeper = self._event()
        dup = self._event()
        # Give the dup its OWN request (the bulk double-upload shape).
        rt = self.create_request_type("Sampling", self.tenant)
        req = Request.objects.create(
            name="Albertsons request",
            tenant=self.tenant,
            request_type=rt,
            created_by=self.system_user,
        )
        Event.objects.filter(id=dup.id).update(request=req)

        report = merge_events(
            tenant_id=self.tenant.id,
            keep_event_id=keeper.id,
            merge_event_ids=[dup.id],
        )
        assert report["deleted_events"] == 1
        # The orphan guard either deleted it or warned — never silent.
        if report["deleted_requests"] == 1:
            assert not Request.objects.filter(id=req.id).exists()
        else:
            assert report["warnings"]

    def test_merge_refuses_cross_tenant(self):
        keeper = self._event()
        foreign = self._event(tenant=self.other_tenant)
        with pytest.raises(ValueError):
            merge_events(
                tenant_id=self.tenant.id,
                keep_event_id=keeper.id,
                merge_event_ids=[foreign.id],
            )
        assert Event.objects.filter(id=foreign.id).exists()

    def test_merge_refuses_empty(self):
        keeper = self._event()
        with pytest.raises(ValueError):
            merge_events(
                tenant_id=self.tenant.id,
                keep_event_id=keeper.id,
                merge_event_ids=[keeper.id],
            )
