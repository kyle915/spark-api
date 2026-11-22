"""Feature 1 & security tests: Brand-owned workspaces, internal & agency
permissions, multi-membership context switching, scoped mobile submission
links, immediate revocation across portal/mobile/exports/shares/AI, legacy
managed client compatibility, and internal worker-pay non-leakage."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, time

from django.test import (
    Client as HttpClient,
    RequestFactory,
    TransactionTestCase,
    override_settings,
)
from django.utils import timezone
from gqlauth.core.middlewares import USER_OR_ERROR_KEY
from gqlauth.models import UserStatus

from brand_chat.service import resolve_tenant as resolve_brand_chat_tenant
from config.schema_client import schema_clients
from events.models import Event, EventType, Product, ProductType
from pilot.models import (
    CanonicalVisit,
    ProviderAssignment,
    WorkspaceMembership,
)
from pilot.permissions import resolve_workspace_access
from pilot.workspaces import (
    assign_provider_work,
    create_or_update_provider,
    create_workspace_shared_link,
    invite_workspace_member,
    revoke_provider_from_workspace,
    revoke_workspace_membership,
    switch_workspace_context,
    upsert_workspace_membership,
)
from spark_mcp.access import eligible_tenants as mcp_eligible_tenants
from tenants.models import Role, Tenant, TenantedUser, User
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
    UserStatus.objects.update_or_create(
        user=user, defaults={"verified": True}
    )
    return user


def _gql_exec(
    query: str,
    *,
    user: User,
    tenant: Tenant | None = None,
    membership: WorkspaceMembership | None = None,
    variables: dict | None = None,
):
    rf = RequestFactory()
    headers = {}
    if tenant is not None:
        headers["HTTP_X_SPARK_TENANT"] = str(tenant.uuid)
    if membership is not None:
        headers["HTTP_X_SPARK_MEMBERSHIP"] = str(membership.uuid)
    wsgi_req = rf.post("/api/v615204/graphql/clients", **headers)
    wsgi_req.user = user
    mock_uoe = type("MockUserOrError", (), {"user": user, "errors": None})()

    class _MockRequest:
        def __init__(self):
            self.wsgi_request = wsgi_req
            self.user = user
            self.META = wsgi_req.META
            self.scope = {
                "type": "http",
                "method": "POST",
                "path": "/api/v615204/graphql/clients",
                USER_OR_ERROR_KEY: mock_uoe,
            }
            setattr(self, USER_OR_ERROR_KEY, mock_uoe)

    req = _MockRequest()

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
            context_value=_Ctx(req),
        )
    )


class WorkspaceIsolationAndSecurityTests(TransactionTestCase):
    def setUp(self):
        self.ignite_admin = _make_user(
            "ops-admin@suncrest-internal.example", Role.SPARK_ADMIN_SLUG
        )
        self.brand_a = Tenant.objects.create(
            name="Suncrest Sparkling",
            slug="suncrest-sparkling",
            created_by=self.ignite_admin,
        )
        self.brand_b = Tenant.objects.create(
            name="Cascade Cold Brew",
            slug="cascade-cold-brew",
            created_by=self.ignite_admin,
        )

        self.brand_a_admin = _make_user("maya@suncrest.example")
        self.brand_a_finance = _make_user("fin@suncrest.example")
        self.brand_a_viewer = _make_user("exec-viewer@suncrest.example")
        self.brand_b_admin = _make_user("dana@cascade.example")
        self.agency_1_mgr = _make_user("pm@fieldpulse-agency.example")
        self.agency_2_mgr = _make_user("pm@promoworks-agency.example")
        self.legacy_client = _make_user("legacy@cascade.example")
        TenantedUser.objects.create(
            tenant=self.brand_b, user=self.legacy_client, is_active=True
        )

        # Bootstrap Brand A & Brand B admins via Ignite staff access.
        staff_a = resolve_workspace_access(self.ignite_admin, tenant=self.brand_a)
        staff_b = resolve_workspace_access(self.ignite_admin, tenant=self.brand_b)

        self.mem_a_admin = upsert_workspace_membership(
            staff_a,
            target_user=self.brand_a_admin,
            workspace_role=WorkspaceMembership.ROLE_BRAND_ADMIN,
        )
        self.mem_b_admin = upsert_workspace_membership(
            staff_b,
            target_user=self.brand_b_admin,
            workspace_role=WorkspaceMembership.ROLE_BRAND_ADMIN,
        )

        # Now Brand A admin manages Brand A's own workspace!
        access_a = resolve_workspace_access(
            self.brand_a_admin, tenant=self.brand_a
        )
        self.agency_1 = create_or_update_provider(
            access_a,
            name="FieldPulse Staffing",
            contact_email="ops@fieldpulse-agency.example",
        )
        self.agency_2 = create_or_update_provider(
            access_a,
            name="PromoWorks Experiential",
            contact_email="ops@promoworks-agency.example",
        )
        self.mem_a_finance = upsert_workspace_membership(
            access_a,
            target_user=self.brand_a_finance,
            workspace_role=WorkspaceMembership.ROLE_FINANCE_REVIEWER,
            can_approve_finance=True,
        )
        self.mem_a_viewer = upsert_workspace_membership(
            access_a,
            target_user=self.brand_a_viewer,
            workspace_role=WorkspaceMembership.ROLE_VIEWER,
        )
        invite_workspace_member(
            access_a,
            email=self.agency_1_mgr.email,
            workspace_role=WorkspaceMembership.ROLE_PROVIDER_MANAGER,
            provider=self.agency_1,
        )
        invite_workspace_member(
            access_a,
            email=self.agency_2_mgr.email,
            workspace_role=WorkspaceMembership.ROLE_PROVIDER_MANAGER,
            provider=self.agency_2,
        )
        self.mem_ag1_on_a = WorkspaceMembership.objects.get(
            tenant=self.brand_a,
            user=self.agency_1_mgr,
            provider=self.agency_1,
        )
        self.mem_ag2_on_a = WorkspaceMembership.objects.get(
            tenant=self.brand_a,
            user=self.agency_2_mgr,
            provider=self.agency_2,
        )

        # Also grant Agency 1 a provider membership on Brand B to test
        # multi-membership context switching!
        access_b = resolve_workspace_access(
            self.brand_b_admin, tenant=self.brand_b
        )
        invite_workspace_member(
            access_b,
            email=self.agency_1_mgr.email,
            workspace_role=WorkspaceMembership.ROLE_PROVIDER_MANAGER,
            provider=self.agency_1,
        )
        self.mem_ag1_on_b = WorkspaceMembership.objects.get(
            tenant=self.brand_b,
            user=self.agency_1_mgr,
            provider=self.agency_1,
        )

        # Seed programs, products, and canonical visits on Brand A & Brand B.
        pt_a = ProductType.objects.create(
            tenant=self.brand_a, name="Beverage", created_by=self.brand_a_admin
        )
        self.prod_a = Product.objects.create(
            tenant=self.brand_a,
            product_type=pt_a,
            name="Suncrest Yuzu Lime",
            created_by=self.brand_a_admin,
        )
        self.prog_a_retail = EventType.objects.create(
            tenant=self.brand_a,
            name="Retail Sampling",
            slug="retail-sampling",
            is_default=True,
            created_by=self.brand_a_admin,
        )
        self.prog_a_vip = EventType.objects.create(
            tenant=self.brand_a,
            name="VIP Event",
            slug="vip-event",
            created_by=self.brand_a_admin,
        )

        self.visit_a1 = CanonicalVisit.objects.create(
            tenant=self.brand_a,
            primary_provider=self.agency_1,
            event_type=self.prog_a_retail,
            store_key="freshmart-101-austin",
            store_label="FreshMart #101",
            chain_key="freshmart",
            chain_label="FreshMart",
            visit_date=date(2026, 10, 12),
            status=CanonicalVisit.STATUS_SCHEDULED,
        )
        self.visit_a2 = CanonicalVisit.objects.create(
            tenant=self.brand_a,
            primary_provider=self.agency_2,
            event_type=self.prog_a_retail,
            store_key="freshmart-202-dallas",
            store_label="FreshMart #202",
            chain_key="freshmart",
            chain_label="FreshMart",
            visit_date=date(2026, 10, 13),
            status=CanonicalVisit.STATUS_COMPLETED,
            samples_distributed=95,
            rep_observed_units=22,
        )
        self.visit_b1 = CanonicalVisit.objects.create(
            tenant=self.brand_b,
            store_key="harbormarket-9-seattle",
            store_label="Harbor Market #9",
            chain_key="harbormarket",
            chain_label="Harbor Market",
            visit_date=date(2026, 10, 14),
            status=CanonicalVisit.STATUS_COMPLETED,
            samples_distributed=110,
            rep_observed_units=30,
        )

        # Assign ONLY visit_a1 to Agency 1 on Brand A.
        self.assign_ag1 = assign_provider_work(
            access_a,
            provider=self.agency_1,
            scope_type=ProviderAssignment.SCOPE_VISIT,
            event_type=self.prog_a_retail,
            canonical_visit=self.visit_a1,
        )

    def test_cross_brand_and_cross_provider_isolation(self):
        # 1. Brand A admin cannot query Brand B workspace overview or visits.
        res_cross = _gql_exec(
            """
            query($tid: ID) {
              workspaceOverview(tenantId: $tid) { tenantName }
            }
            """,
            user=self.brand_a_admin,
            tenant=self.brand_b,
        )
        self.assertIsNotNone(res_cross.errors)

        # 2. Agency 1 on Brand A sees ONLY visit_a1, NEVER Agency 2's visit_a2!
        res_ag1_visits = _gql_exec(
            """
            query {
              canonicalVisits {
                uuid
                storeLabel
                totalCost
              }
            }
            """,
            user=self.agency_1_mgr,
            tenant=self.brand_a,
            membership=self.mem_ag1_on_a,
        )
        self.assertIsNone(res_ag1_visits.errors)
        visits = res_ag1_visits.data["canonicalVisits"]
        self.assertEqual(len(visits), 1)
        self.assertEqual(visits[0]["uuid"], str(self.visit_a1.uuid))
        self.assertIsNone(visits[0]["totalCost"])

        # 3. Agency 1 is blocked from consolidated brand cost, finance queue, POS, and buyer report.
        for blocked_query in [
            "query { brandCostDashboard { groupBy } }",
            "query { financeExceptionQueue { exceptions { uuid } } }",
            "query { retailerPosDashboard { available } }",
            "query { weeklyBuyerStoreReport { totalVisits } }",
        ]:
            res = _gql_exec(
                blocked_query,
                user=self.agency_1_mgr,
                tenant=self.brand_a,
                membership=self.mem_ag1_on_a,
            )
            self.assertIsNotNone(res.errors, f"Expected error for {blocked_query}")

        # 4. Read-only viewer can view consolidated visits/reports, but cannot invite or edit costs.
        res_viewer_ok = _gql_exec(
            'query { weeklyBuyerStoreReport(periodStart: "2026-10-01", periodEnd: "2026-10-31") { totalVisits } }',
            user=self.brand_a_viewer,
            tenant=self.brand_a,
            membership=self.mem_a_viewer,
        )
        self.assertIsNone(res_viewer_ok.errors)
        self.assertEqual(
            res_viewer_ok.data["weeklyBuyerStoreReport"]["totalVisits"], 1
        )

        res_viewer_mut = _gql_exec(
            """
            mutation {
              registerProviderOrganization(name: "Unauthorized Agency") {
                ok
                error
              }
            }
            """,
            user=self.brand_a_viewer,
            tenant=self.brand_a,
            membership=self.mem_a_viewer,
        )
        self.assertFalse(
            res_viewer_mut.data["registerProviderOrganization"]["ok"]
        )

    def test_multi_membership_explicit_context_switching(self):
        # Agency 1 manager belongs to BOTH Brand A and Brand B as provider_manager.
        res_ctxs = _gql_exec(
            """
            query {
              myWorkspaceContexts {
                membershipUuid
                tenantUuid
                tenantName
                workspaceRole
                providerName
                isCurrent
              }
            }
            """,
            user=self.agency_1_mgr,
        )
        self.assertIsNone(res_ctxs.errors)
        ctxs = res_ctxs.data["myWorkspaceContexts"]
        self.assertEqual(len(ctxs), 2)

        # Switch explicitly to Brand B membership; subsequent request with no
        # X-Spark-Tenant header resolves cleanly to Brand B!
        switch_workspace_context(
            user=self.agency_1_mgr, membership_uuid=str(self.mem_ag1_on_b.uuid)
        )
        res_b = _gql_exec(
            "query { workspaceOverview { tenantName viewerRole viewerProviderName } }",
            user=self.agency_1_mgr,
        )
        self.assertIsNone(res_b.errors)
        self.assertEqual(
            res_b.data["workspaceOverview"]["tenantName"], "Cascade Cold Brew"
        )
        self.assertEqual(
            res_b.data["workspaceOverview"]["viewerRole"], "provider_manager"
        )

        # Switch back to Brand A membership.
        switch_workspace_context(
            user=self.agency_1_mgr, membership_uuid=str(self.mem_ag1_on_a.uuid)
        )
        res_a = _gql_exec(
            "query { workspaceOverview { tenantName viewerProviderName } }",
            user=self.agency_1_mgr,
        )
        self.assertIsNone(res_a.errors)
        self.assertEqual(
            res_a.data["workspaceOverview"]["tenantName"], "Suncrest Sparkling"
        )

    def test_scoped_mobile_submission_and_immediate_revocation_across_all_surfaces(self):
        http = HttpClient()
        code = self.assign_ag1.submission_code

        # 1. GET mobile submission context succeeds for assigned visit.
        resp_ctx = http.get(f"/api/public/provider-submit/{code}")
        self.assertEqual(resp_ctx.status_code, 200)
        self.assertEqual(resp_ctx.json()["visitUuid"], str(self.visit_a1.uuid))
        self.assertTrue(resp_ctx.json()["allowMultiplePhotos"])

        # 2. Attempting to submit against Agency 2's visit_a2 is REJECTED (403)!
        resp_wrong_visit = http.post(
            f"/api/public/provider-submit/{code}",
            data=json.dumps(
                {
                    "visitUuid": str(self.visit_a2.uuid),
                    "samplesDistributed": 50,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(resp_wrong_visit.status_code, 403)

        # 3. Attempting to submit against unassigned VIP program is REJECTED (403)!
        resp_wrong_prog = http.post(
            f"/api/public/provider-submit/{code}",
            data=json.dumps(
                {
                    "visitUuid": str(self.visit_a1.uuid),
                    "eventTypeId": self.prog_a_vip.id,
                    "samplesDistributed": 50,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(resp_wrong_prog.status_code, 403)

        # 4. Submitting for the assigned visit with multiple photos & receipt succeeds!
        resp_ok = http.post(
            f"/api/public/provider-submit/{code}",
            data=json.dumps(
                {
                    "visitUuid": str(self.visit_a1.uuid),
                    "samplesDistributed": 84,
                    "consumersSampled": 70,
                    "repObservedUnits": 19,
                    "productSpend": "36.50",
                    "photos": [
                        "https://cdn.example/table-1.jpg",
                        "https://cdn.example/sampling-2.jpg",
                    ],
                    "receipts": ["https://cdn.example/receipt-1.jpg"],
                    "notes": "Strong afternoon traffic.",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(resp_ok.status_code, 200)
        self.assertEqual(resp_ok.json()["status"], CanonicalVisit.STATUS_NEEDS_REVIEW)
        self.assertEqual(resp_ok.json()["photoCount"], 2)

        # 5. Outside provider is NEVER allowed into Spark MCP or Brand Chat!
        self.assertFalse(
            mcp_eligible_tenants(self.agency_1_mgr)
            .filter(id=self.brand_a.id)
            .exists()
        )
        self.assertIsNone(
            resolve_brand_chat_tenant(self.agency_1_mgr, str(self.brand_a.uuid))
        )

        # 6. Finance reviewer creates a shared link, has MCP + Brand Chat access,
        # then gets REVOKED -> next request fails across portal, export, shared
        # link, MCP, and Brand Chat!
        fin_access = resolve_workspace_access(
            self.brand_a_finance,
            tenant=self.brand_a,
            request=type("R", (), {"META": {}})(),
        )
        shared_link = create_workspace_shared_link(
            fin_access, visit_uuid=str(self.visit_a2.uuid)
        )
        self.assertEqual(
            http.get(f"/api/public/workspace-share/{shared_link.token}").status_code,
            200,
        )
        with override_settings(MCP_ALLOWED_TENANT_IDS="all"):
            self.assertTrue(
                mcp_eligible_tenants(self.brand_a_finance)
                .filter(id=self.brand_a.id)
                .exists()
            )
        self.assertIsNotNone(
            resolve_brand_chat_tenant(
                self.brand_a_finance, str(self.brand_a.uuid)
            )
        )

        admin_access = resolve_workspace_access(
            self.brand_a_admin, tenant=self.brand_a
        )
        revoke_workspace_membership(
            admin_access, membership_uuid=str(self.mem_a_finance.uuid)
        )

        # Next portal GraphQL request -> denied
        res_fin_after = _gql_exec(
            "query { workspaceOverview { tenantName } }",
            user=self.brand_a_finance,
            tenant=self.brand_a,
        )
        self.assertIsNotNone(res_fin_after.errors)

        # Next export GraphQL request -> denied
        res_exp_after = _gql_exec(
            'query { exportWeeklyBuyerReport(format: "csv") { content } }',
            user=self.brand_a_finance,
            tenant=self.brand_a,
        )
        self.assertIsNotNone(res_exp_after.errors)

        # Next shared link request -> 403 revoked!
        self.assertEqual(
            http.get(f"/api/public/workspace-share/{shared_link.token}").status_code,
            403,
        )

        # Next MCP & Brand Chat check -> denied!
        with override_settings(MCP_ALLOWED_TENANT_IDS="all"):
            self.assertFalse(
                mcp_eligible_tenants(self.brand_a_finance)
                .filter(id=self.brand_a.id)
                .exists()
            )
        self.assertIsNone(
            resolve_brand_chat_tenant(
                self.brand_a_finance, str(self.brand_a.uuid)
            )
        )

        # 7. Revoke Agency 1 from Brand A -> mobile submission link immediately 403s!
        revoke_provider_from_workspace(
            admin_access, provider_uuid=str(self.agency_1.uuid)
        )
        self.assertEqual(
            http.get(f"/api/public/provider-submit/{code}").status_code, 403
        )

    def test_legacy_managed_customer_and_worker_pay_non_leakage(self):
        # Legacy Ignite-managed customer (TenantedUser only, zero WorkspaceMembership rows)
        # continues working cleanly without regression!
        res_legacy = _gql_exec(
            "query { workspaceOverview { tenantName mode canViewConsolidated } }",
            user=self.legacy_client,
            tenant=self.brand_b,
        )
        self.assertIsNone(res_legacy.errors)
        self.assertEqual(
            res_legacy.data["workspaceOverview"]["tenantName"],
            "Cascade Cold Brew",
        )
        self.assertEqual(
            res_legacy.data["workspaceOverview"]["mode"], "managed"
        )
        self.assertTrue(
            res_legacy.data["workspaceOverview"]["canViewConsolidated"]
        )

        # Verify internal BA labor pay (`campaignCost`) returns empty rows for
        # brand admins / clients and never exposes ambassador hourly pay!
        dt = timezone.make_aware(
            datetime.combine(date(2026, 10, 10), time(12, 0)),
            timezone.get_current_timezone(),
        )
        Event.objects.create(
            tenant=self.brand_a,
            name="Internal Labor Check Event",
            date=dt,
            start_time=dt,
            end_time=dt,
            address="100 Main St",
            created_by=self.ignite_admin,
        )
        res_pnl = _gql_exec(
            """
            query {
              campaignCost(startDate: "2026-10-01", endDate: "2026-10-31") {
                rows { laborCost totalCost }
              }
            }
            """,
            user=self.brand_a_admin,
            tenant=self.brand_a,
        )
        self.assertIsNone(res_pnl.errors)
        self.assertEqual(res_pnl.data["campaignCost"]["rows"], [])

    def test_invitation_token_cannot_be_replayed_after_acceptance_or_revocation(self):
        from pilot.workspaces import (
            accept_workspace_invitation,
            invite_workspace_member,
        )

        admin_access = resolve_workspace_access(
            self.brand_a_admin, tenant=self.brand_a
        )
        # Invite BEFORE the user account exists so invitation starts in PENDING status.
        inv = invite_workspace_member(
            admin_access,
            email="replay-check@aurora.test",
            workspace_role=WorkspaceMembership.ROLE_BRAND_REVIEWER,
        )
        self.assertEqual(inv.status, inv.STATUS_PENDING)
        invitee = _make_user("replay-check@aurora.test")
        mem = accept_workspace_invitation(user=invitee, token=inv.token)
        self.assertEqual(mem.status, WorkspaceMembership.STATUS_ACTIVE)

        # Replaying an already-accepted token is rejected immediately.
        with self.assertRaises(PermissionError):
            accept_workspace_invitation(user=invitee, token=inv.token)

        # Even after revoking the membership, replaying the old invite token cannot restore access.
        revoke_workspace_membership(
            admin_access, membership_uuid=str(mem.uuid)
        )
        with self.assertRaises(PermissionError):
            accept_workspace_invitation(user=invitee, token=inv.token)
        mem.refresh_from_db()
        self.assertEqual(mem.status, WorkspaceMembership.STATUS_REVOKED)

    def test_brand_admin_cannot_overwrite_or_reactivate_shared_provider_org(self):
        from pilot.workspaces import create_or_update_provider

        access_b = resolve_workspace_access(
            self.brand_b_admin, tenant=self.brand_b
        )
        # Brand B registers `fieldpulse-agency` with a hostile name/email;
        # Brand A's shared ProviderOrganization identity must remain untouched.
        reused = create_or_update_provider(
            access_b,
            name="Hijacked Agency Name",
            contact_email="hijack@bad.test",
            slug=self.agency_1.slug,
        )
        self.assertEqual(reused.id, self.agency_1.id)
        self.agency_1.refresh_from_db()
        self.assertEqual(self.agency_1.name, "FieldPulse Staffing")
        self.assertNotEqual(self.agency_1.contact_email, "hijack@bad.test")

        # Globally disabled provider cannot be reactivated by a brand workspace admin.
        self.agency_2.is_active = False
        self.agency_2.save(update_fields=["is_active"])
        with self.assertRaises(PermissionError):
            create_or_update_provider(
                access_b,
                name="Reactivate Attempt",
                slug=self.agency_2.slug,
            )
        self.agency_2.refresh_from_db()
        self.assertFalse(self.agency_2.is_active)

    def test_visit_scoped_pa_checkin_code_without_legacy_event_stays_visit_bound(self):
        from ambassadors import checkin_web

        admin_access = resolve_workspace_access(
            self.brand_a_admin, tenant=self.brand_a
        )
        # Confirm visit_a1 starts with no legacy Event (`event_id is None`).
        self.assertIsNone(self.visit_a1.event_id)
        assignment = assign_provider_work(
            admin_access,
            provider=self.agency_1,
            scope_type=ProviderAssignment.SCOPE_VISIT,
            canonical_visit=self.visit_a1,
        )
        kind, target = checkin_web.resolve_checkin_target(
            assignment.submission_code
        )
        # Must bind to a materialized visit-scoped Event, NEVER fall back to "tenant"!
        self.assertEqual(kind, "event")
        self.visit_a1.refresh_from_db()
        assignment.refresh_from_db()
        self.assertIsNotNone(self.visit_a1.event_id)
        self.assertEqual(target.id, self.visit_a1.event_id)
        self.assertEqual(assignment.event_id, self.visit_a1.event_id)

        from django.urls import reverse

        # Public check-in context is bound to that exact event and stamped recapOnly.
        resp = self.client.get(
            reverse(
                "events.public_checkin_context",
                kwargs={"code": assignment.submission_code},
            )
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body.get("recapOnly"))
        self.assertIn("FieldPulse Staffing", body.get("sourceLabel", ""))

    def test_import_correction_moves_canonical_visit_and_clears_removed_cost_lines(self):
        from decimal import Decimal

        from events.models import Retailer
        from pilot.finance import evaluate_finance_exceptions
        from pilot.imports import (
            brand_included_visits_queryset,
            commit_provider_import,
        )
        from pilot.models import BrandCostLine

        Retailer.objects.get_or_create(
            tenant=self.brand_a,
            name="FreshMart",
            defaults={"created_by": self.ignite_admin},
        )
        admin_access = resolve_workspace_access(
            self.brand_a_admin, tenant=self.brand_a
        )
        csv_initial = (
            "shift_ref,shift_date,account_name,location_code,street,town,region,samples_out,shopper_count,bottles_sold,agency_invoice_amt,reimbursable_spend\n"
            "EV-CORR-99,2026-10-13,FreshMart,10,100 Congress Ave,Austin,TX,80,65,15,240.00,52.50\n"
        )
        job_1 = commit_provider_import(
            admin_access,
            provider_uuid=str(self.agency_1.uuid),
            csv_text=csv_initial,
            preset_key="agency_promoworks",
        )
        self.assertEqual(job_1.created_count, 1)
        row_1 = job_1.rows.first()
        wrong_visit = row_1.canonical_visit
        wrong_visit.refresh_from_db()
        self.assertEqual(wrong_visit.cached_total_cost, Decimal("292.50"))
        evaluate_finance_exceptions(self.brand_a, visit=wrong_visit)

        # Provider re-imports EV-CORR-99 correcting BOTH the store/date AND clearing the erroneous reimbursable_spend!
        csv_corrected = (
            "shift_ref,shift_date,account_name,location_code,street,town,region,samples_out,shopper_count,bottles_sold,agency_invoice_amt,reimbursable_spend\n"
            "EV-CORR-99,2026-10-14,FreshMart,22,200 Main St,Dallas,TX,95,78,21,240.00,\n"
        )
        job_2 = commit_provider_import(
            admin_access,
            provider_uuid=str(self.agency_1.uuid),
            csv_text=csv_corrected,
            preset_key="agency_promoworks",
        )
        self.assertEqual(job_2.updated_count, 1)
        row_2 = job_2.rows.first()
        corrected_visit = row_2.canonical_visit
        self.assertNotEqual(corrected_visit.id, wrong_visit.id)
        self.assertEqual(corrected_visit.visit_date, date(2026, 10, 14))
        self.assertIn("22", corrected_visit.store_label)
        self.assertEqual(corrected_visit.samples_distributed, 95)

        # Cleared product_spend line is removed; demo_fee line moved to corrected_visit!
        corrected_visit.refresh_from_db()
        self.assertEqual(corrected_visit.cached_total_cost, Decimal("240.00"))
        self.assertEqual(
            BrandCostLine.objects.filter(canonical_visit=corrected_visit).count(),
            1,
        )
        self.assertEqual(
            BrandCostLine.objects.filter(canonical_visit=wrong_visit).count(),
            0,
        )

        # Vacated wrong_visit is cancelled and excluded from brand reporting!
        wrong_visit.refresh_from_db()
        self.assertEqual(wrong_visit.status, CanonicalVisit.STATUS_CANCELLED)
        self.assertTrue(wrong_visit.exclude_from_aggregates)
        self.assertFalse(
            brand_included_visits_queryset(self.brand_a)
            .filter(id=wrong_visit.id)
            .exists()
        )
        self.assertTrue(
            brand_included_visits_queryset(self.brand_a)
            .filter(id=corrected_visit.id)
            .exists()
        )

