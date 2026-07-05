"""
Tests for the digest cron endpoints.

Covers the security boundary (secret-gating + fail-closed when the
secret env var is missing) and the management-command pass-through
(--dry-run, --skip-empty, window/days flags get plumbed correctly).

These run with Django's test Client + override_settings; no live
mailer / no Cloud Run. The send command itself is mocked at the
call_command level so we don't smoke a real email.
"""

import pytest
from unittest.mock import patch
from django.test import Client, override_settings
from django.urls import reverse


# Endpoint paths — pinned here so a future URL refactor surfaces as
# a single point of failure instead of a swarm of broken tests.
ADMIN_DIGEST_URL = "/internal/cron/send-admin-digest"
EXEC_SUMMARY_URL = "/internal/cron/send-executive-summary"
SCHEDULED_REPORTS_URL = "/internal/cron/send-scheduled-client-reports"
REPAIR_EVENT_STATUS_URL = "/internal/cron/repair-approved-event-status"
REPAIR_MISSING_EVENTS_URL = (
    "/internal/cron/repair-missing-events-for-approved-requests"
)
REPAIR_EVENT_DATES_URL = "/internal/cron/repair-event-dates"
BACKFILL_EVENT_COORDS_URL = "/internal/cron/backfill-event-coordinates"
BACKFILL_AMBASSADOR_COORDS_URL = "/internal/cron/backfill-ambassador-coordinates"

VALID_SECRET = "test-cron-secret-value-only-for-tests"


@pytest.mark.django_db
class TestSendAdminDigestCronView:
    """Coverage for `/internal/cron/send-admin-digest`."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = Client()

    def _post(self, **kwargs):
        return self.client.post(ADMIN_DIGEST_URL, **kwargs)

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_valid_secret_fires_command(self, mock_call):
        mock_call.return_value = None
        resp = self._post(
            HTTP_X_CRON_SECRET=VALID_SECRET,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["window"] == "daily"  # default
        # call_command got invoked with the management command name.
        mock_call.assert_called_once()
        args, _kwargs = mock_call.call_args
        assert args[0] == "send_admin_digest"
        # --skip-empty default is ON.
        assert "--skip-empty" in args

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_bad_secret_returns_401(self, mock_call):
        resp = self._post(HTTP_X_CRON_SECRET="wrong-secret")
        assert resp.status_code == 401
        body = resp.json()
        assert body["ok"] is False
        assert body["error"] == "unauthorized"
        mock_call.assert_not_called()

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_missing_secret_header_returns_401(self, mock_call):
        # No X-Cron-Secret header at all → constant-time compare
        # against "" still fails (we compare to the configured
        # value), so the endpoint denies.
        resp = self._post()
        assert resp.status_code == 401
        mock_call.assert_not_called()

    @override_settings(INTERNAL_CRON_SECRET="")
    @patch("digest.cron_views.call_command")
    def test_unconfigured_secret_fails_closed_503(self, mock_call):
        # Even with a "valid" caller header, an unconfigured secret
        # env var means the endpoint won't run anything — protects
        # against accidentally shipping the endpoint open.
        resp = self._post(HTTP_X_CRON_SECRET=VALID_SECRET)
        assert resp.status_code == 503
        body = resp.json()
        assert body["ok"] is False
        assert body["error"] == "internal-cron-secret-not-configured"
        mock_call.assert_not_called()

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_dry_run_query_param_plumbed(self, mock_call):
        mock_call.return_value = None
        resp = self.client.post(
            f"{ADMIN_DIGEST_URL}?dry_run=true",
            HTTP_X_CRON_SECRET=VALID_SECRET,
        )
        assert resp.status_code == 200
        assert resp.json()["dry_run"] is True
        args, _ = mock_call.call_args
        assert "--dry-run" in args

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_window_weekly_param_plumbed(self, mock_call):
        mock_call.return_value = None
        resp = self.client.post(
            f"{ADMIN_DIGEST_URL}?window=weekly",
            HTTP_X_CRON_SECRET=VALID_SECRET,
        )
        assert resp.status_code == 200
        assert resp.json()["window"] == "weekly"
        args, _ = mock_call.call_args
        assert args[0] == "send_admin_digest"
        # Window flag passed through positionally to call_command.
        assert "--window" in args
        idx = list(args).index("--window")
        assert args[idx + 1] == "weekly"

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    def test_invalid_window_returns_400(self):
        resp = self.client.post(
            f"{ADMIN_DIGEST_URL}?window=hourly",
            HTTP_X_CRON_SECRET=VALID_SECRET,
        )
        assert resp.status_code == 400

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch(
        "digest.cron_views.call_command", side_effect=RuntimeError("boom"),
    )
    def test_command_failure_surfaces_500_with_detail(self, _mock_call):
        resp = self._post(HTTP_X_CRON_SECRET=VALID_SECRET)
        assert resp.status_code == 500
        body = resp.json()
        assert body["ok"] is False
        assert body["error"] == "command-failed"
        assert "boom" in body["detail"]

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    def test_get_is_secret_gated_liveness(self):
        # GET still requires the secret, but returns a benign
        # "endpoint exists" payload without running the command.
        bad = self.client.get(ADMIN_DIGEST_URL)
        assert bad.status_code == 401

        ok = self.client.get(
            ADMIN_DIGEST_URL, HTTP_X_CRON_SECRET=VALID_SECRET
        )
        assert ok.status_code == 200
        assert ok.json()["endpoint"] == "send-admin-digest"


@pytest.mark.django_db
class TestSendExecutiveSummaryCronView:
    """Sibling coverage for `/internal/cron/send-executive-summary`.

    Less exhaustive than the admin-digest tests — same code path,
    different command + different params. We only verify the
    happy path + the parameter plumbing that differs (`days`).
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = Client()

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_valid_secret_fires_exec_summary_command(self, mock_call):
        mock_call.return_value = None
        resp = self.client.post(
            EXEC_SUMMARY_URL, HTTP_X_CRON_SECRET=VALID_SECRET
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["days"] == 7  # default
        args, _ = mock_call.call_args
        assert args[0] == "send_executive_summary"
        assert "--skip-empty" in args

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_days_param_plumbed(self, mock_call):
        mock_call.return_value = None
        resp = self.client.post(
            f"{EXEC_SUMMARY_URL}?days=14",
            HTTP_X_CRON_SECRET=VALID_SECRET,
        )
        assert resp.status_code == 200
        assert resp.json()["days"] == 14
        args, _ = mock_call.call_args
        assert "--days" in args
        idx = list(args).index("--days")
        assert args[idx + 1] == "14"

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    def test_bad_days_param_returns_400(self):
        resp = self.client.post(
            f"{EXEC_SUMMARY_URL}?days=abc",
            HTTP_X_CRON_SECRET=VALID_SECRET,
        )
        assert resp.status_code == 400

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_bad_secret_returns_401(self, mock_call):
        resp = self.client.post(
            EXEC_SUMMARY_URL, HTTP_X_CRON_SECRET="wrong"
        )
        assert resp.status_code == 401
        mock_call.assert_not_called()


@pytest.mark.django_db
class TestSendScheduledClientReportsCronView:
    """Sibling coverage for `/internal/cron/send-scheduled-client-reports`.

    Same secret-gated code path as the other crons; verifies the security
    boundary (missing/wrong secret denied, command never invoked) and that a
    valid secret fires `send_scheduled_client_reports`. The send command is
    mocked at the call_command level — no PDF render, no real email.

    Default (no params) is intentionally argless so the command falls through
    to its own "prior complete month" + opt-in-only behaviour.
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = Client()

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_valid_secret_fires_scheduled_reports_command(self, mock_call):
        mock_call.return_value = None
        resp = self.client.post(
            SCHEDULED_REPORTS_URL, HTTP_X_CRON_SECRET=VALID_SECRET
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["dry_run"] is False  # default
        mock_call.assert_called_once()
        args, _kwargs = mock_call.call_args
        assert args[0] == "send_scheduled_client_reports"
        # No args by default -> command picks the prior complete month and
        # only emails opted-in tenants.
        assert "--dry-run" not in args
        assert "--month" not in args
        assert "--tenant" not in args

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_missing_secret_header_returns_401(self, mock_call):
        resp = self.client.post(SCHEDULED_REPORTS_URL)
        assert resp.status_code == 401
        mock_call.assert_not_called()

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_bad_secret_returns_401(self, mock_call):
        resp = self.client.post(
            SCHEDULED_REPORTS_URL, HTTP_X_CRON_SECRET="wrong"
        )
        assert resp.status_code == 401
        mock_call.assert_not_called()

    @override_settings(INTERNAL_CRON_SECRET="")
    @patch("digest.cron_views.call_command")
    def test_unconfigured_secret_fails_closed_503(self, mock_call):
        resp = self.client.post(
            SCHEDULED_REPORTS_URL, HTTP_X_CRON_SECRET=VALID_SECRET
        )
        assert resp.status_code == 503
        body = resp.json()
        assert body["ok"] is False
        assert body["error"] == "internal-cron-secret-not-configured"
        mock_call.assert_not_called()

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_dry_run_and_month_params_plumbed(self, mock_call):
        mock_call.return_value = None
        resp = self.client.post(
            f"{SCHEDULED_REPORTS_URL}?dry_run=true&month=2026-05",
            HTTP_X_CRON_SECRET=VALID_SECRET,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["dry_run"] is True
        assert body["month"] == "2026-05"
        args, _ = mock_call.call_args
        assert args[0] == "send_scheduled_client_reports"
        assert "--dry-run" in args
        assert "--month" in args
        idx = list(args).index("--month")
        assert args[idx + 1] == "2026-05"

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_tenant_param_plumbed(self, mock_call):
        mock_call.return_value = None
        resp = self.client.post(
            f"{SCHEDULED_REPORTS_URL}?tenant=12",
            HTTP_X_CRON_SECRET=VALID_SECRET,
        )
        assert resp.status_code == 200
        args, _ = mock_call.call_args
        assert "--tenant" in args
        idx = list(args).index("--tenant")
        assert args[idx + 1] == "12"

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    def test_bad_tenant_param_returns_400(self):
        resp = self.client.post(
            f"{SCHEDULED_REPORTS_URL}?tenant=abc",
            HTTP_X_CRON_SECRET=VALID_SECRET,
        )
        assert resp.status_code == 400

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch(
        "digest.cron_views.call_command", side_effect=RuntimeError("boom"),
    )
    def test_command_failure_surfaces_500_with_detail(self, _mock_call):
        resp = self.client.post(
            SCHEDULED_REPORTS_URL, HTTP_X_CRON_SECRET=VALID_SECRET
        )
        assert resp.status_code == 500
        body = resp.json()
        assert body["ok"] is False
        assert body["error"] == "command-failed"
        assert "boom" in body["detail"]

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    def test_get_is_secret_gated_liveness(self):
        bad = self.client.get(SCHEDULED_REPORTS_URL)
        assert bad.status_code == 401

        ok = self.client.get(
            SCHEDULED_REPORTS_URL, HTTP_X_CRON_SECRET=VALID_SECRET
        )
        assert ok.status_code == 200
        assert ok.json()["endpoint"] == "send-scheduled-client-reports"


@pytest.mark.django_db
class TestRepairApprovedEventStatusCronView:
    """Coverage for `/internal/cron/repair-approved-event-status`.

    Same secret-gated code path as the other crons; verifies the security
    boundary (missing/wrong/unconfigured secret denied, command never invoked)
    and — the safety-critical bit — that the backfill defaults to DRY-RUN
    (`dry_run=True`, NO writes) unless `execute=true` is explicitly passed. The
    command is mocked at the call_command level: no DB writes, and the mock
    writes a sentinel to the captured stdout buffer so we can assert the report
    is returned verbatim in the response.

    Unlike the siblings (which pass positional --flags), this view calls
    call_command with KWARGS (dry_run=, tenant=, stdout=), so the assertions
    inspect kwargs.
    """

    REPORT_SENTINEL = "Summary\n  Updated: 3 event(s) -> approved\n"

    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = Client()

    @staticmethod
    def _write_report(*_args, **kwargs):
        """Mimic the command writing to the stdout buffer it was handed."""
        buf = kwargs.get("stdout")
        if buf is not None:
            buf.write(TestRepairApprovedEventStatusCronView.REPORT_SENTINEL)

    # ── Default is DRY-RUN (no writes) ──────────────────────────────

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_default_is_dry_run_and_returns_report(self, mock_call):
        # No `execute` param → must run the backfill in DRY-RUN.
        mock_call.side_effect = self._write_report
        resp = self.client.post(
            REPAIR_EVENT_STATUS_URL, HTTP_X_CRON_SECRET=VALID_SECRET
        )
        assert resp.status_code == 200
        body = resp.json()
        # Response contract: executed / tenant / report.
        assert body["executed"] is False
        assert body["tenant"] is None
        # Captured command stdout is returned verbatim.
        assert body["report"] == self.REPORT_SENTINEL

        mock_call.assert_called_once()
        args, kwargs = mock_call.call_args
        assert args[0] == "repair_approved_event_status"
        # DEFAULT MUST BE DRY-RUN: dry_run=True, no tenant scope.
        assert kwargs["dry_run"] is True
        assert kwargs["tenant"] is None
        # stdout is a captured buffer, not the real stdout.
        assert kwargs["stdout"] is not None

    # ── execute=true flips to a real (write) run ────────────────────

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_execute_true_runs_for_real(self, mock_call):
        mock_call.side_effect = self._write_report
        resp = self.client.post(
            f"{REPAIR_EVENT_STATUS_URL}?execute=true",
            HTTP_X_CRON_SECRET=VALID_SECRET,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["executed"] is True
        assert body["report"] == self.REPORT_SENTINEL
        args, kwargs = mock_call.call_args
        assert args[0] == "repair_approved_event_status"
        # execute=true => dry_run=False (writes allowed).
        assert kwargs["dry_run"] is False

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_execute_falsey_values_stay_dry_run(self, mock_call):
        # Anything that isn't an explicit truthy token must remain DRY-RUN.
        for falsey in ("false", "0", "no", "", "maybe"):
            mock_call.reset_mock()
            resp = self.client.post(
                f"{REPAIR_EVENT_STATUS_URL}?execute={falsey}",
                HTTP_X_CRON_SECRET=VALID_SECRET,
            )
            assert resp.status_code == 200
            assert resp.json()["executed"] is False
            _args, kwargs = mock_call.call_args
            assert kwargs["dry_run"] is True, f"execute={falsey!r} must stay dry-run"

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_tenant_param_plumbed_as_kwarg(self, mock_call):
        mock_call.side_effect = self._write_report
        resp = self.client.post(
            f"{REPAIR_EVENT_STATUS_URL}?tenant=liquid-death",
            HTTP_X_CRON_SECRET=VALID_SECRET,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["tenant"] == "liquid-death"
        _args, kwargs = mock_call.call_args
        assert kwargs["tenant"] == "liquid-death"
        # tenant scoping must not change the dry-run default.
        assert kwargs["dry_run"] is True

    # ── Security boundary ───────────────────────────────────────────

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_missing_secret_header_returns_401(self, mock_call):
        resp = self.client.post(REPAIR_EVENT_STATUS_URL)
        assert resp.status_code == 401
        mock_call.assert_not_called()

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_bad_secret_returns_401(self, mock_call):
        resp = self.client.post(
            REPAIR_EVENT_STATUS_URL, HTTP_X_CRON_SECRET="wrong"
        )
        assert resp.status_code == 401
        body = resp.json()
        assert body["error"] == "unauthorized"
        mock_call.assert_not_called()

    @override_settings(INTERNAL_CRON_SECRET="")
    @patch("digest.cron_views.call_command")
    def test_unconfigured_secret_fails_closed_503(self, mock_call):
        resp = self.client.post(
            REPAIR_EVENT_STATUS_URL, HTTP_X_CRON_SECRET=VALID_SECRET
        )
        assert resp.status_code == 503
        body = resp.json()
        assert body["ok"] is False
        assert body["error"] == "internal-cron-secret-not-configured"
        mock_call.assert_not_called()

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch(
        "digest.cron_views.call_command", side_effect=RuntimeError("boom"),
    )
    def test_command_failure_surfaces_500_with_detail(self, _mock_call):
        resp = self.client.post(
            REPAIR_EVENT_STATUS_URL, HTTP_X_CRON_SECRET=VALID_SECRET
        )
        assert resp.status_code == 500
        body = resp.json()
        assert body["ok"] is False
        assert body["error"] == "command-failed"
        assert "boom" in body["detail"]

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    def test_get_is_secret_gated_liveness(self):
        bad = self.client.get(REPAIR_EVENT_STATUS_URL)
        assert bad.status_code == 401

        ok = self.client.get(
            REPAIR_EVENT_STATUS_URL, HTTP_X_CRON_SECRET=VALID_SECRET
        )
        assert ok.status_code == 200
        assert ok.json()["endpoint"] == "repair-approved-event-status"


@pytest.mark.django_db
class TestRepairMissingEventsForApprovedRequestsCronView:
    """Coverage for `/internal/cron/repair-missing-events-for-approved-requests`.

    Same secret-gated code path as its siblings; verifies the security
    boundary and — the safety-critical bit — that the backfill defaults to
    DRY-RUN (NO writes) unless `execute=true` is explicitly passed. The command
    is mocked at the call_command level: no DB writes, and the mock writes a
    sentinel to the captured stdout buffer so we assert the report comes back
    verbatim.

    Unlike repair-approved-event-status (which passes dry_run= kwarg), this
    command opts in to writes via --execute, so the view passes execute= and
    the assertions inspect that kwarg.
    """

    REPORT_SENTINEL = "Summary\n  Created: 3 event(s)\n"

    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = Client()

    @staticmethod
    def _write_report(*_args, **kwargs):
        """Mimic the command writing to the stdout buffer it was handed."""
        buf = kwargs.get("stdout")
        if buf is not None:
            buf.write(
                TestRepairMissingEventsForApprovedRequestsCronView.REPORT_SENTINEL
            )

    # ── Default is DRY-RUN (no writes) ──────────────────────────────

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_default_is_dry_run_and_returns_report(self, mock_call):
        mock_call.side_effect = self._write_report
        resp = self.client.post(
            REPAIR_MISSING_EVENTS_URL, HTTP_X_CRON_SECRET=VALID_SECRET
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["executed"] is False
        assert body["tenant"] is None
        assert body["report"] == self.REPORT_SENTINEL

        mock_call.assert_called_once()
        args, kwargs = mock_call.call_args
        assert args[0] == "repair_missing_events_for_approved_requests"
        # DEFAULT MUST BE DRY-RUN: execute=False, no tenant scope.
        assert kwargs["execute"] is False
        assert kwargs["tenant"] is None
        assert kwargs["stdout"] is not None

    # ── execute=true flips to a real (write) run ────────────────────

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_execute_true_runs_for_real(self, mock_call):
        mock_call.side_effect = self._write_report
        resp = self.client.post(
            f"{REPAIR_MISSING_EVENTS_URL}?execute=true",
            HTTP_X_CRON_SECRET=VALID_SECRET,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["executed"] is True
        assert body["report"] == self.REPORT_SENTINEL
        args, kwargs = mock_call.call_args
        assert args[0] == "repair_missing_events_for_approved_requests"
        assert kwargs["execute"] is True

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_execute_falsey_values_stay_dry_run(self, mock_call):
        for falsey in ("false", "0", "no", "", "maybe"):
            mock_call.reset_mock()
            resp = self.client.post(
                f"{REPAIR_MISSING_EVENTS_URL}?execute={falsey}",
                HTTP_X_CRON_SECRET=VALID_SECRET,
            )
            assert resp.status_code == 200
            assert resp.json()["executed"] is False
            _args, kwargs = mock_call.call_args
            assert kwargs["execute"] is False, (
                f"execute={falsey!r} must stay dry-run"
            )

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_tenant_param_plumbed_as_kwarg(self, mock_call):
        mock_call.side_effect = self._write_report
        resp = self.client.post(
            f"{REPAIR_MISSING_EVENTS_URL}?tenant=liquid-death",
            HTTP_X_CRON_SECRET=VALID_SECRET,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["tenant"] == "liquid-death"
        _args, kwargs = mock_call.call_args
        assert kwargs["tenant"] == "liquid-death"
        # tenant scoping must not change the dry-run default.
        assert kwargs["execute"] is False

    # ── Security boundary ───────────────────────────────────────────

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_missing_secret_header_returns_401(self, mock_call):
        resp = self.client.post(REPAIR_MISSING_EVENTS_URL)
        assert resp.status_code == 401
        mock_call.assert_not_called()

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_bad_secret_returns_401(self, mock_call):
        resp = self.client.post(
            REPAIR_MISSING_EVENTS_URL, HTTP_X_CRON_SECRET="wrong"
        )
        assert resp.status_code == 401
        assert resp.json()["error"] == "unauthorized"
        mock_call.assert_not_called()

    @override_settings(INTERNAL_CRON_SECRET="")
    @patch("digest.cron_views.call_command")
    def test_unconfigured_secret_fails_closed_503(self, mock_call):
        resp = self.client.post(
            REPAIR_MISSING_EVENTS_URL, HTTP_X_CRON_SECRET=VALID_SECRET
        )
        assert resp.status_code == 503
        assert resp.json()["error"] == "internal-cron-secret-not-configured"
        mock_call.assert_not_called()

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch(
        "digest.cron_views.call_command", side_effect=RuntimeError("boom"),
    )
    def test_command_failure_surfaces_500_with_detail(self, _mock_call):
        resp = self.client.post(
            REPAIR_MISSING_EVENTS_URL, HTTP_X_CRON_SECRET=VALID_SECRET
        )
        assert resp.status_code == 500
        body = resp.json()
        assert body["ok"] is False
        assert body["error"] == "command-failed"
        assert "boom" in body["detail"]

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    def test_get_is_secret_gated_liveness(self):
        bad = self.client.get(REPAIR_MISSING_EVENTS_URL)
        assert bad.status_code == 401

        ok = self.client.get(
            REPAIR_MISSING_EVENTS_URL, HTTP_X_CRON_SECRET=VALID_SECRET
        )
        assert ok.status_code == 200
        assert (
            ok.json()["endpoint"]
            == "repair-missing-events-for-approved-requests"
        )


@pytest.mark.django_db
class TestRepairEventDatesCronView:
    """Coverage for `/internal/cron/repair-event-dates`.

    Same secret-gated code path as its siblings; verifies the security boundary
    and — the safety-critical bit — that the backfill defaults to DRY-RUN (NO
    writes) unless `execute=true` is explicitly passed. The command is mocked
    at the call_command level: no DB writes, and the mock writes a sentinel to
    the captured stdout buffer so we assert the report comes back verbatim.
    """

    REPORT_SENTINEL = "Summary\n  Updated: 3 event(s)\n"

    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = Client()

    @staticmethod
    def _write_report(*_args, **kwargs):
        buf = kwargs.get("stdout")
        if buf is not None:
            buf.write(TestRepairEventDatesCronView.REPORT_SENTINEL)

    # ── Default is DRY-RUN (no writes) ──────────────────────────────

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_default_is_dry_run_and_returns_report(self, mock_call):
        mock_call.side_effect = self._write_report
        resp = self.client.post(
            REPAIR_EVENT_DATES_URL, HTTP_X_CRON_SECRET=VALID_SECRET
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["executed"] is False
        assert body["tenant"] is None
        assert body["report"] == self.REPORT_SENTINEL

        mock_call.assert_called_once()
        args, kwargs = mock_call.call_args
        assert args[0] == "repair_event_dates"
        assert kwargs["execute"] is False
        assert kwargs["tenant"] is None
        assert kwargs["stdout"] is not None

    # ── execute=true flips to a real (write) run ────────────────────

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_execute_true_runs_for_real(self, mock_call):
        mock_call.side_effect = self._write_report
        resp = self.client.post(
            f"{REPAIR_EVENT_DATES_URL}?execute=true",
            HTTP_X_CRON_SECRET=VALID_SECRET,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["executed"] is True
        assert body["report"] == self.REPORT_SENTINEL
        args, kwargs = mock_call.call_args
        assert args[0] == "repair_event_dates"
        assert kwargs["execute"] is True

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_execute_falsey_values_stay_dry_run(self, mock_call):
        for falsey in ("false", "0", "no", "", "maybe"):
            mock_call.reset_mock()
            mock_call.side_effect = self._write_report
            resp = self.client.post(
                f"{REPAIR_EVENT_DATES_URL}?execute={falsey}",
                HTTP_X_CRON_SECRET=VALID_SECRET,
            )
            assert resp.status_code == 200
            assert resp.json()["executed"] is False
            _args, kwargs = mock_call.call_args
            assert kwargs["execute"] is False, (
                f"execute={falsey!r} must stay dry-run"
            )

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_tenant_param_plumbed_as_kwarg(self, mock_call):
        mock_call.side_effect = self._write_report
        resp = self.client.post(
            f"{REPAIR_EVENT_DATES_URL}?tenant=liquid-death",
            HTTP_X_CRON_SECRET=VALID_SECRET,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["tenant"] == "liquid-death"
        _args, kwargs = mock_call.call_args
        assert kwargs["tenant"] == "liquid-death"
        assert kwargs["execute"] is False

    # ── Security boundary ───────────────────────────────────────────

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_missing_secret_header_returns_401(self, mock_call):
        resp = self.client.post(REPAIR_EVENT_DATES_URL)
        assert resp.status_code == 401
        mock_call.assert_not_called()

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_bad_secret_returns_401(self, mock_call):
        resp = self.client.post(
            REPAIR_EVENT_DATES_URL, HTTP_X_CRON_SECRET="wrong"
        )
        assert resp.status_code == 401
        mock_call.assert_not_called()

    @override_settings(INTERNAL_CRON_SECRET="")
    @patch("digest.cron_views.call_command")
    def test_unconfigured_secret_fails_closed_503(self, mock_call):
        resp = self.client.post(
            REPAIR_EVENT_DATES_URL, HTTP_X_CRON_SECRET=VALID_SECRET
        )
        assert resp.status_code == 503
        mock_call.assert_not_called()

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch(
        "digest.cron_views.call_command", side_effect=RuntimeError("boom"),
    )
    def test_command_failure_surfaces_500_with_detail(self, _mock_call):
        resp = self.client.post(
            REPAIR_EVENT_DATES_URL, HTTP_X_CRON_SECRET=VALID_SECRET
        )
        assert resp.status_code == 500
        body = resp.json()
        assert body["ok"] is False
        assert body["error"] == "command-failed"
        assert "boom" in body["detail"]

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    def test_get_is_secret_gated_liveness(self):
        bad = self.client.get(REPAIR_EVENT_DATES_URL)
        assert bad.status_code == 401

        ok = self.client.get(
            REPAIR_EVENT_DATES_URL, HTTP_X_CRON_SECRET=VALID_SECRET
        )
        assert ok.status_code == 200
        assert ok.json()["endpoint"] == "repair-event-dates"


@pytest.mark.django_db
class TestBackfillEventCoordinatesCronView:
    """Coverage for `/internal/cron/backfill-event-coordinates`.

    Same secret-gated path as the repair siblings; verifies the security
    boundary and that the backfill defaults to DRY-RUN (NO writes / NO real
    geocoding) unless `execute=true` is explicitly passed. call_command is
    mocked: no DB writes, no network — the mock writes a sentinel to the
    captured stdout so we assert the report comes back verbatim.
    """

    REPORT_SENTINEL = "Summary\n  Updated: 2 event(s)\n"

    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = Client()

    @staticmethod
    def _write_report(*_args, **kwargs):
        buf = kwargs.get("stdout")
        if buf is not None:
            buf.write(TestBackfillEventCoordinatesCronView.REPORT_SENTINEL)

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_default_is_dry_run_and_returns_report(self, mock_call):
        mock_call.side_effect = self._write_report
        resp = self.client.post(
            BACKFILL_EVENT_COORDS_URL, HTTP_X_CRON_SECRET=VALID_SECRET
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["executed"] is False
        assert body["tenant"] is None
        assert body["report"] == self.REPORT_SENTINEL
        mock_call.assert_called_once()
        args, kwargs = mock_call.call_args
        assert args[0] == "backfill_event_coordinates"
        assert kwargs["execute"] is False
        assert kwargs["tenant"] is None
        assert kwargs["stdout"] is not None

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_execute_true_runs_for_real(self, mock_call):
        mock_call.side_effect = self._write_report
        resp = self.client.post(
            f"{BACKFILL_EVENT_COORDS_URL}?execute=true",
            HTTP_X_CRON_SECRET=VALID_SECRET,
        )
        assert resp.status_code == 200
        assert resp.json()["executed"] is True
        _args, kwargs = mock_call.call_args
        assert kwargs["execute"] is True

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_execute_falsey_values_stay_dry_run(self, mock_call):
        for falsey in ("false", "0", "no", "", "maybe"):
            mock_call.reset_mock()
            mock_call.side_effect = self._write_report
            resp = self.client.post(
                f"{BACKFILL_EVENT_COORDS_URL}?execute={falsey}",
                HTTP_X_CRON_SECRET=VALID_SECRET,
            )
            assert resp.status_code == 200
            assert resp.json()["executed"] is False
            _args, kwargs = mock_call.call_args
            assert kwargs["execute"] is False, (
                f"execute={falsey!r} must stay dry-run"
            )

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_tenant_param_plumbed_as_kwarg(self, mock_call):
        mock_call.side_effect = self._write_report
        resp = self.client.post(
            f"{BACKFILL_EVENT_COORDS_URL}?tenant=liquid-death",
            HTTP_X_CRON_SECRET=VALID_SECRET,
        )
        assert resp.status_code == 200
        assert resp.json()["tenant"] == "liquid-death"
        _args, kwargs = mock_call.call_args
        assert kwargs["tenant"] == "liquid-death"
        assert kwargs["execute"] is False

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_missing_secret_header_returns_401(self, mock_call):
        resp = self.client.post(BACKFILL_EVENT_COORDS_URL)
        assert resp.status_code == 401
        mock_call.assert_not_called()

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_bad_secret_returns_401(self, mock_call):
        resp = self.client.post(
            BACKFILL_EVENT_COORDS_URL, HTTP_X_CRON_SECRET="wrong"
        )
        assert resp.status_code == 401
        mock_call.assert_not_called()

    @override_settings(INTERNAL_CRON_SECRET="")
    @patch("digest.cron_views.call_command")
    def test_unconfigured_secret_fails_closed_503(self, mock_call):
        resp = self.client.post(
            BACKFILL_EVENT_COORDS_URL, HTTP_X_CRON_SECRET=VALID_SECRET
        )
        assert resp.status_code == 503
        mock_call.assert_not_called()

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch(
        "digest.cron_views.call_command", side_effect=RuntimeError("boom"),
    )
    def test_command_failure_surfaces_500_with_detail(self, _mock_call):
        resp = self.client.post(
            BACKFILL_EVENT_COORDS_URL, HTTP_X_CRON_SECRET=VALID_SECRET
        )
        assert resp.status_code == 500
        body = resp.json()
        assert body["ok"] is False
        assert body["error"] == "command-failed"
        assert "boom" in body["detail"]

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    def test_get_is_secret_gated_liveness(self):
        assert self.client.get(BACKFILL_EVENT_COORDS_URL).status_code == 401
        ok = self.client.get(
            BACKFILL_EVENT_COORDS_URL, HTTP_X_CRON_SECRET=VALID_SECRET
        )
        assert ok.status_code == 200
        assert ok.json()["endpoint"] == "backfill-event-coordinates"


@pytest.mark.django_db
class TestBackfillAmbassadorCoordinatesCronView:
    """Coverage for `/internal/cron/backfill-ambassador-coordinates`.

    Mirrors the event-coordinates endpoint: secret-gated, DRY-RUN by default,
    execute/tenant plumbed through to the mocked command.
    """

    REPORT_SENTINEL = "Summary\n  Updated: 4 ambassador(s)\n"

    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = Client()

    @staticmethod
    def _write_report(*_args, **kwargs):
        buf = kwargs.get("stdout")
        if buf is not None:
            buf.write(TestBackfillAmbassadorCoordinatesCronView.REPORT_SENTINEL)

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_default_is_dry_run_and_returns_report(self, mock_call):
        mock_call.side_effect = self._write_report
        resp = self.client.post(
            BACKFILL_AMBASSADOR_COORDS_URL, HTTP_X_CRON_SECRET=VALID_SECRET
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["executed"] is False
        assert body["tenant"] is None
        assert body["report"] == self.REPORT_SENTINEL
        args, kwargs = mock_call.call_args
        assert args[0] == "backfill_ambassador_coordinates"
        assert kwargs["execute"] is False
        assert kwargs["tenant"] is None
        assert kwargs["stdout"] is not None

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_execute_true_runs_for_real(self, mock_call):
        mock_call.side_effect = self._write_report
        resp = self.client.post(
            f"{BACKFILL_AMBASSADOR_COORDS_URL}?execute=1",
            HTTP_X_CRON_SECRET=VALID_SECRET,
        )
        assert resp.status_code == 200
        assert resp.json()["executed"] is True
        _args, kwargs = mock_call.call_args
        assert kwargs["execute"] is True

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_tenant_param_plumbed_as_kwarg(self, mock_call):
        mock_call.side_effect = self._write_report
        resp = self.client.post(
            f"{BACKFILL_AMBASSADOR_COORDS_URL}?tenant=42",
            HTTP_X_CRON_SECRET=VALID_SECRET,
        )
        assert resp.status_code == 200
        _args, kwargs = mock_call.call_args
        assert kwargs["tenant"] == "42"
        assert kwargs["execute"] is False

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_missing_secret_header_returns_401(self, mock_call):
        resp = self.client.post(BACKFILL_AMBASSADOR_COORDS_URL)
        assert resp.status_code == 401
        mock_call.assert_not_called()

    @override_settings(INTERNAL_CRON_SECRET="")
    @patch("digest.cron_views.call_command")
    def test_unconfigured_secret_fails_closed_503(self, mock_call):
        resp = self.client.post(
            BACKFILL_AMBASSADOR_COORDS_URL, HTTP_X_CRON_SECRET=VALID_SECRET
        )
        assert resp.status_code == 503
        mock_call.assert_not_called()

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    def test_get_is_secret_gated_liveness(self):
        assert self.client.get(
            BACKFILL_AMBASSADOR_COORDS_URL
        ).status_code == 401
        ok = self.client.get(
            BACKFILL_AMBASSADOR_COORDS_URL, HTTP_X_CRON_SECRET=VALID_SECRET
        )
        assert ok.status_code == 200
        assert ok.json()["endpoint"] == "backfill-ambassador-coordinates"


@pytest.mark.django_db
class TestCronEndpointsAreCsrfExempt:
    """Regression: the CronRun heartbeat wrapper in digest/urls.py must not
    strip the csrf_exempt attribute off the wrapped view. If it does, EVERY
    cron POST 403s on CSRF (prod incident: all scheduled jobs went dark).
    The default test Client doesn't enforce CSRF, so this uses
    enforce_csrf_checks=True to actually catch it."""

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    def test_post_is_csrf_exempt(self):
        csrf_client = Client(enforce_csrf_checks=True)
        # No CSRF token + no secret: a csrf-exempt view reaches the secret
        # gate and returns 401 (unauthorized). A NON-exempt view would 403
        # on CSRF before ever running. So 401 (not 403) proves exemption.
        resp = csrf_client.post(ADMIN_DIGEST_URL)
        assert resp.status_code == 401, (
            f"expected 401 (csrf-exempt, secret gate), got {resp.status_code} "
            "— the heartbeat wrapper likely dropped csrf_exempt"
        )

    def test_resolved_view_carries_csrf_exempt_attr(self):
        from django.urls import resolve

        for url in (ADMIN_DIGEST_URL, EXEC_SUMMARY_URL, SCHEDULED_REPORTS_URL):
            match = resolve(url)
            assert getattr(match.func, "csrf_exempt", False) is True, (
                f"{url} view is not csrf_exempt"
            )
