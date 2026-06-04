"""
Coverage for the internal-event RMM routing fix:

  * events/routing.py :: compute_request_routing / route_request_sync — the
    shared, email-free routing used by the internal create hook and the
    backfill. Stamps request.state from the address + assigns the territory
    RMM, only filling BLANKS (idempotent).
  * events/management/commands/backfill_request_rmm_routing.py — DRY-RUN by
    default (no writes, no Sheets calls); --execute assigns + re-syncs the
    linked sheet, capped at --limit, reporting how many REMAIN.

The Google Sheets upsert is STUBBED in the execute test (no network).
"""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from events import models as event_models
from events.routing import compute_request_routing, route_request_sync
from events.tests.base import EventsGraphQLTestCase

# Where the command imports upsert_request_row (patch target).
UPSERT_PATH = (
    "events.management.commands.backfill_request_rmm_routing.upsert_request_row"
)


@pytest.mark.django_db
class TestRequestRmmRouting(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        self.system_user = self.get_system_user()
        self.roles = self.setup_default_roles()

        # Liquid Death — the only territory-mapped tenant today. The map keys
        # on request_url_name / slug == "ighn-liquid-death".
        self.ld = self.create_tenant(
            name="Liquid Death", request_url_name="ighn-liquid-death"
        )
        # Manuela Cristancho owns GA/FL/NC/... in LIQUID_DEATH_TERRITORY.
        self.manuela = self.create_user(
            username="m.cristancho@liquiddeath.com",
            email="m.cristancho@liquiddeath.com",
            role=self.roles["spark_admin"],
            first_name="Manuela",
            last_name="Cristancho",
        )
        self.ga = event_models.State.objects.create(
            name="Georgia", code="GA", created_by=self.system_user
        )
        self.request_type = self.create_request_type(
            name="Retail Sampling", tenant=self.ld
        )

    # ── factory ────────────────────────────────────────────────────────
    def _make_request(self, *, tenant=None, address="1355 S Park St, Carrollton, GA 30117", **kwargs):
        return event_models.Request.objects.create(
            name="Kroger Pharmacy",
            address=address,
            request_type=self.request_type,
            tenant=tenant or self.ld,
            created_by=self.system_user,
            **kwargs,
        )

    # ── compute_request_routing (read-only) ─────────────────────────────
    def test_compute_assigns_territory_owner_and_state(self):
        req = self._make_request()
        assigned, state_code, state_obj = compute_request_routing(req)
        assert assigned is not None and assigned.id == self.manuela.id
        assert state_code == "GA"
        assert state_obj is not None and state_obj.id == self.ga.id
        # Read-only: nothing persisted.
        req.refresh_from_db()
        assert req.rmm_asigned_id is None
        assert req.state_id is None

    def test_compute_unrouted_tenant_gets_no_rmm(self):
        other = self.create_tenant(name="Total Wireless", request_url_name="total-wireless")
        req = self._make_request(tenant=other)
        assigned, state_code, _state_obj = compute_request_routing(req)
        assert assigned is None  # not in the territory map, no default RMM
        assert state_code == "GA"  # state still resolves from the address

    def test_compute_honors_default_external_rmm_override(self):
        override_user = self.create_user(
            username="ops@total-wireless.com",
            email="ops@total-wireless.com",
            role=self.roles["spark_admin"],
        )
        other = self.create_tenant(
            name="TW", request_url_name="tw", default_external_rmm=override_user
        )
        req = self._make_request(tenant=other)
        assigned, _state_code, _state_obj = compute_request_routing(req)
        assert assigned is not None and assigned.id == override_user.id

    # ── route_request_sync (persists, signal-free) ──────────────────────
    def test_route_request_sync_persists_and_is_idempotent(self):
        req = self._make_request()
        assigned, state_code, changed = route_request_sync(req)
        assert changed is True
        assert assigned.id == self.manuela.id
        assert state_code == "GA"
        req.refresh_from_db()
        assert req.rmm_asigned_id == self.manuela.id
        assert req.state_id == self.ga.id

        # Second run is a no-op (only fills blanks).
        _assigned2, _code2, changed2 = route_request_sync(req)
        assert changed2 is False

    def test_route_request_sync_does_not_overwrite_existing_rmm(self):
        other_rmm = self.create_user(
            username="ross@liquiddeath.com",
            email="ross@liquiddeath.com",
            role=self.roles["spark_admin"],
        )
        req = self._make_request(rmm_asigned=other_rmm)
        _assigned, _code, changed = route_request_sync(req)
        req.refresh_from_db()
        # Pre-set RMM is preserved; only the (blank) state gets stamped.
        assert req.rmm_asigned_id == other_rmm.id
        assert req.state_id == self.ga.id
        assert changed is True

    # ── backfill command ────────────────────────────────────────────────
    def test_backfill_dry_run_writes_nothing(self):
        req = self._make_request()
        out = StringIO()
        with patch(UPSERT_PATH) as mock_upsert:
            call_command("backfill_request_rmm_routing", stdout=out)
            mock_upsert.assert_not_called()  # no Sheets calls in dry-run
        req.refresh_from_db()
        assert req.rmm_asigned_id is None  # untouched
        assert req.state_id is None
        report = out.getvalue()
        assert "DRY RUN" in report
        assert "RESULT mode=dry-run" in report
        assert "would_rmm=1" in report

    def test_backfill_execute_assigns_and_reports(self):
        self._make_request()
        out = StringIO()
        with patch(UPSERT_PATH, return_value=True) as mock_upsert:
            call_command("backfill_request_rmm_routing", execute=True, stdout=out)
            mock_upsert.assert_called_once()  # one changed row → one re-sync
        report = out.getvalue()
        assert "RESULT mode=execute" in report
        assert "assigned=1" in report
        assert "synced=1" in report
        assert "remaining=0" in report
        assert (
            event_models.Request.objects.filter(rmm_asigned=self.manuela).count() == 1
        )

    def test_backfill_execute_is_idempotent(self):
        self._make_request()
        with patch(UPSERT_PATH, return_value=True):
            call_command("backfill_request_rmm_routing", execute=True, stdout=StringIO())
            # Second run: the row now has an RMM, so it's no longer a candidate.
            out2 = StringIO()
            with patch(UPSERT_PATH) as mock_upsert2:
                call_command(
                    "backfill_request_rmm_routing", execute=True, stdout=out2
                )
                mock_upsert2.assert_not_called()
            assert "candidates=0" in out2.getvalue()

    def test_backfill_caps_at_limit_and_reports_remaining(self):
        for _ in range(3):
            self._make_request()
        out1 = StringIO()
        with patch(UPSERT_PATH, return_value=True):
            call_command(
                "backfill_request_rmm_routing", execute=True, limit=2, stdout=out1
            )
        r1 = out1.getvalue()
        assert "assigned=2" in r1
        assert "remaining=1" in r1
        assert (
            event_models.Request.objects.filter(rmm_asigned=self.manuela).count() == 2
        )

        out2 = StringIO()
        with patch(UPSERT_PATH, return_value=True):
            call_command(
                "backfill_request_rmm_routing", execute=True, limit=2, stdout=out2
            )
        r2 = out2.getvalue()
        assert "assigned=1" in r2
        assert "remaining=0" in r2
        assert (
            event_models.Request.objects.filter(rmm_asigned=self.manuela).count() == 3
        )

    def test_geocode_state_fallback_routes_unparseable_address(self):
        # Address with NO parseable state (just a venue name). Without
        # --geocode-state it can't route; WITH it, Photon resolves "Georgia"
        # → GA → m.cristancho's territory.
        req = event_models.Request.objects.create(
            name="Walmart SC",
            address="Walmart Supercenter 389",  # no city/state for the regex
            request_type=self.request_type,
            tenant=self.ld,
            created_by=self.system_user,
        )
        # Sanity: not routable without geocoding.
        out0 = StringIO()
        with patch(UPSERT_PATH, return_value=True):
            call_command(
                "backfill_request_rmm_routing", execute=True, stdout=out0
            )
        assert "assigned=0" in out0.getvalue()
        req.refresh_from_db()
        assert req.rmm_asigned_id is None

        # With --geocode-state: Photon → "Georgia" → GA → Manuela.
        out1 = StringIO()
        with patch(UPSERT_PATH, return_value=True), patch("time.sleep"), patch(
            "utils.geocoding.photon_state_for_address", return_value="Georgia"
        ):
            call_command(
                "backfill_request_rmm_routing",
                execute=True,
                geocode_state=True,
                stdout=out1,
            )
        r1 = out1.getvalue()
        assert "geocoded=1" in r1
        assert "assigned=1" in r1
        req.refresh_from_db()
        assert req.rmm_asigned_id == self.manuela.id
        assert req.state_id == self.ga.id

    def test_backfill_stamps_state_for_rmm_set_but_state_null(self):
        # The old public-form path set the RMM but never stamped request.state,
        # so the Market/State column was blank and the RMM couldn't see the row
        # on their sheet. The backfill must catch these (candidates = no RMM OR
        # no state) and stamp the state without disturbing the RMM.
        req = self._make_request(rmm_asigned=self.manuela)  # rmm set, state null
        assert req.state_id is None
        out = StringIO()
        with patch(UPSERT_PATH, return_value=True) as mock_upsert:
            call_command("backfill_request_rmm_routing", execute=True, stdout=out)
            mock_upsert.assert_called_once()  # re-synced with the stamped state
        req.refresh_from_db()
        assert req.state_id == self.ga.id  # state now stamped
        assert req.rmm_asigned_id == self.manuela.id  # RMM preserved
        report = out.getvalue()
        assert "stated=1" in report
        assert "unroutable=0" in report

    # ── force path (--ids + --force-state) ──────────────────────────────
    def test_force_state_stamps_explicit_state_and_assigns_rmm(self):
        # A venue-only address the parser + Photon can't resolve, with no RMM
        # and no state — the genuinely-incomplete case. The operator knows it's
        # Florida and forces it; the territory RMM (Manuela) then attaches and
        # the Sheet row re-syncs.
        fl = event_models.State.objects.create(
            name="Florida", code="FL", created_by=self.system_user
        )
        req = event_models.Request.objects.create(
            name="Conde Nast",
            address="1 World Trade Center",  # no parseable state
            request_type=self.request_type,
            tenant=self.ld,
            created_by=self.system_user,
        )
        assert req.state_id is None and req.rmm_asigned_id is None
        out = StringIO()
        with patch(UPSERT_PATH, return_value=True) as mock_upsert:
            call_command(
                "backfill_request_rmm_routing",
                execute=True,
                ids=str(req.id),
                force_state="FL",
                stdout=out,
            )
            mock_upsert.assert_called_once()  # one row re-synced
        req.refresh_from_db()
        assert req.state_id == fl.id  # state forced
        assert req.rmm_asigned_id == self.manuela.id  # FL → m.cristancho
        report = out.getvalue()
        assert "RESULT mode=execute force_state=FL" in report
        assert "forced=1" in report
        assert "assigned=1" in report
        assert "synced=1" in report

    def test_force_state_preserves_existing_rmm(self):
        # A row that already has an RMM but no state: force the state, keep RMM.
        ga = self.ga
        req = self._make_request(rmm_asigned=self.manuela, address="venue only")
        assert req.state_id is None
        out = StringIO()
        with patch(UPSERT_PATH, return_value=True):
            call_command(
                "backfill_request_rmm_routing",
                execute=True,
                ids=str(req.id),
                force_state="GA",
                stdout=out,
            )
        req.refresh_from_db()
        assert req.state_id == ga.id
        assert req.rmm_asigned_id == self.manuela.id  # unchanged
        # RMM was already set → not counted as a NEW assignment.
        assert "assigned=0" in out.getvalue()

    def test_force_state_dry_run_writes_nothing(self):
        req = self._make_request()
        out = StringIO()
        with patch(UPSERT_PATH) as mock_upsert:
            call_command(
                "backfill_request_rmm_routing",
                ids=str(req.id),
                force_state="GA",
                stdout=out,
            )
            mock_upsert.assert_not_called()
        req.refresh_from_db()
        assert req.state_id is None  # untouched in dry-run
        assert "RESULT mode=dry-run force_state=GA" in out.getvalue()

    def test_force_state_requires_both_ids_and_state(self):
        with pytest.raises(CommandError):
            call_command(
                "backfill_request_rmm_routing",
                execute=True,
                ids="1",
                stdout=StringIO(),
            )

    def test_force_state_unknown_code_errors(self):
        req = self._make_request()
        with pytest.raises(CommandError):
            call_command(
                "backfill_request_rmm_routing",
                execute=True,
                ids=str(req.id),
                force_state="ZZ",  # not a real state
                stdout=StringIO(),
            )
