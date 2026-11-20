"""The read-only Ignite-domain audit (command + cron endpoint)."""

import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import Client, override_settings

from gqlauth.models import UserStatus
from tenants.ignite_domain_audit import build_ignite_domain_audit
from tenants.models import User
from tenants.tests.base import BaseGraphQLTestCase

SECRET = "audit-test-secret"


@pytest.mark.django_db(transaction=True)
class TestIgniteDomainAudit(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, transactional_db):
        self.roles = self.setup_default_roles()
        self.kyle = self._user("kyle@igniteproductions.co", "client", True)
        self.planted = self._user("eve@igniteproductions.co", "ambassador", True)
        self.unverified = self._user("Mallory@IgniteProductions.co", "client", False)
        self.staff_unverified = self._user("ops@igniteproductions.co", "spark_admin", False)
        self.outsider_admin = self._user("evil@gmail.com", "spark_admin", True)
        self.ba = self._user("someba@example.com", "ambassador", True)

    def _user(self, email, role, verified):
        user = User.objects.create_user(
            username=email, email=email, role=self.roles[role], password="x"
        )
        UserStatus.objects.update_or_create(
            user=user, defaults={"verified": verified, "archived": False}
        )
        return user

    def test_flags_planted_and_unverified_ignite_accounts(self):
        payload = build_ignite_domain_audit()
        by_id = {r["id"]: r for r in payload["ignite_domain_accounts"]}
        assert set(by_id) == {
            self.kyle.id, self.planted.id, self.unverified.id, self.staff_unverified.id
        }
        assert by_id[self.kyle.id]["suspicious"] is False
        assert by_id[self.kyle.id]["admin_after_fix"] is True
        assert by_id[self.planted.id]["admin_before_fix"] is True
        assert by_id[self.planted.id]["admin_after_fix"] is False
        assert "ambassador-role" in by_id[self.planted.id]["reasons"]
        assert by_id[self.unverified.id]["admin_after_fix"] is False
        assert "unverified" in by_id[self.unverified.id]["reasons"]
        # spark-admin role still admin, regardless of verification.
        assert by_id[self.staff_unverified.id]["admin_after_fix"] is True

    def test_other_domain_elevated_accounts_show_domain_only(self):
        payload = build_ignite_domain_audit()
        rows = payload["elevated_accounts_other_domains"]
        row = next(r for r in rows if r["id"] == self.outsider_admin.id)
        assert row["email_domain"] == "gmail.com"
        assert "evil" not in json.dumps(rows)
        # Plain BAs never appear anywhere in the payload.
        assert "someba" not in json.dumps(payload, default=str)

    def test_command_runs_and_prints_json(self):
        out = StringIO()
        call_command("audit_ignite_domain_accounts", "--json", stdout=out)
        text = out.getvalue()
        assert "eve@igniteproductions.co" in text
        assert "IGNITEDOMAINAUDIT_JSON_START" in text

    @override_settings(INTERNAL_CRON_SECRET=SECRET)
    def test_cron_endpoint_is_secret_gated_and_read_only(self):
        client = Client()
        url = "/internal/cron/audit-ignite-domain-accounts"
        assert client.post(url).status_code == 401
        before = User.objects.count()
        resp = client.post(url, HTTP_X_CRON_SECRET=SECRET)
        assert resp.status_code == 200
        body = resp.json()
        assert body["read_only"] is True
        assert body["summary"]["ignite_domain_suspicious"] >= 2
        assert User.objects.count() == before
