"""Famus Missing Recaps gate — lifted; accepted orphans parked."""

from datetime import date

import pytest
from django.utils import timezone

from events.missing_recaps_gate import (
    ACCEPTED_WALKUP_ORPHAN_EVENT_IDS,
    MISSING_RECAPS_DISABLED_SLUGS,
    missing_recaps_disabled_for_slug,
    missing_recaps_disabled_for_tenant,
    walkup_orphan_filed_count,
)
from events.models import Event
from events.tests.base import EventsGraphQLTestCase
from recaps import models as rm


def test_famus_slug_no_longer_disables_missing_recaps():
    assert "famus-energy" not in MISSING_RECAPS_DISABLED_SLUGS
    assert missing_recaps_disabled_for_slug("famus-energy") is False
    assert missing_recaps_disabled_for_slug("liquid-death") is False
    assert missing_recaps_disabled_for_slug("") is False
    assert missing_recaps_disabled_for_slug(None) is False


def test_tenant_object_famus_enabled():
    class T:
        slug = None
        request_url_name = "famus-energy"

    assert missing_recaps_disabled_for_tenant(T()) is False

    class Other:
        slug = "wook-water"
        request_url_name = "wook-water"

    assert missing_recaps_disabled_for_tenant(Other()) is False
    assert missing_recaps_disabled_for_tenant(None) is False


def test_accepted_orphans_parked():
    assert ACCEPTED_WALKUP_ORPHAN_EVENT_IDS == frozenset(
        {6948, 6988, 7021, 7039}
    )


@pytest.mark.django_db(transaction=True)
class TestWalkupOrphanKpiParksAccepted(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="KPI Orphan Park")

    def test_parked_orphan_ids_excluded_from_kpi(self, monkeypatch):
        parked = Event.objects.create(
            tenant=self.tenant,
            name="Parked orphan",
            address="1 A St",
            date=date.today(),
            created_by=self.system_user,
            request=None,
        )
        live = Event.objects.create(
            tenant=self.tenant,
            name="Actionable orphan",
            address="2 B St",
            date=date.today(),
            created_by=self.system_user,
            request=None,
        )
        for ev in (parked, live):
            rm.Recap.objects.create(
                name=f"filed-{ev.id}",
                event=ev,
                created_by=self.system_user,
                updated_by=self.system_user,
                products_sold=3,
                submited_at=timezone.now(),
            )

        monkeypatch.setattr(
            "events.missing_recaps_gate.ACCEPTED_WALKUP_ORPHAN_EVENT_IDS",
            frozenset({parked.id}),
        )
        assert walkup_orphan_filed_count(self.tenant.id) == 1
        assert walkup_orphan_filed_count(self.tenant.id, days=45) >= 1
