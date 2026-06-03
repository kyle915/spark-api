"""
Coverage for the `backfill_ambassador_coordinates` management command.

It geocodes ``Ambassador.address`` via the keyless Photon API to populate
``Ambassador.coordinates`` for BAs with empty coordinates AND an address.
Behaviour contract mirrors the repair_* commands:
    * DRY-RUN by default (writes nothing); --execute opts in to writes.
    * idempotent (a second --execute run updates 0 rows).
    * --tenant scopes to BAs linked (via job applications) to one tenant.
    * saves ONLY the `coordinates` field.

The Photon HTTP call is STUBBED in every test (no real network).
"""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command

from ambassadors.models import Ambassador
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from jobs import models as job_models
from utils.utils import ROLE_ID

GEOCODE_PATH = (
    "ambassadors.management.commands.backfill_ambassador_coordinates.photon_geocode"
)
SLEEP_PATH = (
    "ambassadors.management.commands.backfill_ambassador_coordinates.time.sleep"
)


@pytest.mark.django_db
class TestBackfillAmbassadorCoordinates(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        self.system_user = self.get_system_user()
        self.ba_role = self.create_role(
            name="Ambassador", role_id=ROLE_ID.Ambassadors, slug="ambassador"
        )
        self.tenant = self.create_tenant(name="Liquid Death", slug="liquid-death")
        self.other_tenant = self.create_tenant(
            name="Total Wireless", slug="total-wireless"
        )
        self._user_seq = 0

    # ─── factories ───────────────────────────────────────────────────

    def _make_ba(self, *, address="123 Main St, Reno, NV", coordinates=None):
        self._user_seq += 1
        user = self.create_user(
            username=f"ba{self._user_seq}@example.com",
            email=f"ba{self._user_seq}@example.com",
            role=self.ba_role,
        )
        return self.create_ambassador(
            user=user,
            address=address,
            coordinates=coordinates if coordinates is not None else [],
        )

    def _link_to_tenant(self, amb, tenant):
        """Give the BA a job application within `tenant` so the --tenant scope
        (Ambassador.job_applications__tenant_id) matches them."""
        from events.models import Event, EventStatus

        status = EventStatus.objects.create(
            name="Approved", slug=f"approved-{tenant.id}", tenant=tenant,
            created_by=self.system_user,
        )
        event = Event.objects.create(
            name="Gig", tenant=tenant, status=status, address="x",
            created_by=self.system_user,
        )
        job_title = self.create_job_title(name=f"Demo-{tenant.id}", tenant=tenant)
        job = self.create_job(
            name="Gig", code=f"GIG-{tenant.id}", address="x",
            event=event, job_title=job_title, tenant=tenant,
        )
        return job_models.JobApplication.objects.create(
            tenant=tenant, job=job, ambassador=amb,
        )

    # ─── geocode path (stubbed Photon) ───────────────────────────────

    def test_execute_geocodes_address(self):
        amb = self._make_ba(address="123 Main St, Reno, NV", coordinates=[])
        out = StringIO()
        with patch(GEOCODE_PATH, return_value=[39.53, -119.81]) as mock_geo, \
                patch(SLEEP_PATH):
            call_command("backfill_ambassador_coordinates", execute=True, stdout=out)
            mock_geo.assert_called_once()
        amb.refresh_from_db()
        assert amb.coordinates == [39.53, -119.81]
        report = out.getvalue()
        assert "Updated: 1 ambassador(s)" in report
        assert "geocoded=1" in report

    def test_geocode_miss_skips_row(self):
        amb = self._make_ba(coordinates=[])
        out = StringIO()
        with patch(GEOCODE_PATH, return_value=None), patch(SLEEP_PATH):
            call_command("backfill_ambassador_coordinates", execute=True, stdout=out)
        amb.refresh_from_db()
        assert amb.coordinates == []  # untouched
        report = out.getvalue()
        assert "Updated: 0 ambassador(s)" in report
        assert "Skipped/failed" in report

    # ─── candidate selection ─────────────────────────────────────────

    def test_ba_without_address_is_not_a_candidate(self):
        amb = self._make_ba(address=None, coordinates=[])
        out = StringIO()
        with patch(GEOCODE_PATH) as mock_geo, patch(SLEEP_PATH):
            call_command("backfill_ambassador_coordinates", execute=True, stdout=out)
            mock_geo.assert_not_called()
        amb.refresh_from_db()
        assert amb.coordinates == []
        assert "0 candidate ambassador(s)" in out.getvalue()

    def test_ba_with_valid_coords_is_not_a_candidate(self):
        amb = self._make_ba(coordinates=[39.0, -119.0])
        out = StringIO()
        with patch(GEOCODE_PATH) as mock_geo, patch(SLEEP_PATH):
            call_command("backfill_ambassador_coordinates", execute=True, stdout=out)
            mock_geo.assert_not_called()
        amb.refresh_from_db()
        assert amb.coordinates == [39.0, -119.0]  # unchanged
        assert "Updated: 0 ambassador(s)" in out.getvalue()

    def test_zero_island_coords_are_a_candidate(self):
        # [0,0] is the null-island sentinel — treated as "needs backfill".
        amb = self._make_ba(coordinates=[0, 0])
        out = StringIO()
        with patch(GEOCODE_PATH, return_value=[39.53, -119.81]), patch(SLEEP_PATH):
            call_command("backfill_ambassador_coordinates", execute=True, stdout=out)
        amb.refresh_from_db()
        assert amb.coordinates == [39.53, -119.81]
        assert "Updated: 1 ambassador(s)" in out.getvalue()

    # ─── idempotency ─────────────────────────────────────────────────

    def test_second_execute_run_updates_zero(self):
        self._make_ba(coordinates=[])
        with patch(GEOCODE_PATH, return_value=[39.53, -119.81]), patch(SLEEP_PATH):
            first = StringIO()
            call_command("backfill_ambassador_coordinates", execute=True, stdout=first)
            assert "Updated: 1 ambassador(s)" in first.getvalue()

            second = StringIO()
            with patch(GEOCODE_PATH) as mock_geo2:
                call_command(
                    "backfill_ambassador_coordinates", execute=True, stdout=second
                )
                mock_geo2.assert_not_called()
            assert "Updated: 0 ambassador(s)" in second.getvalue()

    # ─── dry-run writes nothing / no sleep ───────────────────────────

    def test_dry_run_is_default_and_writes_nothing(self):
        amb = self._make_ba(coordinates=[])
        out = StringIO()
        with patch(GEOCODE_PATH, return_value=[39.53, -119.81]) as mock_geo, \
                patch(SLEEP_PATH) as mock_sleep:
            call_command("backfill_ambassador_coordinates", stdout=out)  # dry-run
            # Count-only: dry-run never geocodes or sleeps (the 504 fix).
            mock_geo.assert_not_called()
            mock_sleep.assert_not_called()
        amb.refresh_from_db()
        assert amb.coordinates == []  # untouched
        report = out.getvalue()
        assert "DRY RUN" in report
        assert "Would geocode 1 ambassador(s)" in report

    def test_execute_caps_geocode_at_limit_and_reports_remaining(self):
        # 3 BAs all need a geocode. --limit 2 geocodes only 2 this run and
        # reports 1 remaining; a second run drains it (keeps each request under
        # the Cloud Run timeout).
        for _ in range(3):
            self._make_ba(coordinates=[])
        out1 = StringIO()
        with patch(GEOCODE_PATH, return_value=[39.53, -119.81]), patch(SLEEP_PATH):
            call_command(
                "backfill_ambassador_coordinates", execute=True, limit=2, stdout=out1
            )
        r1 = out1.getvalue()
        assert "geocoded=2" in r1
        assert "Remaining (still need geocode): 1" in r1
        assert Ambassador.objects.filter(coordinates=[39.53, -119.81]).count() == 2

        out2 = StringIO()
        with patch(GEOCODE_PATH, return_value=[39.53, -119.81]), patch(SLEEP_PATH):
            call_command(
                "backfill_ambassador_coordinates", execute=True, limit=2, stdout=out2
            )
        r2 = out2.getvalue()
        assert "geocoded=1" in r2
        assert "Remaining (still need geocode): 0" in r2
        assert Ambassador.objects.filter(coordinates=[39.53, -119.81]).count() == 3

    # ─── --tenant scoping ────────────────────────────────────────────

    def test_tenant_scope_only_touches_linked_bas(self):
        mine = self._make_ba(coordinates=[])
        theirs = self._make_ba(coordinates=[])
        self._link_to_tenant(mine, self.tenant)
        self._link_to_tenant(theirs, self.other_tenant)

        out = StringIO()
        with patch(GEOCODE_PATH, return_value=[39.53, -119.81]), patch(SLEEP_PATH):
            call_command(
                "backfill_ambassador_coordinates", execute=True,
                tenant="liquid-death", stdout=out,
            )
        mine.refresh_from_db()
        theirs.refresh_from_db()
        assert mine.coordinates == [39.53, -119.81]  # linked → repaired
        assert theirs.coordinates == []  # other tenant's BA untouched
        assert "Updated: 1 ambassador(s)" in out.getvalue()

    # ─── only saves the coordinates field ────────────────────────────

    def test_only_coordinates_field_is_saved(self):
        amb = self._make_ba(coordinates=[])
        Ambassador.objects.filter(id=amb.id).update(rating=99)
        with patch(GEOCODE_PATH, return_value=[39.53, -119.81]), patch(SLEEP_PATH):
            call_command(
                "backfill_ambassador_coordinates", execute=True, stdout=StringIO()
            )
        amb.refresh_from_db()
        assert amb.coordinates == [39.53, -119.81]
        assert amb.rating == 99  # command didn't clobber other fields
