"""Anti-scraping guardrails, exercised through the real clients schema.

Covers: the shared Postgres limiter (incl. two "instances" = two DB
connections), real-client-IP parsing, per-user GraphQL rate limits,
depth/alias/token limits, introspection block, brand page-size clamp using
the FE's own RequestsQuery shape, ambassador-list scoping/throttling and the
distinct-BA log, export/bulk caps + audit rows, login throttles, batching.
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest import mock

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth.models import AnonymousUser
from django.db import DatabaseError, connection, connections
from django.test import RequestFactory

from tenants.tests.base import BaseGraphQLTestCase
from utils.abuse import limiter
from utils.abuse.identity import clear_identity_cache, client_ip
from utils.abuse.models import AmbassadorReadLog, AuditLogEntry, RateLimitCounter, UsageBucket

FE_OPS = Path(__file__).parent / "fe_ops"
PATH = "/api/v615204/graphql/clients"


def _codes(result) -> list[str]:
    return [((e.extensions or {}).get("code")) for e in (result.errors or [])]


class _Ctx:
    def __init__(self, request):
        self.request = request

    def __getitem__(self, key):
        if key == "request":
            return self.request
        raise KeyError(key)


async def run(query, variables=None, user=None, *, xff=None, ua="Mozilla/5.0 test",
              operation_name=None):
    from gqlauth.core.middlewares import USER_OR_ERROR_KEY

    from config.schema_client import schema_clients

    extra = {"HTTP_USER_AGENT": ua}
    if xff:
        extra["HTTP_X_FORWARDED_FOR"] = xff
    req = RequestFactory().post(PATH, **extra)
    req.user = user or AnonymousUser()

    class _UOE:
        def __init__(self, u):
            self.user = u
            self.errors = None

    setattr(req, USER_OR_ERROR_KEY, _UOE(req.user))
    return await schema_clients.execute(
        query, variable_values=variables, context_value=_Ctx(req),
        operation_name=operation_name,
    )


# ---------------------------------------------------------------- limiter

@pytest.mark.django_db(transaction=True)
class TestLimiter:
    def test_fixed_window_counts_and_blocks(self):
        now = 1_700_000_010.0
        results = [limiter.hit("t:a", 3, 60, now=now) for _ in range(4)]
        assert [r.allowed for r in results] == [True, True, True, False]
        assert results[-1].count == 4
        assert 1 <= results[-1].retry_after <= 61
        # next window resets
        assert limiter.hit("t:a", 3, 60, now=now + 60).allowed

    def test_hit_many_one_round_trip_and_reports_blocking_key(self):
        now = 1_700_000_000.0
        rules = [("t:m:1m", 2, 60), ("t:m:1h", 100, 3600)]
        assert limiter.hit_many(rules, now=now) is None
        assert limiter.hit_many(rules, now=now) is None
        blocked = limiter.hit_many(rules, now=now)
        assert blocked is not None and blocked.key == "t:m:1m"
        # the hour window still accrued every hit
        assert limiter.peek("t:m:1h", 3600, now=now) == 3

    def test_shared_across_instances(self):
        """Two Cloud Run instances == two independent DB connections. The
        limit is global, not per instance."""
        allowed = []

        def instance(n):
            try:
                for _ in range(n):
                    allowed.append(limiter.hit("t:shared", 10, 3600).allowed)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=instance, args=(8,)) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert allowed.count(True) == 10
        assert allowed.count(False) == 6
        assert RateLimitCounter.objects.get(key="t:shared").count == 16

    def test_fails_open_on_db_error(self):
        with mock.patch.object(connection, "cursor", side_effect=DatabaseError("down")):
            res = limiter.hit("t:down", 1, 60)
            assert res.allowed and res.degraded
            assert limiter.hit_many([("t:down2", 1, 60)]) is None


def test_client_ip_uses_last_hop_not_spoofed_first(settings):
    settings.ABUSE_TRUSTED_PROXY_COUNT = 1
    req = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="1.2.3.4, 203.0.113.9")
    assert client_ip(req) == "203.0.113.9"
    settings.ABUSE_TRUSTED_PROXY_COUNT = 2
    req = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="1.2.3.4, 203.0.113.9, 35.191.0.1")
    assert client_ip(req) == "203.0.113.9"
    req = RequestFactory().get("/", REMOTE_ADDR="10.0.0.5")
    assert client_ip(req) == "10.0.0.5"


# ---------------------------------------------------------------- schema

class _Base(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def _setup(self, db, settings):
        clear_identity_cache()
        settings.DEBUG = False
        self.settings = settings
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Brand Co")
        self.brand = self.create_user("brand1", "brand1@brandco.com", self.roles["client"])
        self.create_tenanted_user(self.brand, self.tenant)
        self.admin = self.create_user("adm1", "adm1@igniteproductions.co", self.roles["spark_admin"])
        yield
        clear_identity_cache()


@pytest.mark.django_db(transaction=True)
class TestGraphQLRateLimit(_Base):
    @pytest.mark.asyncio
    async def test_brand_user_throttled_admin_not(self):
        self.settings.ABUSE_GQL_USER_PER_MINUTE = 3
        self.settings.ABUSE_GQL_ADMIN_PER_MINUTE = 50
        q = "query Me { __typename }"
        for _ in range(3):
            assert (await run(q, user=self.brand)).errors is None
        blocked = await run(q, user=self.brand)
        assert _codes(blocked) == ["RATE_LIMITED"]
        assert blocked.data is None
        assert blocked.errors[0].message.startswith("Too many requests")
        assert blocked.errors[0].extensions["retryAfter"] >= 1
        for _ in range(5):
            assert (await run(q, user=self.admin)).errors is None
        throttled = await sync_to_async(
            lambda: AuditLogEntry.objects.filter(kind="throttled", user_id=self.brand.pk).count()
        )()
        assert throttled == 1

    @pytest.mark.asyncio
    async def test_anonymous_keyed_by_real_ip(self):
        self.settings.ABUSE_GQL_ANON_IP_PER_MINUTE = 2
        q = "{ healthcheck }"
        # Spoofed first hop changes every call; the real (last) hop doesn't.
        for i in range(2):
            assert (await run(q, xff=f"9.9.9.{i}, 198.51.100.7")).errors is None
        assert _codes(await run(q, xff="9.9.9.99, 198.51.100.7")) == ["RATE_LIMITED"]
        assert (await run(q, xff="9.9.9.99, 198.51.100.8")).errors is None

    @pytest.mark.asyncio
    async def test_usage_rollup_one_row_per_hour_operation(self):
        for _ in range(3):
            await run("query SidebarCounts { __typename }", user=self.brand,
                      operation_name="SidebarCounts")
        rows = await sync_to_async(list)(UsageBucket.objects.filter(user_id=self.brand.pk))
        assert len(rows) == 1
        assert rows[0].request_count == 3 and rows[0].operation == "SidebarCounts"
        assert rows[0].tenant_id == self.tenant.id
        assert rows[0].ips == ["127.0.0.1"]

    @pytest.mark.asyncio
    async def test_disabled_switch(self):
        self.settings.ABUSE_GUARDRAILS_ENABLED = False
        self.settings.ABUSE_GQL_USER_PER_MINUTE = 1
        for _ in range(3):
            assert (await run("{ __typename }", user=self.brand)).errors is None


@pytest.mark.django_db(transaction=True)
class TestComplexityAndIntrospection(_Base):
    @pytest.mark.asyncio
    async def test_alias_limit(self):
        q = "{ " + " ".join(f"a{i}: __typename" for i in range(25)) + " }"
        assert "QUERY_TOO_COMPLEX" in _codes(await run(q, user=self.admin))

    @pytest.mark.asyncio
    async def test_token_limit(self):
        q = "{ " + " ".join("__typename" for _ in range(4100)) + " }"
        res = await run(q, user=self.admin)
        assert "QUERY_TOO_COMPLEX" in _codes(res)

    @pytest.mark.asyncio
    async def test_depth_limit(self):
        inner = "id"
        for _ in range(14):
            inner = f"edges {{ node {{ {inner} }} }}"
        res = await run(f"query Deep {{ requests(first: 1) {{ {inner} }} }}", user=self.admin)
        assert "QUERY_TOO_COMPLEX" in _codes(res)

    @pytest.mark.asyncio
    async def test_real_fe_operations_pass_limits(self):
        for name in ("RequestsQuery", "EventUuidQuery", "ApprovalsListQuery", "SetupChecklistQuery"):
            text = (FE_OPS / f"{name}.graphql").read_text()
            res = await run(text, {"first": 5, "uuid": "00000000-0000-0000-0000-000000000000",
                                   "userUuid": None}, user=self.brand)
            assert "QUERY_TOO_COMPLEX" not in _codes(res), (name, res.errors)

    @pytest.mark.asyncio
    async def test_introspection_blocked_for_brand_allowed_for_admin(self):
        q = "{ __schema { queryType { name } } }"
        res = await run(q, user=self.brand)
        assert _codes(res) == ["INTROSPECTION_DISABLED"]
        # never the FE's "expired token" phrase
        assert "introspection is disabled" not in res.errors[0].message.lower()
        assert _codes(await run('{ __type(name: "Query") { name } }', user=self.brand)) == [
            "INTROSPECTION_DISABLED"
        ]
        ok = await run(q, user=self.admin)
        assert ok.errors is None and ok.data["__schema"]["queryType"]["name"] == "Query"
        # __typename is not introspection
        assert (await run("{ __typename }", user=self.brand)).errors is None

    @pytest.mark.asyncio
    async def test_debug_keeps_introspection(self):
        self.settings.DEBUG = True
        assert (await run("{ __schema { queryType { name } } }", user=self.brand)).errors is None

    def test_batched_operation_lists_rejected(self):
        from django.test import Client

        resp = Client().post(
            PATH, data='[{"query":"{ __typename }"},{"query":"{ __typename }"}]',
            content_type="application/json",
        )
        assert resp.status_code == 400


@pytest.mark.django_db(transaction=True)
class TestPageCap(_Base):
    """Uses the FE's real RequestsQuery (Master Tracker) shape."""

    async def _spy_first(self, variables, user, text=None):
        from events.queries import RequestQueriesService

        seen = []
        original = RequestQueriesService.get_connection

        async def spy(svc, *args, **kwargs):
            seen.append(kwargs.get("first"))
            return await original(svc, *args, **kwargs)

        with mock.patch.object(RequestQueriesService, "get_connection", spy):
            res = await run(text or (FE_OPS / "RequestsQuery.graphql").read_text(),
                            variables, user=user)
        return res, seen

    @pytest.mark.asyncio
    async def test_brand_variable_first_clamped(self):
        res, seen = await self._spy_first({"first": 2000}, self.brand)
        assert res.errors is None, res.errors
        assert seen == [200]

    @pytest.mark.asyncio
    async def test_brand_literal_first_clamped(self):
        res, seen = await self._spy_first(
            None, self.brand, text="query T { requests(first: 1000) { totalCount } }")
        assert res.errors is None, res.errors
        assert seen == [200]

    @pytest.mark.asyncio
    async def test_brand_small_page_untouched(self):
        _, seen = await self._spy_first({"first": 100}, self.brand)
        assert seen == [100]

    @pytest.mark.asyncio
    async def test_admin_not_clamped(self):
        _, seen = await self._spy_first({"first": 2000}, self.admin)
        assert seen == [2000]


@pytest.mark.django_db(transaction=True)
class TestAmbassadorBulkProtection(_Base):
    SEARCH = """query AmbassadorSearchQuery($q: String, $first: Int) {
      activeAmbassadors(q: $q, first: $first) {
        totalCount edges { node { id uuid user { firstName lastName } } }
        pageInfo { hasNextPage endCursor }
      }
    }"""

    @pytest.fixture(autouse=True)
    def _bas(self, _setup):
        from ambassadors.models import Ambassador

        sysu = self.get_system_user()
        self.bas = []
        for i in range(60):
            u = self.create_user(f"ba{i}", f"ba{i}@example.com", self.roles["ambassador"],
                                 first_name=f"Ba{i}", last_name="Test")
            self.bas.append(Ambassador.objects.create(user=u, is_active=True,
                                                      created_by=sysu, updated_by=sysu))

    @pytest.mark.asyncio
    async def test_normal_use_response_unchanged(self):
        self.settings.ABUSE_GUARDRAILS_ENABLED = False
        before = await run(self.SEARCH, {"q": "Ba1", "first": 8}, user=self.brand)
        self.settings.ABUSE_GUARDRAILS_ENABLED = True
        after = await run(self.SEARCH, {"q": "Ba1", "first": 8}, user=self.brand)
        assert before.errors is None and after.errors is None
        assert before.data == after.data

    @pytest.mark.asyncio
    async def test_no_args_is_paginated(self):
        res = await run("{ activeAmbassadors { totalCount edges { node { id } } } }",
                        user=self.admin)
        assert res.data["activeAmbassadors"]["totalCount"] == 60
        assert len(res.data["activeAmbassadors"]["edges"]) == 50

    @pytest.mark.asyncio
    async def test_bulk_walk_throttled_and_logged(self):
        self.settings.ABUSE_AMBASSADOR_PER_MINUTE = 5
        after = None
        for _ in range(5):
            res = await run(self.SEARCH.replace("first: $first", "first: $first, after: $after")
                            .replace("$first: Int)", "$first: Int, $after: String)"),
                            {"first": 10, "after": after}, user=self.brand)
            assert res.errors is None, res.errors
            after = res.data["activeAmbassadors"]["pageInfo"]["endCursor"]
        blocked = await run(self.SEARCH, {"first": 10}, user=self.brand)
        assert _codes(blocked) == ["RATE_LIMITED"]
        distinct = await sync_to_async(
            lambda: AmbassadorReadLog.objects.filter(user_id=self.brand.pk).count())()
        assert distinct == 50
        paged = await sync_to_async(
            lambda: sum(b.paged_count for b in UsageBucket.objects.filter(user_id=self.brand.pk)))()
        assert paged == 4
        reads = await sync_to_async(
            lambda: AuditLogEntry.objects.filter(kind="ambassador_read", user_id=self.brand.pk).count())()
        assert reads == 5
        # admins are not on the ambassador bucket
        for _ in range(8):
            assert (await run(self.SEARCH, {"first": 10}, user=self.admin)).errors is None

    @pytest.mark.asyncio
    async def test_distinct_ambassador_ceiling(self):
        self.settings.ABUSE_AMBASSADOR_DISTINCT_PER_DAY = 15
        assert (await run(self.SEARCH, {"first": 20}, user=self.brand)).errors is None
        assert _codes(await run(self.SEARCH, {"first": 5}, user=self.brand)) == ["RATE_LIMITED"]


@pytest.mark.django_db(transaction=True)
class TestExportsAndBulk(_Base):
    APPROVE = """mutation B($input: BulkApproveRecapsInput!) {
      bulkApproveRecaps(input: $input) { success message }
    }"""
    EXPORT = """mutation E($input: ExportRecapsXlsxInput!) {
      exportRecapsXlsx(input: $input) { success message }
    }"""

    @pytest.mark.asyncio
    async def test_bulk_id_cap_for_brand(self):
        ids = [str(i) for i in range(201)]
        res = await run(self.APPROVE, {"input": {"recapIds": ids}}, user=self.brand)
        assert _codes(res) == ["BULK_LIMIT"]
        assert "max 200" in res.errors[0].message
        ok = await run(self.APPROVE, {"input": {"recapIds": ids[:5]}}, user=self.brand)
        assert "BULK_LIMIT" not in _codes(ok)
        # admins get a higher per-call ceiling
        admin = await run(self.APPROVE, {"input": {"recapIds": ids}}, user=self.admin)
        assert "BULK_LIMIT" not in _codes(admin)
        rows = await sync_to_async(list)(AuditLogEntry.objects.filter(kind="bulk"))
        assert {r.operation for r in rows} == {"bulkApproveRecaps"}
        assert any(r.params.get("input", {}).get("recapIds", {}).get("len") == 5 for r in rows)

    @pytest.mark.asyncio
    async def test_export_rate_limit_and_audit(self):
        self.settings.ABUSE_EXPORTS_PER_HOUR = 2
        variables = {"input": {"tenantId": str(self.tenant.id)}}
        with mock.patch("recaps.mutations.export_recaps_to_xlsx", create=True):
            for _ in range(2):
                res = await run(self.EXPORT, variables, user=self.brand)
                assert "EXPORT_LIMIT" not in _codes(res), res.errors
            blocked = await run(self.EXPORT, variables, user=self.brand)
        assert _codes(blocked) == ["EXPORT_LIMIT"]
        rows = await sync_to_async(list)(
            AuditLogEntry.objects.filter(kind="export", user_id=self.brand.pk))
        assert len(rows) == 2
        assert rows[0].operation == "exportRecapsXlsx"
        assert rows[0].tenant_id == self.tenant.id
        assert rows[0].ip == "127.0.0.1" and rows[0].user_agent.startswith("Mozilla")

    def test_import_row_cap(self, settings):
        from io import BytesIO

        from openpyxl import Workbook

        from events.batch_requests import import_requests_from_excel_bytes
        from events.models import RequestStatus

        wb = Workbook()
        ws = wb.active
        ws.append(["date", "start_time", "end_time", "address", "store_number",
                   "scheduling_status", "timezone_code"])
        for i in range(6):
            ws.append(["2026-12-01", "10:00", "14:00", f"{i} Main St", str(i), "pending",
                       "America/Chicago"])
        buf = BytesIO()
        wb.save(buf)
        RequestStatus.objects.get_or_create(
            slug="approved", tenant=self.tenant,
            defaults={"name": "Approved", "created_by": self.get_system_user()},
        )
        with pytest.raises(ValueError, match="limit is 5 per import"):
            import_requests_from_excel_bytes(
                file_bytes=buf.getvalue(), tenant_id=self.tenant.id,
                created_by_id=self.brand.id, dry_run=True, max_rows=5,
            )


@pytest.mark.django_db(transaction=True)
class TestLoginThrottle(_Base):
    LOGIN = """mutation TokenAuth($input: ObtainJSONWebTokenInput!) {
      tokenAuth(input: $input) { success errors }
    }"""
    MAGIC = """mutation M($input: RequestMagicLinkInput!) {
      requestMagicLink(input: $input) { success message }
    }"""

    @pytest.mark.asyncio
    async def test_per_identifier_same_for_real_and_unknown(self):
        self.settings.ABUSE_LOGIN_IDENTIFIER_PER_15M = 3
        for who in ("brand1", "nobody-here"):
            for i in range(3):
                res = await run(self.LOGIN, {"input": {"username": who, "password": "x"}},
                                xff=f"203.0.113.{i}")
                assert "LOGIN_THROTTLED" not in _codes(res)
            res = await run(self.LOGIN, {"input": {"username": who, "password": "x"}},
                            xff="203.0.113.50")
            # identical response whether or not the account exists
            assert _codes(res) == ["LOGIN_THROTTLED"]
            assert res.errors[0].message.startswith("Too many sign-in attempts")

    @pytest.mark.asyncio
    async def test_per_ip(self):
        self.settings.ABUSE_LOGIN_IP_PER_15M = 4
        with mock.patch("tenants.mutations.MagicLinkMailer", create=True):
            for i in range(4):
                res = await run(self.MAGIC, {"input": {"email": f"u{i}@x.com"}}, xff="198.51.100.20")
                assert "LOGIN_THROTTLED" not in _codes(res)
            res = await run(self.MAGIC, {"input": {"email": "u9@x.com"}}, xff="198.51.100.20")
        assert _codes(res) == ["LOGIN_THROTTLED"]


def test_checkin_throttle_ip_uses_trusted_hop(settings):
    """Check-in throttles key on the same right-most trusted hop as the
    GraphQL limiter and the MCP throttle, not the spoofable first entry."""
    from events.checkin_views import _client_ip

    settings.ABUSE_TRUSTED_PROXY_COUNT = 1
    req = RequestFactory().post("/", HTTP_X_FORWARDED_FOR="6.6.6.6, 203.0.113.44")
    assert _client_ip(req) == "203.0.113.44"
    req = RequestFactory().post("/", HTTP_X_FORWARDED_FOR="7.7.7.7, 203.0.113.44")
    assert _client_ip(req) == "203.0.113.44"
