"""Legal acceptance: server-side gate, recording (IP via right-most hop),
version bump re-gating, staff report, management command, and the surfaces
that must NOT be gated (anonymous, ambassadors, mobile schema).

Everything runs through the real clients / spark schemas with the gate ON.
"""

from __future__ import annotations

from io import StringIO

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.core.management import call_command

from legal import versions
from legal.graphql_extension import ERROR_CODE, LegalAcceptanceGate
from legal.models import LegalAcceptance
from tenants.tests.base import BaseGraphQLTestCase

CURRENT = "2026-11-17"

DATA_QUERY = "query { myNotificationPreferences { category emailEnabled } }"
ME_QUERY = "query { me { email } }"
STATUS_QUERY = """
query { myLegalStatus {
  required needsAcceptance currentVersion isUpdate changeSummary
  documents { document version acceptedVersion acceptedAt needsAcceptance path }
} }
"""
ACCEPT = """
mutation Accept($version: String!) {
  acceptLegalTerms(version: $version) {
    success message code status { needsAcceptance currentVersion }
  }
}
"""
REPORT = """
query Report($pendingOnly: Boolean!) {
  legalAcceptanceReport(pendingOnly: $pendingOnly) {
    currentVersion totalUsers acceptedCount pendingCount
    rows { email role status acceptedVersion acceptedAt ipAddress }
  }
}
"""


def _codes(result) -> list[str]:
    return [((e.extensions or {}).get("code")) for e in (result.errors or [])]


@pytest.mark.django_db(transaction=True)
class TestLegalAcceptance(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db, settings):
        from config.schema_client import schema_clients

        settings.LEGAL_ACCEPTANCE_ENFORCED = True
        settings.LEGAL_DOCUMENT_VERSIONS = None
        settings.LEGAL_CHANGE_SUMMARY = None
        settings.ABUSE_TRUSTED_PROXY_COUNT = 1
        self.settings = settings
        cache.clear()

        self.schema = schema_clients
        self.endpoint_path = "/api/v615204/graphql/clients"
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Legal Brand")

        self.client_user = self.create_user(
            username="legal-client", email="client@brand.test", role=self.roles["client"]
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        self.admin = self.create_user(
            username="legal-admin", email="admin@spark.test", role=self.roles["spark_admin"]
        )
        self.staff = self.create_user(
            username="legal-staff", email="ops@igniteproductions.co",
            role=self.roles["ambassador"], is_staff=True,
        )
        self.ba = self.create_user(
            username="legal-ba", email="ba@ba-mail.test", role=self.roles["ambassador"]
        )
        self.create_tenanted_user(self.ba, self.tenant)
        yield
        cache.clear()

    async def run(self, query, variables=None, *, user=None, schema=None,
                  xff: str | None = None, ua: str = "pytest-agent/1.0"):
        """Like _execute_mutation but with real request headers (XFF / UA)."""
        from django.test import RequestFactory
        from gqlauth.core.middlewares import USER_OR_ERROR_KEY

        meta = {"HTTP_USER_AGENT": ua}
        if xff is not None:
            meta["HTTP_X_FORWARDED_FOR"] = xff
        wsgi_request = RequestFactory().post(self.endpoint_path, **meta)
        wsgi_request.user = user or AnonymousUser()

        class UOE:
            def __init__(self, u):
                self.user = u
                self.errors = None

        class Req:
            def __init__(self, w):
                self.user = w.user
                self.META = w.META
                self.scope = {"type": "http", USER_OR_ERROR_KEY: UOE(w.user)}
                setattr(self, USER_OR_ERROR_KEY, UOE(w.user))

        class Ctx:
            def __init__(self, r):
                self.request = r

            def __getitem__(self, key):
                if key == "request":
                    return self.request
                raise KeyError(key)

        return await (schema or self.schema).execute(
            query, variable_values=variables or {}, context_value=Ctx(Req(wsgi_request))
        )

    # -- the gate ---------------------------------------------------------

    @pytest.mark.asyncio
    async def test_client_admin_and_staff_are_blocked_until_they_accept(self):
        for user in (self.client_user, self.admin, self.staff):
            result = await self.run(DATA_QUERY, user=user)
            assert result.data is None, user.email
            assert _codes(result) == [ERROR_CODE], user.email
            assert result.errors[0].extensions["version"] == CURRENT

    @pytest.mark.asyncio
    async def test_mutations_are_blocked_too(self):
        result = await self.run(
            """mutation { setMyNotificationPreference(input: {category: "digests", emailEnabled: false}) { success } }""",
            user=self.client_user,
        )
        assert _codes(result) == [ERROR_CODE]

    @pytest.mark.asyncio
    async def test_spark_schema_is_gated_as_well(self):
        from config.schema_spark import schema_spark

        result = await self.run("query { users(first: 1) { edges { node { id } } } }",
                                user=self.admin, schema=schema_spark)
        assert _codes(result) == [ERROR_CODE]

    @pytest.mark.asyncio
    async def test_status_me_and_accept_are_reachable_while_gated(self):
        me = await self.run(ME_QUERY, user=self.client_user)
        assert me.errors is None and me.data["me"]["email"] == "client@brand.test"

        status = await self.run(STATUS_QUERY, user=self.client_user)
        assert status.errors is None
        s = status.data["myLegalStatus"]
        assert s["required"] is True and s["needsAcceptance"] is True
        assert s["currentVersion"] == CURRENT and s["isUpdate"] is False
        assert {d["document"] for d in s["documents"]} == {"terms", "aup", "privacy"}
        assert {d["path"] for d in s["documents"]} == {
            "/terms-of-service", "/acceptable-use-policy", "/privacy-policy"}

        # A mixed operation (allowed + data field) is still blocked.
        mixed = await self.run("query { me { email } myNotificationPreferences { category } }",
                               user=self.client_user)
        assert _codes(mixed) == [ERROR_CODE]

    @pytest.mark.asyncio
    async def test_accept_records_every_document_and_unblocks(self):
        res = await self.run(ACCEPT, {"version": CURRENT}, user=self.client_user,
                             xff="6.6.6.6, 203.0.113.9", ua="Mozilla/5.0 Test")
        assert res.errors is None
        body = res.data["acceptLegalTerms"]
        assert body["success"] is True and body["code"] is None
        assert body["status"]["needsAcceptance"] is False

        rows = [r async for r in LegalAcceptance.objects.filter(user=self.client_user)]
        assert {r.document for r in rows} == {"terms", "aup", "privacy"}
        for r in rows:
            assert r.version == CURRENT
            # Right-most hop, never the spoofable left-most entry.
            assert r.ip_address == "203.0.113.9"
            assert r.user_agent == "Mozilla/5.0 Test"
            assert r.method == "clickwrap"
            assert r.email == "client@brand.test" and r.role == "client"
            assert r.accepted_at is not None

        ok = await self.run(DATA_QUERY, user=self.client_user)
        assert ok.errors is None and ok.data["myNotificationPreferences"] is not None

        # Idempotent: accepting again keeps one row per document.
        again = await self.run(ACCEPT, {"version": CURRENT}, user=self.client_user)
        assert again.data["acceptLegalTerms"]["success"] is True
        assert await LegalAcceptance.objects.filter(user=self.client_user).acount() == 3

    @pytest.mark.asyncio
    async def test_ip_honours_trusted_proxy_count(self):
        self.settings.ABUSE_TRUSTED_PROXY_COUNT = 2
        await self.run(ACCEPT, {"version": CURRENT}, user=self.admin,
                       xff="6.6.6.6, 198.51.100.7, 35.191.0.1")
        row = await LegalAcceptance.objects.aget(user=self.admin, document="terms")
        assert row.ip_address == "198.51.100.7"

    @pytest.mark.asyncio
    async def test_stale_version_is_rejected(self):
        res = await self.run(ACCEPT, {"version": "2025-01-01"}, user=self.client_user)
        body = res.data["acceptLegalTerms"]
        assert body["success"] is False and body["code"] == "VERSION_MISMATCH"
        assert body["status"]["currentVersion"] == CURRENT
        assert await LegalAcceptance.objects.filter(user=self.client_user).acount() == 0

    @pytest.mark.asyncio
    async def test_version_bump_regates_and_shows_what_changed(self):
        await self.run(ACCEPT, {"version": CURRENT}, user=self.client_user)
        assert (await self.run(DATA_QUERY, user=self.client_user)).errors is None

        self.settings.LEGAL_DOCUMENT_VERSIONS = {"terms": "2027-03-01"}
        self.settings.LEGAL_CHANGE_SUMMARY = "We clarified how AI summaries use recap data."

        blocked = await self.run(DATA_QUERY, user=self.client_user)
        assert _codes(blocked) == [ERROR_CODE]
        assert blocked.errors[0].extensions["version"] == "2027-03-01"

        s = (await self.run(STATUS_QUERY, user=self.client_user)).data["myLegalStatus"]
        assert s["needsAcceptance"] is True and s["isUpdate"] is True
        assert s["currentVersion"] == "2027-03-01"
        assert s["changeSummary"] == "We clarified how AI summaries use recap data."
        terms = next(d for d in s["documents"] if d["document"] == "terms")
        assert terms["acceptedVersion"] == CURRENT and terms["needsAcceptance"] is True
        privacy = next(d for d in s["documents"] if d["document"] == "privacy")
        assert privacy["needsAcceptance"] is False

        res = await self.run(ACCEPT, {"version": "2027-03-01"}, user=self.client_user)
        assert res.data["acceptLegalTerms"]["success"] is True
        assert (await self.run(DATA_QUERY, user=self.client_user)).errors is None
        # The original acceptance is kept: the audit trail is append-only.
        assert await LegalAcceptance.objects.filter(
            user=self.client_user, document="terms").acount() == 2

    # -- surfaces that must not be gated -----------------------------------

    @pytest.mark.asyncio
    async def test_ambassadors_are_not_gated(self):
        res = await self.run(DATA_QUERY, user=self.ba)
        assert ERROR_CODE not in _codes(res)
        s = (await self.run(STATUS_QUERY, user=self.ba)).data["myLegalStatus"]
        assert s["required"] is False and s["needsAcceptance"] is False

    @pytest.mark.asyncio
    async def test_anonymous_is_not_gated(self):
        res = await self.run(DATA_QUERY)
        assert ERROR_CODE not in _codes(res)

    @pytest.mark.asyncio
    async def test_mobile_and_ambassador_schemas_never_gate(self):
        from config.schema_ambassador import schema_ambassador
        from config.schema_mobile import schema_mobile

        for schema in (schema_mobile, schema_ambassador):
            assert LegalAcceptanceGate not in schema.extensions
            res = await self.run(ME_QUERY, user=self.client_user, schema=schema)
            assert ERROR_CODE not in _codes(res)

    def test_gate_is_off_when_disabled(self):
        import asyncio

        self.settings.LEGAL_ACCEPTANCE_ENFORCED = False
        res = asyncio.run(self.run(DATA_QUERY, user=self.client_user))
        assert ERROR_CODE not in _codes(res)

    # -- staff report + command ---------------------------------------------

    @pytest.mark.asyncio
    async def test_staff_report_lists_who_accepted_what_and_when(self):
        await self.run(ACCEPT, {"version": CURRENT}, user=self.staff, xff="192.0.2.44")
        await self.run(ACCEPT, {"version": CURRENT}, user=self.client_user, xff="192.0.2.10")

        res = await self.run(REPORT, {"pendingOnly": False}, user=self.staff)
        assert res.errors is None
        rep = res.data["legalAcceptanceReport"]
        assert rep["currentVersion"] == CURRENT
        by_email = {r["email"]: r for r in rep["rows"]}
        # BAs are not in the population; client, admin and staff are.
        assert set(by_email) == {"client@brand.test", "admin@spark.test", "ops@igniteproductions.co"}
        assert by_email["client@brand.test"]["status"] == "accepted"
        assert by_email["client@brand.test"]["ipAddress"] == "192.0.2.10"
        assert by_email["client@brand.test"]["acceptedAt"] is not None
        assert by_email["admin@spark.test"]["status"] == "never"
        assert rep["acceptedCount"] == 2 and rep["pendingCount"] == 1

        pending = await self.run(REPORT, {"pendingOnly": True}, user=self.staff)
        assert [r["email"] for r in pending.data["legalAcceptanceReport"]["rows"]] == ["admin@spark.test"]

    @pytest.mark.asyncio
    async def test_report_is_staff_only(self):
        await self.run(ACCEPT, {"version": CURRENT}, user=self.client_user)
        res = await self.run(REPORT, {"pendingOnly": False}, user=self.client_user)
        assert res.data is None
        assert _codes(res) == ["FORBIDDEN"]

    def test_management_command_lists_pending_users(self):
        from asgiref.sync import async_to_sync

        async_to_sync(self.run)(ACCEPT, {"version": CURRENT}, user=self.client_user)
        out = StringIO()
        call_command("legal_acceptance_report", stdout=out)
        text = out.getvalue()
        assert "admin@spark.test" in text and "ops@igniteproductions.co" in text
        assert "client@brand.test" not in text
        assert "1 of 3 admin/client users accepted; 2 pending" in text

        out = StringIO()
        call_command("legal_acceptance_report", "--all", stdout=out)
        assert "client@brand.test" in out.getvalue()


def test_versions_single_source_of_truth(settings):
    settings.LEGAL_DOCUMENT_VERSIONS = None
    assert versions.current_versions() == {"terms": CURRENT, "aup": CURRENT, "privacy": CURRENT}
    assert versions.current_version() == CURRENT
    settings.LEGAL_DOCUMENT_VERSIONS = {"privacy": "2027-01-05"}
    assert versions.current_version() == "2027-01-05"


def test_client_ip_right_most_hop(settings):
    from django.test import RequestFactory

    from legal.service import client_ip

    settings.ABUSE_TRUSTED_PROXY_COUNT = 1
    rf = RequestFactory()
    assert client_ip(rf.get("/", HTTP_X_FORWARDED_FOR="1.1.1.1, 2.2.2.2")) == "2.2.2.2"
    assert client_ip(rf.get("/", REMOTE_ADDR="10.0.0.5")) == "10.0.0.5"
    settings.ABUSE_TRUSTED_PROXY_COUNT = 5
    assert client_ip(rf.get("/", HTTP_X_FORWARDED_FOR="1.1.1.1, 2.2.2.2")) == "1.1.1.1"
