"""Demo budget pacing, ROI leaderboard, and today's attention queue."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from decimal import Decimal

from django.test import RequestFactory, TransactionTestCase
from django.utils import timezone
from gqlauth.core.middlewares import USER_OR_ERROR_KEY
from gqlauth.models import UserStatus

from brand_insights.attention_queue import build_today_attention_queue
from brand_insights.roi_pacing import (
    compute_budget_pacing,
    compute_roi_leaderboard,
    parse_month,
    upsert_budget_target,
)
from config.schema_client import schema_clients
from events.models import Event, EventType
from pilot.models import (
    BrandCostLine,
    CanonicalVisit,
    FinanceException,
    RetailerSalesImport,
    RetailerSalesRow,
    StoreRecommendationAction,
    WorkspaceMembership,
)
from pilot.permissions import resolve_workspace_access
from pilot.workspaces import upsert_workspace_membership
from tenants.models import Role, Tenant, User
from tenants.tests.base import ensure_role


def _make_user(email: str, role_slug: str = Role.CLIENT_SLUG) -> User:
    role = ensure_role(role_slug.replace("-", " ").title(), slug=role_slug)
    user = User.objects.create_user(
        email=email,
        username=email,
        password="test-password-123",
        first_name=email.split("@")[0].title(),
        last_name="Tester",
        role=role,
    )
    UserStatus.objects.update_or_create(user=user, defaults={"verified": True})
    return user


def _gql(query: str, *, user: User, tenant: Tenant, variables: dict | None = None):
    rf = RequestFactory()
    wsgi_req = rf.post(
        "/api/v615204/graphql/clients",
        HTTP_X_SPARK_TENANT=str(tenant.uuid),
    )
    wsgi_req.user = user
    mock_uoe = type("MockUserOrError", (), {"user": user, "errors": None})()

    class _MockRequest:
        def __init__(self):
            self.wsgi_request = wsgi_req
            self.user = user
            self.META = wsgi_req.META
            self.headers = wsgi_req.headers
            self.scope = {
                "type": "http",
                "method": "POST",
                "path": "/api/v615204/graphql/clients",
                USER_OR_ERROR_KEY: mock_uoe,
            }
            setattr(self, USER_OR_ERROR_KEY, mock_uoe)

    class _Ctx:
        def __init__(self, r):
            self.request = r

        def __getitem__(self, key):
            if key == "request":
                return self.request
            raise KeyError(key)

    return asyncio.run(
        schema_clients.execute(
            query,
            variable_values=variables or {},
            context_value=_Ctx(_MockRequest()),
        )
    )


PACING_Q = """
query ($month: String, $groupBy: String) {
  demoBudgetPacingAndLeaderboard(month: $month, groupBy: $groupBy) {
    month
    targetAmount
    spentAmount
    committedAmount
    projectedAmount
    paceStatus
    paceNote
    canEditBudget
    visitCount
    leaderboard { groupKey groupLabel visitCount demoDollars units unitsPerDollar note }
    targets { uuid scopeType targetAmount }
  }
}
"""

UPSERT = """
mutation ($amount: String!, $month: String) {
  upsertDemoBudgetTarget(targetAmount: $amount, month: $month) {
    ok reason target { uuid targetAmount scopeType }
  }
}
"""

ATTENTION_Q = """
query {
  todayAttentionQueue {
    totalCount
    buckets { key label count items { title actionUrl severity refId } }
  }
}
"""

DISCARD_ATTENTION = """
mutation ($items: [TodayAttentionDiscardItemInput!]!) {
  discardTodayAttentionItems(items: $items) {
    ok reason discardedCount fingerprints
  }
}
"""


class RoiPacingTests(TransactionTestCase):
    def setUp(self):
        self.admin = _make_user("ops@ignite.test", Role.SPARK_ADMIN_SLUG)
        self.brand = Tenant.objects.create(
            name="Pace Brand", slug="pace-brand", created_by=self.admin
        )
        self.other = Tenant.objects.create(
            name="Other Brand", slug="other-brand", created_by=self.admin
        )
        self.brand_admin = _make_user("maya@pace.test")
        staff = resolve_workspace_access(self.admin, tenant=self.brand)
        self.mem = upsert_workspace_membership(
            staff,
            target_user=self.brand_admin,
            workspace_role=WorkspaceMembership.ROLE_BRAND_ADMIN,
        )
        self.today = date(2026, 11, 15)
        self.month = date(2026, 11, 1)
        self.et = EventType.objects.create(
            tenant=self.brand, name="Retail", created_by=self.admin
        )
        self.visit_a = CanonicalVisit.objects.create(
            tenant=self.brand,
            visit_date=date(2026, 11, 5),
            store_key="tw:101",
            store_label="Total Wine 101",
            chain_key="total-wine",
            chain_label="Total Wine",
            market="LA",
            state_code="CA",
            status=CanonicalVisit.STATUS_COMPLETED,
            rep_observed_units=40,
            cached_supported_cost=Decimal("200.00"),
            cached_total_cost=Decimal("200.00"),
            cached_cost_incomplete=False,
            event_type=self.et,
        )
        self.visit_b = CanonicalVisit.objects.create(
            tenant=self.brand,
            visit_date=date(2026, 11, 8),
            store_key="bev:22",
            store_label="BevMo 22",
            chain_key="bevmo",
            chain_label="BevMo",
            market="SD",
            state_code="CA",
            status=CanonicalVisit.STATUS_COMPLETED,
            rep_observed_units=10,
            cached_supported_cost=Decimal("100.00"),
            cached_total_cost=Decimal("100.00"),
            cached_cost_incomplete=False,
            event_type=self.et,
        )
        BrandCostLine.objects.create(
            tenant=self.brand,
            canonical_visit=self.visit_a,
            category=BrandCostLine.CAT_DEMO_FEE,
            amount=Decimal("200.00"),
            settlement_status=BrandCostLine.SETTLEMENT_INVOICED,
            evidence_status=BrandCostLine.EVIDENCE_SUPPORTED,
        )
        BrandCostLine.objects.create(
            tenant=self.brand,
            canonical_visit=self.visit_b,
            category=BrandCostLine.CAT_DEMO_FEE,
            amount=Decimal("100.00"),
            settlement_status=BrandCostLine.SETTLEMENT_INVOICED,
            evidence_status=BrandCostLine.EVIDENCE_SUPPORTED,
        )
        # Other-tenant visit must never leak.
        CanonicalVisit.objects.create(
            tenant=self.other,
            visit_date=date(2026, 11, 5),
            store_key="x:1",
            status=CanonicalVisit.STATUS_COMPLETED,
            rep_observed_units=999,
            cached_supported_cost=Decimal("999.00"),
            cached_total_cost=Decimal("999.00"),
            cached_cost_incomplete=False,
        )

    def _access(self, user=None):
        return resolve_workspace_access(
            user or self.brand_admin, tenant=self.brand
        )

    def test_parse_month(self):
        assert parse_month("2026-11") == date(2026, 11, 1)
        assert parse_month("2026-11-15") == date(2026, 11, 1)

    def test_pacing_under_booked_when_behind_target(self):
        upsert_budget_target(
            self._access(),
            month=self.month,
            target_amount=Decimal("10000.00"),
        )
        pacing = compute_budget_pacing(
            self._access(), month=self.month, today=self.today
        )
        assert pacing.spent_amount == Decimal("300.00")
        assert pacing.target_amount == Decimal("10000.00")
        assert pacing.pace_status == "under_booked"
        assert pacing.can_edit_budget is True

    def test_projected_vs_full_target_not_elapsed_slice(self):
        """Fully booked mid-month is on_track (vs full target), not over_booked."""
        upsert_budget_target(
            self._access(),
            month=self.month,
            target_amount=Decimal("300.00"),
        )
        pacing = compute_budget_pacing(
            self._access(), month=self.month, today=self.today
        )
        assert pacing.projected_amount == Decimal("300.00")
        assert pacing.expected_spend_to_date is not None
        assert pacing.expected_spend_to_date < Decimal("300.00")
        assert pacing.pace_status == "on_track"

    def test_future_incomplete_estimate_not_double_counted(self):
        CanonicalVisit.objects.create(
            tenant=self.brand,
            visit_date=date(2026, 11, 28),
            store_key="tw:999",
            store_label="Future Store",
            chain_key="total-wine",
            chain_label="Total Wine",
            status=CanonicalVisit.STATUS_SCHEDULED,
            cached_supported_cost=Decimal("200.00"),
            cached_total_cost=None,
            cached_cost_incomplete=True,
            event_type=self.et,
        )
        pacing = compute_budget_pacing(
            self._access(), month=self.month, today=self.today
        )
        # Past visits $300 spent; future incomplete $200 committed once.
        assert pacing.spent_amount == Decimal("300.00")
        assert pacing.committed_amount == Decimal("200.00")
        assert pacing.projected_amount == Decimal("500.00")

    def test_leaderboard_ranks_by_units_per_dollar(self):
        rows = compute_roi_leaderboard(
            self._access(),
            month=self.month,
            group_by="retailer",
            today=self.today,
        )
        assert len(rows) == 2
        assert rows[0].group_key == "total-wine"
        assert rows[0].units_per_dollar == Decimal("0.20")  # 40/200
        assert rows[1].group_key == "bevmo"
        assert all("999" not in (r.group_key + r.group_label) for r in rows)

    def test_leaderboard_falls_back_to_rep_units_when_pos_units_absent(self):
        imp = RetailerSalesImport.objects.create(
            tenant=self.brand,
            retailer_banner="Total Wine",
            owner=self.admin,
            period_start=date(2026, 11, 1),
            period_end=date(2026, 11, 30),
            filename="pos.csv",
            file_sha256="a" * 64,
            total_rows=1,
            matched_rows=1,
        )
        # Sales dollars present, register_units null → use rep_observed_units.
        RetailerSalesRow.objects.create(
            sales_import=imp,
            tenant=self.brand,
            row_number=1,
            store_key="tw:101",
            store_label="Total Wine 101",
            chain_key="total-wine",
            week_start=date(2026, 11, 3),
            week_end=date(2026, 11, 9),
            register_units=None,
            sales_dollars=Decimal("500.00"),
            match_status=RetailerSalesRow.MATCH_MATCHED,
        )
        rows = compute_roi_leaderboard(
            self._access(),
            month=self.month,
            group_by="retailer",
            today=self.today,
        )
        tw = next(r for r in rows if r.group_key == "total-wine")
        assert tw.units == 40  # rep-observed, not POS zero
        assert tw.sales_dollars == Decimal("500.00")
        assert tw.units_per_dollar == Decimal("0.20")

    def test_graphql_pacing_and_upsert_isolation(self):
        res = _gql(
            UPSERT,
            user=self.brand_admin,
            tenant=self.brand,
            variables={"amount": "5000.00", "month": "2026-11"},
        )
        assert res.errors is None
        assert res.data["upsertDemoBudgetTarget"]["ok"] is True

        res2 = _gql(
            PACING_Q,
            user=self.brand_admin,
            tenant=self.brand,
            variables={"month": "2026-11", "groupBy": "retailer"},
        )
        assert res2.errors is None
        data = res2.data["demoBudgetPacingAndLeaderboard"]
        assert data["targetAmount"] == "5000.00"
        assert data["spentAmount"] == "300.00"
        assert data["canEditBudget"] is True
        assert len(data["leaderboard"]) == 2

        # Other brand admin cannot see Pace Brand targets via their tenant header.
        other_admin = _make_user("other@brand.test")
        staff_o = resolve_workspace_access(self.admin, tenant=self.other)
        upsert_workspace_membership(
            staff_o,
            target_user=other_admin,
            workspace_role=WorkspaceMembership.ROLE_BRAND_ADMIN,
        )
        res3 = _gql(
            PACING_Q,
            user=other_admin,
            tenant=self.other,
            variables={"month": "2026-11"},
        )
        assert res3.errors is None
        other_data = res3.data["demoBudgetPacingAndLeaderboard"]
        # Own-tenant spend only — Pace Brand's $300 / Total Wine must not leak.
        assert other_data["spentAmount"] == "999.00"
        assert other_data["targets"] == []
        assert all(
            "total-wine" not in (row["groupKey"] or "")
            for row in other_data["leaderboard"]
        )


class AttentionQueueTests(TransactionTestCase):
    def setUp(self):
        self.admin = _make_user("ops2@ignite.test", Role.SPARK_ADMIN_SLUG)
        self.brand = Tenant.objects.create(
            name="Attention Brand", slug="attention-brand", created_by=self.admin
        )
        self.brand_admin = _make_user("ops@attention.test")
        staff = resolve_workspace_access(self.admin, tenant=self.brand)
        upsert_workspace_membership(
            staff,
            target_user=self.brand_admin,
            workspace_role=WorkspaceMembership.ROLE_BRAND_ADMIN,
        )
        self.et = EventType.objects.create(
            tenant=self.brand, name="Retail", created_by=self.admin
        )
        now = timezone.now()
        self.event = Event.objects.create(
            tenant=self.brand,
            name="Unstaffed Demo",
            event_type=self.et,
            start_time=now + timedelta(hours=12),
            end_time=now + timedelta(hours=16),
            created_by=self.admin,
        )
        StoreRecommendationAction.objects.create(
            tenant=self.brand,
            finding_id_snapshot="f1",
            finding_code="top_store",
            finding_scope_key="store:tw:1",
            store_key="tw:1",
            store_label="Total Wine 1",
            action_type=StoreRecommendationAction.ACTION_REBOOK,
            idempotency_key="rebook-tw-1",
            status=StoreRecommendationAction.STATUS_ASSIGNED,
        )
        FinanceException.objects.create(
            tenant=self.brand,
            exception_type=FinanceException.TYPE_MISSING_PROOF,
            severity=FinanceException.SEV_HIGH,
            explanation="Receipt photo unreadable",
            dedupe_key="missing-proof-1",
        )

    def test_attention_buckets_and_graphql(self):
        access = resolve_workspace_access(self.brand_admin, tenant=self.brand)
        queue = build_today_attention_queue(access)
        keys = {b.key: b for b in queue.buckets}
        assert keys["receipt_checks"].count >= 1
        assert keys["unstaffed"].count >= 1
        assert keys["rebooks"].count >= 1

        res = _gql(ATTENTION_Q, user=self.brand_admin, tenant=self.brand)
        assert res.errors is None
        assert res.data["todayAttentionQueue"] is None

        res_staff = _gql(ATTENTION_Q, user=self.admin, tenant=self.brand)
        assert res_staff.errors is None
        data = res_staff.data["todayAttentionQueue"]
        assert data is not None
        assert data["totalCount"] >= 3
        by_key = {b["key"]: b for b in data["buckets"]}
        assert by_key["rebooks"]["items"][0]["actionUrl"] == "/buyer-report"
        assert by_key["receipt_checks"]["items"][0]["actionUrl"] == "/finance-queue"

    def test_discard_is_shared_across_admins(self):
        """Discarded notices stay gone for every staff user on refresh."""
        from brand_insights.attention_dismissals import fingerprint
        from tenants.models import Role

        other_admin = _make_user("ops3@ignite.test", Role.SPARK_ADMIN_SLUG)

        res_staff = _gql(ATTENTION_Q, user=self.admin, tenant=self.brand)
        data = res_staff.data["todayAttentionQueue"]
        by_key = {b["key"]: b for b in data["buckets"]}
        receipt = by_key["receipt_checks"]["items"][0]
        unstaffed = by_key["unstaffed"]["items"][0]
        before_total = data["totalCount"]

        # Brand clients cannot discard.
        denied = _gql(
            DISCARD_ATTENTION,
            user=self.brand_admin,
            tenant=self.brand,
            variables={
                "items": [{"bucket": "receipt_checks", "refId": receipt["refId"]}]
            },
        )
        assert denied.errors is None
        assert denied.data["discardTodayAttentionItems"]["ok"] is False

        discarded = _gql(
            DISCARD_ATTENTION,
            user=self.admin,
            tenant=self.brand,
            variables={
                "items": [
                    {"bucket": "receipt_checks", "refId": receipt["refId"]},
                    {"bucket": "unstaffed", "refId": unstaffed["refId"]},
                ]
            },
        )
        assert discarded.errors is None
        payload = discarded.data["discardTodayAttentionItems"]
        assert payload["ok"] is True
        assert payload["discardedCount"] == 2
        fps = set(payload["fingerprints"])
        assert fingerprint("receipt_checks", receipt["refId"]) in fps
        assert fingerprint("unstaffed", unstaffed["refId"]) in fps

        # Same admin: items gone, counts drop.
        after = _gql(ATTENTION_Q, user=self.admin, tenant=self.brand)
        after_data = after.data["todayAttentionQueue"]
        assert after_data["totalCount"] == before_total - 2
        after_by = {b["key"]: b for b in after_data["buckets"]}
        receipt_refs = {i["refId"] for i in after_by["receipt_checks"]["items"]}
        unstaffed_refs = {i["refId"] for i in after_by["unstaffed"]["items"]}
        assert receipt["refId"] not in receipt_refs
        assert unstaffed["refId"] not in unstaffed_refs

        # Another Ignite admin sees the same shared dismissals.
        peer = _gql(ATTENTION_Q, user=other_admin, tenant=self.brand)
        peer_data = peer.data["todayAttentionQueue"]
        assert peer_data["totalCount"] == before_total - 2
        peer_by = {b["key"]: b for b in peer_data["buckets"]}
        assert receipt["refId"] not in {
            i["refId"] for i in peer_by["receipt_checks"]["items"]
        }
        assert unstaffed["refId"] not in {
            i["refId"] for i in peer_by["unstaffed"]["items"]
        }
