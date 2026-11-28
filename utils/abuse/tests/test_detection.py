"""Phase 2 detector + its cron endpoint."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
from django.test import Client

from tenants.tests.base import BaseGraphQLTestCase
from utils.abuse import detection
from utils.abuse.models import AmbassadorReadLog, AuditLogEntry, UsageBucket

NOW = datetime(2026, 11, 17, 18, 0, tzinfo=timezone.utc)
BROWSER = "Mozilla/5.0 (Macintosh) Chrome/131"
TEAM = "alerts@igniteproductions.co"


@pytest.mark.django_db
class TestDetector(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def _setup(self, db):
        roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Brand Co")
        self.normal = self.create_user("normal", "normal@brand.test", roles["client"])
        self.bot = self.create_user("bot", "bot@brand.test", roles["client"])
        self.admin = self.create_user("adm", "adm@igniteproductions.co", roles["spark_admin"])

    def _bucket(self, user, hours_ago, n, *, op="Op", paged=0, ips=("1.1.1.1",),
                uas=(BROWSER,), is_admin=False):
        UsageBucket.objects.create(
            hour=(NOW - timedelta(hours=hours_ago)).replace(minute=0),
            user=user, tenant_id=self.tenant.id, is_admin=is_admin, operation=op,
            request_count=n, paged_count=paged, ips=list(ips), user_agents=list(uas),
        )

    def _baseline(self, user, per_day=200):
        for d in range(2, 15):
            self._bucket(user, 24 * d, per_day, op=f"B{d}")

    def test_normal_user_not_flagged(self):
        self._baseline(self.normal)
        self._bucket(self.normal, 3, 250)
        assert detection.detect(now=NOW) == []

    def test_bulk_scraper_flagged_on_every_signal(self):
        self._baseline(self.bot, per_day=100)
        # 10:00 UTC = 02:00 Pacific → off-hours
        self._bucket(self.bot, 8, 4000, op="RequestsQuery", paged=900,
                     ips=[f"10.0.0.{i}" for i in range(7)],
                     uas=("python-requests/2.32", BROWSER))
        for _ in range(16):
            AuditLogEntry.objects.create(user=self.bot, kind="export",
                                         operation="exportRecapsXlsx",
                                         tenant_id=self.tenant.id)
        AuditLogEntry.objects.update(created_at=NOW - timedelta(hours=2))
        AmbassadorReadLog.objects.bulk_create([
            AmbassadorReadLog(day=NOW.date(), user=self.bot, ambassador_key=f"ba{i}")
            for i in range(120)
        ])
        findings = detection.detect(now=NOW)
        user_f = [f for f in findings if f.scope == "user"]
        assert len(user_f) == 1 and user_f[0].user_id == self.bot.pk
        assert set(user_f[0].signals) >= {
            "volume_spike", "page_walking", "export_burst", "non_browser_ua",
            "many_ips", "off_hours_bulk", "ambassador_harvest",
        }
        assert user_f[0].stats["distinct_ambassadors"] == 120
        assert user_f[0].stats["non_browser_user_agents"] == ["python-requests/2.32"]
        html = detection.render_report_html(findings, 24)
        assert "bot@brand.test" in html and "ambassador_harvest" in html

    def test_admin_usage_ignored(self):
        self._bucket(self.admin, 2, 50000, paged=5000, uas=("curl/8",), is_admin=True)
        assert detection.detect(now=NOW) == []

    def test_non_browser_ua_classifier(self):
        assert detection.is_non_browser_ua("python-httpx/0.27")
        assert detection.is_non_browser_ua("curl/8.4")
        assert detection.is_non_browser_ua("")
        assert detection.is_non_browser_ua("Mozilla/5.0 HeadlessChrome/120")
        assert not detection.is_non_browser_ua(BROWSER)

    def test_report_only_goes_to_ignite_addresses(self, settings):
        settings.ABUSE_ALERT_EMAILS = [TEAM, "someone@brand.test"]
        f = detection.Finding("user", self.bot.pk, "bot@brand.test", self.tenant.id,
                              ["page_walking"], {"paged": 900})
        sent = []

        def fake_send_now(mailer):
            sent.append(mailer.envelope().to_emails)

        with mock.patch("utils.mailer.Mailer.send_now", fake_send_now):
            assert detection.send_report([f], 24) == 1
            assert detection.send_report([], 24) == 0
        assert sent == [[TEAM]]


@pytest.mark.django_db
class TestCronEndpoint:
    URL = "/internal/cron/abuse-usage-watch"

    def test_secret_required(self, settings):
        settings.INTERNAL_CRON_SECRET = "s3cret"
        assert Client().post(self.URL).status_code == 401
        assert Client().post(self.URL, HTTP_X_CRON_SECRET="nope").status_code == 401

    def test_cron_post_is_csrf_exempt(self, settings):
        # The real GitHub Actions cron sends no CSRF token. Django's test Client
        # skips CSRF by default, which hid a 403 on every production run.
        settings.INTERNAL_CRON_SECRET = "s3cret"
        with mock.patch.object(detection, "detect", return_value=[]), \
                mock.patch.object(detection, "prune_counters", return_value=0):
            resp = Client(enforce_csrf_checks=True).post(
                self.URL + "?dry_run=true", HTTP_X_CRON_SECRET="s3cret")
        assert resp.status_code == 200

    def test_dry_run_never_emails(self, settings):
        settings.INTERNAL_CRON_SECRET = "s3cret"
        fake = detection.Finding("user", 1, "x@brand.test", 1, ["page_walking"], {})
        with mock.patch.object(detection, "detect", return_value=[fake]), \
                mock.patch.object(detection, "send_report") as send:
            resp = Client().post(self.URL + "?dry_run=true&notify=true",
                                 HTTP_X_CRON_SECRET="s3cret")
            assert resp.status_code == 200
            body = json.loads(resp.content)
            assert body["dry_run"] is True and body["notify"] is False and body["flagged"] == 1
            send.assert_not_called()

            # notify defaults OFF
            Client().post(self.URL, HTTP_X_CRON_SECRET="s3cret")
            send.assert_not_called()

            Client().post(self.URL + "?notify=true", HTTP_X_CRON_SECRET="s3cret")
            send.assert_called_once()
