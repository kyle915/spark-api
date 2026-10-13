from unittest.mock import patch

import pytest
from django.test import Client, override_settings

SECRET = "rollup-secret"
SEND_URL = "/internal/cron/send-daily-retail-rollup"
SET_URL = "/internal/cron/set-daily-retail-rollup"


@pytest.mark.django_db
class TestDailyRetailRollupCron:
    @override_settings(INTERNAL_CRON_SECRET=SECRET)
    def test_requires_secret(self):
        assert Client().post(SEND_URL).status_code == 401
        assert Client().post(SET_URL, {"tenant": "17"}).status_code == 401

    @override_settings(INTERNAL_CRON_SECRET=SECRET)
    @patch("digest.rollup_cron_views.call_command")
    def test_send_passes_flags(self, mock_call):
        resp = Client().post(
            f"{SEND_URL}?dry_run=true&tenant=17",
            {"date": "2026-10-12", "preview_to": "kyle@igniteproductions.co"},
            HTTP_X_CRON_SECRET=SECRET,
        )
        assert resp.status_code == 200
        args = mock_call.call_args[0]
        assert args[0] == "send_daily_retail_rollup"
        assert "--dry-run" in args
        assert args[args.index("--date") + 1] == "2026-10-12"
        assert args[args.index("--preview-to") + 1] == "kyle@igniteproductions.co"

    @override_settings(INTERNAL_CRON_SECRET=SECRET)
    @patch("digest.rollup_cron_views.call_command")
    def test_preview_requires_tenant(self, mock_call):
        resp = Client().post(
            SEND_URL, {"preview_to": "kyle@igniteproductions.co"}, HTTP_X_CRON_SECRET=SECRET
        )
        assert resp.status_code == 400
        mock_call.assert_not_called()

    @override_settings(INTERNAL_CRON_SECRET=SECRET)
    @patch("digest.rollup_cron_views.call_command")
    def test_set_is_dry_run_unless_apply(self, mock_call):
        Client().post(SET_URL, {"tenant": "keee-torch-thc", "enabled": "true"}, HTTP_X_CRON_SECRET=SECRET)
        assert "--apply" not in mock_call.call_args[0]
        Client().post(
            SET_URL, {"tenant": "keee-torch-thc", "enabled": "true", "apply": "true"},
            HTTP_X_CRON_SECRET=SECRET,
        )
        assert "--apply" in mock_call.call_args[0]
