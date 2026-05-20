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
