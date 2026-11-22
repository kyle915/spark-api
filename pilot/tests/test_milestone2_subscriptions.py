"""Milestone 2 automated test suite:
- Feature 7: Self-service brand onboarding ('Use Spark with my team'), starter
  program & recap template, repeat-safe invites, interrupted signup resume,
  managed-account resolution, mobile field submission, and self-service visit
  approval + shareable recap without Ignite staff.
- Feature 8: Plan permissions (`Free`, `Core`, `Pro`, quote-only `Scale`),
  timezone-aware monthly canonical visit metering, multi-submission dedupe,
  historical import exclusion, reversal/cancellation adjustment, non-blocking
  in-progress fieldwork at cap, and server-side API parity.
- Feature 9 & Acceptance Scenario 5 ('Free through paid'): Pre-confirmation
  disclosures, explicit recurring-billing consent, Stripe Test Mode checkout &
  portal sessions, HMAC webhook verification, replay deduplication, monotonic
  out-of-order protection, failed payment surfacing, paid-through cancellation,
  $1,500 60-day Pro pilot provisioning/expiry, managed SOW + add-on preservation,
  and strict separation from agency invoices, rebates, and worker payouts."""

from __future__ import annotations

import json
import time as time_module
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

from asgiref.sync import async_to_sync
from django.contrib.auth.models import AnonymousUser
from django.core.management import call_command
from django.test import Client, RequestFactory, TransactionTestCase, override_settings
from django.utils import timezone
from gqlauth.core.middlewares import USER_OR_ERROR_KEY
from gqlauth.models import UserStatus

from billing.models import Invoice
from brand_insights.models import BrandInsight
from config.schema_client import schema_clients
from events.models import EventType, RequestType, Retailer, TimeZone
from jobs.models import AmbassadorJob
from legal.models import LegalAcceptance
from pilot.costs import build_brand_cost_rollups
from pilot.entitlements import (
    PlanEntitlementError,
    compute_monthly_visit_usage,
    ensure_brand_subscription,
    resolve_brand_entitlements,
)
from pilot.finance import list_finance_exceptions
from pilot.imports import commit_provider_import
from pilot.models import (
    BrandOnboardingProgress,
    BrandSubscription,
    CanonicalVisit,
    StripeCheckoutRecord,
    StripeWebhookEvent,
    VisitSubmissionSource,
    WorkspaceInvitation,
    WorkspaceMembership,
    WorkspaceProfile,
)
from pilot.onboarding import (
    approve_canonical_visit_recap,
    get_onboarding_state,
    onboarding_invite_contributor,
    reverse_canonical_visit_completion,
    schedule_standalone_visit,
    select_onboarding_starter_program,
    start_or_resume_self_serve_onboarding,
)
from pilot.permissions import resolve_workspace_access
from pilot.pos import build_retailer_pos_measurement
from pilot.subscriptions import (
    cancel_brand_subscription,
    change_subscription_plan,
    complete_test_mode_checkout,
    create_customer_portal_session,
    create_subscription_checkout_session,
    get_plans_and_billing_overview,
    preview_subscription_change,
    process_stripe_webhook,
    provision_pro_pilot,
    sign_stripe_webhook_payload,
)
from pilot.workspaces import ensure_workspace_profile
from receipts.models import ConsumerReceipt
from tenants.models import Role, Tenant, TenantedUser, User


def _exec_gql(query: str, *, user=None, variables: dict | None = None, headers: dict | None = None):
    rf = RequestFactory()
    actual_user = user if user is not None else AnonymousUser()
    meta_headers = {}
    for k, v in (headers or {}).items():
        key = f"HTTP_{k.upper().replace('-', '_')}" if not k.startswith("HTTP_") else k
        meta_headers[key] = v
    wsgi_req = rf.post("/api/v615204/graphql/clients", **meta_headers)
    wsgi_req.user = actual_user
    mock_uoe = type("MockUserOrError", (), {"user": actual_user, "errors": None})()

    class _MockRequest:
        def __init__(self):
            self.wsgi_request = wsgi_req
            self.user = actual_user
            self.headers = headers or {}
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

    return async_to_sync(schema_clients.execute)(
        query,
        variable_values=variables or {},
        context_value=_Ctx(req),
    )


class Milestone2SubscriptionsTests(TransactionTestCase):
    def setUp(self):
        # 0001_initial seeds Ambassador/Spark Admin with explicit PKs and
        # null slugs without advancing the serial — use ensure_role so the
        # alphabetically-first test does not collide on tenants_role_pkey.
        from tenants.tests.base import ensure_role

        self.client_role = ensure_role("Client", slug=Role.CLIENT_SLUG)
        self.admin_role = ensure_role("Spark Admin", slug=Role.SPARK_ADMIN_SLUG)
        self.ignite_admin = User.objects.create_user(
            username="ignite-ops@demo.spark",
            email="ignite-ops@demo.spark",
            password="Password123!",
            role=self.admin_role,
            is_staff=True,
        )
        UserStatus.objects.update_or_create(
            user=self.ignite_admin, defaults={"verified": True, "archived": False}
        )
        self.http_client = Client()

    def test_feature7_self_serve_onboarding_idempotency_and_first_visit(self):
        # 1. Public starter catalog & unauthenticated GraphQL self-serve signup
        cat_res = _exec_gql(
            """
            query {
              selfServeStarterCatalog {
                signupEnabled
                starters { key name templateName defaultSkuHint }
              }
            }
            """
        )
        self.assertIsNone(cat_res.errors)
        self.assertTrue(cat_res.data["selfServeStarterCatalog"]["signupEnabled"])
        self.assertEqual(len(cat_res.data["selfServeStarterCatalog"]["starters"]), 3)

        signup_mutation = """
        mutation StartOnboarding(
          $email: String!
          $password: String!
          $firstName: String!
          $lastName: String!
          $brandName: String!
          $tz: String!
        ) {
          startSelfServeOnboarding(
            email: $email
            password: $password
            firstName: $firstName
            lastName: $lastName
            brandName: $brandName
            businessTimezone: $tz
            acceptTerms: true
          ) {
            ok
            error
            token
            refreshToken
            tenantUuid
            tenantName
            membershipUuid
            workspaceRole
            resumedExisting
            resolvedManaged
            joinedInvitation
          }
        }
        """
        res1 = _exec_gql(
            signup_mutation,
            variables={
                "email": "founder@canyonspritz.test",
                "password": "Password123!",
                "firstName": "Maya",
                "lastName": "Lin",
                "brandName": "Canyon Spritz Co.",
                "tz": "America/Los_Angeles",
            },
        )
        self.assertIsNone(res1.errors)
        payload1 = res1.data["startSelfServeOnboarding"]
        self.assertTrue(payload1["ok"], payload1["error"])
        self.assertTrue(payload1["token"])
        self.assertFalse(payload1["resumedExisting"])
        self.assertEqual(payload1["workspaceRole"], "brand_admin")

        founder = User.objects.get(email="founder@canyonspritz.test")
        tenant = Tenant.objects.get(uuid=payload1["tenantUuid"])
        self.assertTrue(LegalAcceptance.objects.filter(user=founder).exists())

        sub = ensure_brand_subscription(tenant)
        self.assertEqual(sub.plan_code, WorkspaceProfile.PLAN_FREE)
        self.assertEqual(sub.status, BrandSubscription.STATUS_FREE_ACTIVE)
        self.assertEqual(sub.stripe_subscription_id, "")

        # 2. Interrupted signup / page reload resumes exact workspace with zero duplicates
        res2 = _exec_gql(
            signup_mutation,
            variables={
                "email": "founder@canyonspritz.test",
                "password": "Password123!",
                "firstName": "Maya",
                "lastName": "Lin",
                "brandName": "Canyon Spritz Co.",
                "tz": "America/Los_Angeles",
            },
        )
        self.assertIsNone(res2.errors)
        payload2 = res2.data["startSelfServeOnboarding"]
        self.assertTrue(payload2["ok"])
        self.assertTrue(payload2["resumedExisting"])
        self.assertEqual(payload2["tenantUuid"], payload1["tenantUuid"])
        self.assertEqual(
            Tenant.objects.filter(name="Canyon Spritz Co.").count(), 1
        )
        self.assertEqual(
            WorkspaceMembership.objects.filter(tenant=tenant, user=founder).count(),
            1,
        )

        # 3. Select starter program & recap template (idempotent across reloads)
        access = resolve_workspace_access(founder, tenant=tenant)
        prog1 = select_onboarding_starter_program(
            access,
            starter_key="beverage_retail_demo",
            starter_sku_name="Canyon Blood Orange 12oz",
        )
        prog2 = select_onboarding_starter_program(
            access,
            starter_key="on_premise_tasting",
            starter_sku_name="Canyon Blood Orange 12oz",
        )
        self.assertEqual(prog1.starter_event_type_id, prog2.starter_event_type_id)
        self.assertEqual(EventType.objects.filter(tenant=tenant).count(), 1)
        submission_code = prog2.starter_assignment.submission_code
        self.assertTrue(submission_code.startswith("PA-"))

        # 4. Repeat-safe contributor invite + invitee signup resolves into invited workspace
        inv1 = onboarding_invite_contributor(
            access,
            email="rep@canyonspritz.test",
            workspace_role=WorkspaceMembership.ROLE_FIELD_CONTRIBUTOR,
        )
        inv2 = onboarding_invite_contributor(
            access,
            email="rep@canyonspritz.test",
            workspace_role=WorkspaceMembership.ROLE_FIELD_CONTRIBUTOR,
        )
        self.assertEqual(inv1.uuid, inv2.uuid)
        self.assertEqual(
            WorkspaceInvitation.objects.filter(
                tenant=tenant, email="rep@canyonspritz.test"
            ).count(),
            1,
        )

        # Unauthenticated attacker guessing an invited email without token MUST be rejected
        with self.assertRaises(PermissionError):
            start_or_resume_self_serve_onboarding(
                email="rep@canyonspritz.test",
                password="AttackerPassword123!",
                first_name="Mallory",
                last_name="Guess",
                brand_name="Should Not Claim Invite",
            )
        self.assertFalse(
            User.objects.filter(email="rep@canyonspritz.test").exists()
        )
        inv1.refresh_from_db()
        self.assertEqual(inv1.status, WorkspaceInvitation.STATUS_PENDING)

        rep_signup = start_or_resume_self_serve_onboarding(
            email="rep@canyonspritz.test",
            password="Password123!",
            first_name="Jules",
            last_name="Field",
            brand_name="Should Not Create Duplicate Brand",
            invitation_token=inv1.token,
        )
        self.assertTrue(rep_signup["joined_invitation"])
        self.assertEqual(rep_signup["tenant"].id, tenant.id)
        self.assertFalse(
            Tenant.objects.filter(name="Should Not Create Duplicate Brand").exists()
        )

        # 5. Existing Ignite-managed client account resolves to managed SOW workspace
        managed_tenant = Tenant.objects.create(
            name="Legacy Managed Beverage",
            slug="legacy-managed-bev",
            created_by=self.ignite_admin,
            updated_by=self.ignite_admin,
        )
        managed_user = User.objects.create_user(
            username="vp@legacymanaged.test",
            email="vp@legacymanaged.test",
            password="Password123!",
            role=self.client_role,
        )
        TenantedUser.objects.create(tenant=managed_tenant, user=managed_user)
        managed_res = start_or_resume_self_serve_onboarding(
            email="vp@legacymanaged.test",
            password="Password123!",
        )
        self.assertTrue(managed_res["resolved_managed"])
        self.assertEqual(managed_res["tenant"].id, managed_tenant.id)
        managed_ent = resolve_brand_entitlements(managed_tenant)
        self.assertTrue(managed_ent.is_managed_sow)

        # 6. Mobile field recap GET + POST + brand self-service approval (no Ignite staff!)
        get_resp = self.http_client.get(
            f"/api/public/provider-submit/{submission_code}"
        )
        self.assertEqual(get_resp.status_code, 200)
        progress = BrandOnboardingProgress.objects.get(tenant=tenant)
        self.assertTrue(progress.step_mobile_link_opened)

        post_resp = self.http_client.post(
            f"/api/public/provider-submit/{submission_code}",
            data=json.dumps(
                {
                    "storeName": "Pacific Market #12",
                    "address": "400 Ocean Ave",
                    "city": "Santa Monica",
                    "stateCode": "CA",
                    "visitDate": "2026-11-15",
                    "samplesDistributed": 94,
                    "consumersSampled": 76,
                    "repObservedUnits": 19,
                    "photos": [
                        "https://example.invalid/setup-1.jpg",
                        "https://example.invalid/sampling-2.jpg",
                    ],
                    "receipts": ["https://example.invalid/receipt-1.jpg"],
                    "productSpend": "32.50",
                    "notes": "Strong afternoon traffic near cold box.",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(post_resp.status_code, 200)
        visit_uuid = post_resp.json()["visitUuid"]

        # Unapproved visit does NOT count toward completed monthly allowance yet
        usage_before = compute_monthly_visit_usage(
            tenant,
            year=2026,
            month=11,
            at_time=datetime(2026, 11, 16, 18, 0, tzinfo=dt_timezone.utc),
        )
        self.assertEqual(usage_before["completed_visits_used"], 0)
        self.assertEqual(usage_before["in_progress_or_pending_count"], 1)

        # Brand admin approves recap without Ignite staff -> mints shareable recap link!
        approved_visit, shared_link = approve_canonical_visit_recap(
            access,
            visit_uuid=visit_uuid,
            completed_at=datetime(2026, 11, 16, 18, 5, tzinfo=dt_timezone.utc),
        )
        self.assertEqual(approved_visit.status, CanonicalVisit.STATUS_COMPLETED)
        self.assertTrue(shared_link.token.startswith("wsl_"))

        share_resp = self.http_client.get(
            f"/api/public/workspace-share/{shared_link.token}"
        )
        self.assertEqual(share_resp.status_code, 200)
        self.assertEqual(
            share_resp.json()["visit"]["storeLabel"], "Pacific Market #12"
        )

        st = get_onboarding_state(access)
        self.assertEqual(st["steps_completed"], 5)
        self.assertTrue(st["step_first_visit_completed"])
        self.assertEqual(st["first_shared_token"], shared_link.token)

    @override_settings(CLIENT_BILLING_LAUNCHED=True)
    def test_feature8_monthly_metering_timezone_cutoffs_caps_and_reversals(self):
        signup = start_or_resume_self_serve_onboarding(
            email="ops@westcoastfizz.test",
            password="Password123!",
            first_name=" Devon ",
            last_name=" Park ",
            brand_name="West Coast Fizz",
            business_timezone="America/Los_Angeles",
        )
        tenant = signup["tenant"]
        owner = signup["user"]
        access = resolve_workspace_access(owner, tenant=tenant)
        prog = select_onboarding_starter_program(
            access, starter_key="beverage_retail_demo"
        )
        provider = prog.default_provider

        # 1. Timezone cutoff: 2026-11-01 03:30 UTC == 2026-10-31 20:30 PDT (counts in OCT, not NOV!)
        oct_visit = CanonicalVisit.objects.create(
            tenant=tenant,
            primary_provider=provider,
            store_key="sunset-1",
            store_label="Sunset Grocers #1",
            visit_date=date(2026, 10, 31),
            shift_key="oct-late",
            status=CanonicalVisit.STATUS_COMPLETED,
            completed_at=datetime(2026, 11, 1, 3, 30, tzinfo=dt_timezone.utc),
        )
        # 2. Nov boundary: 2026-11-01 08:30 UTC == 2026-11-01 00:30 PST (counts in NOV!)
        nov_visit = CanonicalVisit.objects.create(
            tenant=tenant,
            primary_provider=provider,
            store_key="sunset-2",
            store_label="Sunset Grocers #2",
            visit_date=date(2026, 11, 1),
            shift_key="nov-early",
            status=CanonicalVisit.STATUS_COMPLETED,
            completed_at=datetime(2026, 11, 1, 8, 30, tzinfo=dt_timezone.utc),
            co_brand_share_pct=Decimal("75.00"),
        )
        # Attach 3 submissions (2 agency + 1 mobile) to `nov_visit` -> STILL 1 canonical visit!
        for idx, kind in enumerate(
            [
                VisitSubmissionSource.KIND_PROVIDER_CSV,
                VisitSubmissionSource.KIND_PROVIDER_CSV,
                VisitSubmissionSource.KIND_WALKUP_CHECKIN,
            ]
        ):
            VisitSubmissionSource.objects.create(
                tenant=tenant,
                canonical_visit=nov_visit,
                provider=provider,
                source_kind=kind,
                recap_status=VisitSubmissionSource.RECAP_APPROVED,
                samples_distributed=40 + idx,
            )

        # 3. Late approval: conducted Oct 29, approved Nov 3 -> counts in NOV without rewriting Oct!
        CanonicalVisit.objects.create(
            tenant=tenant,
            primary_provider=provider,
            store_key="sunset-3",
            store_label="Sunset Grocers #3",
            visit_date=date(2026, 10, 29),
            shift_key="late-approval",
            status=CanonicalVisit.STATUS_COMPLETED,
            completed_at=datetime(2026, 11, 3, 19, 0, tzinfo=dt_timezone.utc),
        )

        # 4. Historical import + needs_review + cancelled visits -> 0 counted units!
        CanonicalVisit.objects.create(
            tenant=tenant,
            primary_provider=provider,
            store_key="hist-1",
            store_label="Historical Store",
            visit_date=date(2026, 11, 5),
            shift_key="hist",
            status=CanonicalVisit.STATUS_COMPLETED,
            is_historical_import=True,
            completed_at=datetime(2026, 11, 5, 19, 0, tzinfo=dt_timezone.utc),
        )
        CanonicalVisit.objects.create(
            tenant=tenant,
            primary_provider=provider,
            store_key="draft-1",
            store_label="Pending Store",
            visit_date=date(2026, 11, 6),
            shift_key="pending",
            status=CanonicalVisit.STATUS_NEEDS_REVIEW,
        )

        oct_usage = compute_monthly_visit_usage(
            tenant,
            year=2026,
            month=10,
            at_time=datetime(2026, 11, 10, 12, 0, tzinfo=dt_timezone.utc),
        )
        self.assertEqual(oct_usage["completed_visits_used"], 1)
        self.assertEqual(
            oct_usage["counted_visits"][0]["visit_uuid"], str(oct_visit.uuid)
        )

        nov_usage = compute_monthly_visit_usage(
            tenant,
            year=2026,
            month=11,
            at_time=datetime(2026, 11, 10, 12, 0, tzinfo=dt_timezone.utc),
        )
        self.assertEqual(nov_usage["completed_visits_used"], 2)
        self.assertEqual(nov_usage["historical_excluded_count"], 1)
        self.assertEqual(nov_usage["in_progress_or_pending_count"], 1)
        self.assertEqual(nov_usage["co_branded_visit_count"], 1)
        nov_item = next(
            c
            for c in nov_usage["counted_visits"]
            if c["visit_uuid"] == str(nov_visit.uuid)
        )
        self.assertEqual(nov_item["submission_count"], 3)
        self.assertEqual(nov_item["co_brand_share_pct"], "75.00")

        # 5. Deterministic reversal drops monthly completed visit count from 2 -> 1
        reverse_canonical_visit_completion(
            access,
            visit_uuid=str(nov_visit.uuid),
            new_status=CanonicalVisit.STATUS_CANCELLED,
            reason="Duplicate test booking cancelled",
        )
        nov_after_rev = compute_monthly_visit_usage(
            tenant,
            year=2026,
            month=11,
            at_time=datetime(2026, 11, 10, 12, 5, tzinfo=dt_timezone.utc),
        )
        self.assertEqual(nov_after_rev["completed_visits_used"], 1)
        self.assertEqual(nov_after_rev["cancelled_or_reversed_count"], 1)

        # 6. Server-side plan feature gating on Free (both Python services & GraphQL!)
        with self.assertRaises(PlanEntitlementError):
            build_brand_cost_rollups(access)
        with self.assertRaises(PlanEntitlementError):
            list_finance_exceptions(access)
        with self.assertRaises(PlanEntitlementError):
            build_retailer_pos_measurement(access)
        with self.assertRaises(PlanEntitlementError):
            commit_provider_import(
                access,
                provider_uuid=str(provider.uuid),
                filename="ongoing.csv",
                csv_text="Store,Date,Samples\nSunset,2026-11-10,50\n",
                column_map={
                    "retailer_name": "Store",
                    "visit_date": "Date",
                    "samples_distributed": "Samples",
                },
                is_historical=False,
            )

        # Historical backfill (`is_historical=True`) IS allowed on Free and creates 0 billable units!
        Retailer.objects.create(
            tenant=tenant,
            name="Backfill Market",
            address="500 Ocean Blvd",
            created_by=owner,
            updated_by=owner,
        )
        hist_job = commit_provider_import(
            access,
            provider_uuid=str(provider.uuid),
            filename="backfill.csv",
            csv_text="Store,Date,Samples\nBackfill Market,2026-11-08,65\n",
            column_map={
                "retailer_name": "Store",
                "visit_date": "Date",
                "samples_distributed": "Samples",
            },
            is_historical=True,
        )
        self.assertEqual(hist_job.created_count, 1)
        nov_after_hist = compute_monthly_visit_usage(
            tenant,
            year=2026,
            month=11,
            at_time=datetime(2026, 11, 10, 12, 10, tzinfo=dt_timezone.utc),
        )
        self.assertEqual(nov_after_hist["completed_visits_used"], 1)

        # Free blocks BrandInsight store recommendations via GraphQL while returning graceful planGated on dashboards
        BrandInsight.objects.create(
            tenant=tenant,
            kind="recommendation",
            code="rebook_high_velocity",
            scope_key="store:sunset-2",
            subject="Sunset Grocers #2",
            template="Rebook Sunset Grocers #2.",
            body="Rebook Sunset Grocers #2.",
            period_start=date(2026, 11, 1),
            period_end=date(2026, 11, 7),
            generated_at=timezone.now(),
            status="published",
        )
        gql_free = _exec_gql(
            """
            query($tid: ID!) {
              tenantStoreInsights(tenantId: $tid) {
                recommendations { id subject }
              }
              brandCostDashboard {
                planGated
                requiredPlan
                planUpgradeMessage
              }
            }
            """,
            user=owner,
            variables={"tid": str(tenant.id)},
            headers={"X-Spark-Tenant": str(tenant.uuid), "X-Tenant-ID": str(tenant.uuid)},
        )
        self.assertIsNone(gql_free.errors)
        self.assertEqual(
            gql_free.data["tenantStoreInsights"]["recommendations"], []
        )
        self.assertTrue(gql_free.data["brandCostDashboard"]["planGated"])
        self.assertEqual(gql_free.data["brandCostDashboard"]["requiredPlan"], "pro")

    @override_settings(CLIENT_BILLING_LAUNCHED=True)
    def test_scenario5_free_through_paid_webhooks_pilot_and_managed_sow(self):
        """Acceptance Scenario 5 ('Free through paid') + $1,500 60-day Pro pilot +
        managed SOW account + strict invoice/rebate/payout separation."""
        signup = start_or_resume_self_serve_onboarding(
            email="billing@harborbotanicals.test",
            password="Password123!",
            first_name="Elena",
            last_name="Vance",
            brand_name="Harbor Botanicals",
            business_timezone="America/Chicago",
        )
        tenant = signup["tenant"]
        owner = signup["user"]
        access = resolve_workspace_access(owner, tenant=tenant)
        prog = select_onboarding_starter_program(
            access, starter_key="beverage_retail_demo"
        )
        submission_code = prog.starter_assignment.submission_code

        now = timezone.now()
        # Schedule an in-progress 6th shift BEFORE hitting the 5-visit cap!
        sixth_visit, sixth_assign = schedule_standalone_visit(
            access,
            store_name="Lakeshore Market #6",
            visit_date=now.date(),
            city="Chicago",
            state_code="IL",
        )

        # Complete 5 visits on Free -> hits 5/5 monthly cap
        for i in range(1, 6):
            v, _ = schedule_standalone_visit(
                access,
                store_name=f"Lakeshore Market #{i}",
                visit_date=now.date(),
                city="Chicago",
                state_code="IL",
            )
            approve_canonical_visit_recap(
                access, visit_uuid=str(v.uuid), completed_at=now
            )

        usage_at_cap = compute_monthly_visit_usage(tenant, at_time=now)
        self.assertEqual(usage_at_cap["completed_visits_used"], 5)
        self.assertTrue(usage_at_cap["at_or_over_cap"])
        self.assertFalse(usage_at_cap["can_book_new_visit"])

        # 1. CRITICAL: In-progress 6th fieldwork shift STILL submits & approves with zero automatic charge!
        sixth_post = self.http_client.post(
            f"/api/public/provider-submit/{sixth_assign.submission_code}",
            data=json.dumps(
                {
                    "visitUuid": str(sixth_visit.uuid),
                    "samplesDistributed": 88,
                    "consumersSampled": 70,
                    "repObservedUnits": 22,
                    "photos": ["https://example.invalid/shift6.jpg"],
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(sixth_post.status_code, 200)
        approve_canonical_visit_recap(
            access, visit_uuid=str(sixth_visit.uuid), completed_at=now
        )
        # Also program-level walk-up submission link never blocks an active rep mid-shift!
        walkup_post = self.http_client.post(
            f"/api/public/provider-submit/{submission_code}",
            data=json.dumps(
                {
                    "storeName": "Lakeshore Pop-Up Shift",
                    "city": "Chicago",
                    "stateCode": "IL",
                    "visitDate": now.date().isoformat(),
                    "samplesDistributed": 50,
                    "consumersSampled": 40,
                    "repObservedUnits": 11,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(walkup_post.status_code, 200)

        # 2. Booking a NEW visit at cap is blocked on BOTH standalone scheduling AND direct `createRequest` GraphQL!
        with self.assertRaises(PlanEntitlementError) as cap_err:
            schedule_standalone_visit(
                access,
                store_name="Blocked 7th Store",
                visit_date=now.date(),
                city="Chicago",
                state_code="IL",
            )
        self.assertEqual(cap_err.exception.code, "MONTHLY_VISIT_LIMIT_REACHED")

        tz_obj, _ = TimeZone.objects.get_or_create(
            code="CST",
            defaults={
                "name": "Central Time",
                "offset": -6,
                "created_by": owner,
                "updated_by": owner,
            },
        )
        req_type = RequestType.objects.filter(tenant=tenant).first()
        direct_req = _exec_gql(
            """
            mutation BlockedCreateRequest($input: CreateRequestInput!) {
              createRequest(input: $input) {
                ... on RequestDetailResponse {
                  success
                  message
                }
              }
            }
            """,
            user=owner,
            variables={
                "input": {
                    "name": "Blocked Direct API Request",
                    "date": (now + timedelta(days=2)).date().isoformat(),
                    "startTime": "12:00:00",
                    "endTime": "15:00:00",
                    "address": "100 N State St, Chicago, IL",
                    "coordinates": [-87.6278, 41.8832],
                    "timezoneId": str(tz_obj.id),
                    "requestTypeId": str(req_type.id),
                    "details": [],
                    "products": [],
                }
            },
            headers={"X-Spark-Tenant": str(tenant.uuid), "X-Tenant-ID": str(tenant.uuid)},
        )
        self.assertIsNone(direct_req.errors)
        self.assertFalse(direct_req.data["createRequest"]["success"])
        self.assertIn(
            "Monthly visit limit reached", direct_req.data["createRequest"]["message"]
        )

        # 3. Pre-confirmation preview, explicit consent check, quote-only Scale guard, and checkout idempotency
        preview_pro = preview_subscription_change(access, target_plan_code="pro")
        self.assertEqual(preview_pro["monthly_price_usd"], "750.00")
        self.assertEqual(preview_pro["charge_today_usd"], "750.00")
        self.assertEqual(preview_pro["included_visits"], 100)
        self.assertIn("$750.00", preview_pro["disclosure"])

        with self.assertRaises(ValueError):
            create_subscription_checkout_session(
                access, target_plan_code="pro", explicit_terms_accepted=False
            )
        with self.assertRaises(PlanEntitlementError) as scale_err:
            create_subscription_checkout_session(
                access, target_plan_code="scale", explicit_terms_accepted=True
            )
        self.assertEqual(scale_err.exception.code, "QUOTE_ONLY_PLAN")

        chk1 = create_subscription_checkout_session(
            access,
            target_plan_code="pro",
            explicit_terms_accepted=True,
            idempotency_key="idem-pro-upgrade-1",
        )
        chk2 = create_subscription_checkout_session(
            access,
            target_plan_code="pro",
            explicit_terms_accepted=True,
            idempotency_key="idem-pro-upgrade-1",
        )
        self.assertEqual(chk1.uuid, chk2.uuid)
        self.assertEqual(StripeCheckoutRecord.objects.filter(tenant=tenant).count(), 1)

        # 4. Webhook signature rejection, livemode rejection, verified activation, replay dedupe & out-of-order safety
        bad_sig_resp = self.http_client.post(
            "/api/public/stripe-billing/webhook",
            data=b'{"id":"evt_bad"}',
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="t=1700000000,v1=deadbeef",
        )
        self.assertEqual(bad_sig_resp.status_code, 400)

        now_ts = int(time_module.time())
        live_payload = json.dumps(
            {
                "id": "evt_live_rejected",
                "type": "checkout.session.completed",
                "created": now_ts,
                "livemode": True,
                "data": {"object": {"id": chk1.stripe_session_id}},
            }
        ).encode("utf-8")
        live_sig = sign_stripe_webhook_payload(live_payload, timestamp=now_ts)
        live_resp = self.http_client.post(
            "/api/public/stripe-billing/webhook",
            data=live_payload,
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE=live_sig,
        )
        self.assertEqual(live_resp.status_code, 403)

        # Complete test-mode checkout (routes signed `checkout.session.completed` through `process_stripe_webhook`)
        wh_res = complete_test_mode_checkout(
            access, stripe_session_id=chk1.stripe_session_id
        )
        self.assertEqual(wh_res["status"], "processed")

        ent_pro = resolve_brand_entitlements(tenant)
        self.assertEqual(ent_pro.effective_plan_code, WorkspaceProfile.PLAN_PRO)
        self.assertTrue(ent_pro.allows_brand_cost_tracking)
        self.assertTrue(ent_pro.allows_finance_review_queue)
        self.assertTrue(ent_pro.allows_tracked_store_actions)

        # Historical usage is preserved (`6 / 100`) and new bookings are unlocked!
        usage_pro = compute_monthly_visit_usage(tenant, at_time=now)
        self.assertEqual(usage_pro["completed_visits_used"], 6)
        self.assertEqual(usage_pro["included_visits"], 100)
        self.assertEqual(usage_pro["remaining_visits"], 94)
        self.assertTrue(usage_pro["can_book_new_visit"])

        seventh_visit, _ = schedule_standalone_visit(
            access,
            store_name="Unlocked 7th Store on Pro",
            visit_date=now.date(),
            city="Chicago",
            state_code="IL",
        )
        self.assertIsNotNone(seventh_visit.id)

        # Replay exact same webhook event ID -> ignored_duplicate!
        replayed_event = StripeWebhookEvent.objects.get(tenant=tenant)
        replay_body = json.dumps(replayed_event.payload).encode("utf-8")
        replay_sig = sign_stripe_webhook_payload(replay_body, timestamp=now_ts)
        replay_out = process_stripe_webhook(replay_body, replay_sig, now_ts=now_ts)
        self.assertEqual(replay_out["status"], "ignored_duplicate")

        # Out-of-order older event (`created` older than watermark) -> ignored_stale_out_of_order!
        sub_after_pro = BrandSubscription.objects.get(tenant=tenant)
        stale_ts = int(sub_after_pro.last_stripe_event_created) - 50
        stale_body = json.dumps(
            {
                "id": "evt_test_stale_older_delivery",
                "type": "customer.subscription.deleted",
                "created": stale_ts,
                "livemode": False,
                "data": {
                    "object": {
                        "id": sub_after_pro.stripe_subscription_id,
                        "customer": sub_after_pro.stripe_customer_id,
                        "metadata": {"tenant_uuid": str(tenant.uuid)},
                    }
                },
            }
        ).encode("utf-8")
        stale_sig = sign_stripe_webhook_payload(stale_body, timestamp=now_ts)
        stale_out = process_stripe_webhook(stale_body, stale_sig, now_ts=now_ts)
        self.assertEqual(stale_out["status"], "ignored_stale_out_of_order")
        self.assertEqual(
            resolve_brand_entitlements(tenant).effective_plan_code,
            WorkspaceProfile.PLAN_PRO,
        )

        # 5. Customer portal session + failed renewal surfacing without deleting visit history
        portal = create_customer_portal_session(access)
        self.assertIn("portal_url", portal)

        fail_ts = int(sub_after_pro.last_stripe_event_created) + 10
        fail_body = json.dumps(
            {
                "id": "evt_test_payment_failed_1",
                "type": "invoice.payment_failed",
                "created": fail_ts,
                "livemode": False,
                "data": {
                    "object": {
                        "id": "in_test_failed_1",
                        "customer": sub_after_pro.stripe_customer_id,
                        "subscription": sub_after_pro.stripe_subscription_id,
                        "last_finalization_error": {
                            "message": "Your card has insufficient funds."
                        },
                        "metadata": {"tenant_uuid": str(tenant.uuid)},
                    }
                },
            }
        ).encode("utf-8")
        fail_sig = sign_stripe_webhook_payload(fail_body, timestamp=now_ts)
        fail_resp = self.http_client.post(
            "/api/public/stripe-billing/webhook",
            data=fail_body,
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE=fail_sig,
        )
        self.assertEqual(fail_resp.status_code, 200)
        ov_failed = get_plans_and_billing_overview(access)
        self.assertTrue(ov_failed["has_payment_failure"])
        self.assertIn("insufficient funds", ov_failed["payment_failure_message"])
        # Visit history is 100% intact!
        self.assertEqual(
            CanonicalVisit.objects.filter(
                tenant=tenant, status=CanonicalVisit.STATUS_COMPLETED
            ).count(),
            6,
        )

        # 6. Cancellation preserves paid-through Pro access until `current_period_end`, then falls back to Free!
        canceled_sub = cancel_brand_subscription(
            access, immediate=False, reason="Seasonal pause"
        )
        self.assertTrue(canceled_sub.cancel_at_period_end)
        self.assertIsNotNone(canceled_sub.current_period_end)

        ent_during_paid_through = resolve_brand_entitlements(
            tenant, at_time=canceled_sub.current_period_end - timedelta(days=1)
        )
        self.assertEqual(
            ent_during_paid_through.effective_plan_code, WorkspaceProfile.PLAN_PRO
        )
        self.assertTrue(ent_during_paid_through.allows_brand_cost_tracking)

        ent_after_period_end = resolve_brand_entitlements(
            tenant, at_time=canceled_sub.current_period_end + timedelta(seconds=1)
        )
        self.assertEqual(
            ent_after_period_end.effective_plan_code, WorkspaceProfile.PLAN_FREE
        )
        self.assertFalse(ent_after_period_end.allows_brand_cost_tracking)

        # Strict domain separation: zero agency invoices, consumer receipts, or BA payout jobs created!
        self.assertEqual(Invoice.objects.count(), 0)
        self.assertEqual(ConsumerReceipt.objects.count(), 0)
        self.assertEqual(AmbassadorJob.objects.count(), 0)

        # 7. Manually provisioned $1,500, 60-day Pro pilot + expiry without unauthorized auto-billing
        pilot_tenant = Tenant.objects.create(
            name="Suncrest Pilot Brand",
            slug="suncrest-pilot-brand",
            created_by=self.ignite_admin,
            updated_by=self.ignite_admin,
        )
        with self.assertRaises(PermissionError):
            provision_pro_pilot(actor=owner, tenant=pilot_tenant)

        pilot_start = datetime(2026, 11, 1, 12, 0, tzinfo=dt_timezone.utc)
        pilot_sub = provision_pro_pilot(
            actor=self.ignite_admin,
            tenant=pilot_tenant,
            pilot_fee_usd=Decimal("1500.00"),
            duration_days=60,
            included_visits_monthly=100,
            starts_at=pilot_start,
        )
        self.assertTrue(pilot_sub.is_pilot)
        self.assertEqual(pilot_sub.pilot_fee_usd, Decimal("1500.00"))
        self.assertIsNone(pilot_sub.recurring_billing_accepted_at)

        ent_pilot_day30 = resolve_brand_entitlements(
            pilot_tenant, at_time=pilot_start + timedelta(days=30)
        )
        self.assertEqual(
            ent_pilot_day30.effective_plan_code, WorkspaceProfile.PLAN_PILOT_PRO
        )
        self.assertTrue(ent_pilot_day30.allows_finance_review_queue)
        self.assertEqual(ent_pilot_day30.included_completed_visits, 100)

        ent_pilot_day61 = resolve_brand_entitlements(
            pilot_tenant, at_time=pilot_start + timedelta(days=61)
        )
        self.assertEqual(
            ent_pilot_day61.effective_plan_code, WorkspaceProfile.PLAN_FREE
        )
        self.assertEqual(
            ent_pilot_day61.subscription_status,
            BrandSubscription.STATUS_PILOT_EXPIRED,
        )

        # 8. Managed SOW account + paid add-on retains SOW reporting regardless of add-on changes
        sow_tenant = Tenant.objects.create(
            name="Managed SOW Co",
            slug="managed-sow-co",
            created_by=self.ignite_admin,
            updated_by=self.ignite_admin,
        )
        ensure_workspace_profile(
            sow_tenant,
            mode=WorkspaceProfile.MODE_MANAGED,
            plan_tier=WorkspaceProfile.PLAN_MANAGED_SOW,
        )
        WorkspaceMembership.objects.create(
            tenant=sow_tenant,
            user=owner,
            membership_scope="brand",
            workspace_role=WorkspaceMembership.ROLE_BRAND_ADMIN,
            created_by=self.ignite_admin,
        )
        sow_access = resolve_workspace_access(owner, tenant=sow_tenant)
        change_subscription_plan(
            sow_access, target_plan_code="pro", explicit_terms_accepted=True
        )
        sow_ent = resolve_brand_entitlements(sow_tenant)
        self.assertTrue(sow_ent.is_managed_sow)
        self.assertEqual(sow_ent.addon_plan_code, "pro")
        self.assertIsNone(sow_ent.included_completed_visits)

    @override_settings(
        DEBUG=True, STRIPE_BILLING_MODE="live", CLIENT_BILLING_LAUNCHED=True
    )
    def test_live_stripe_mode_refused_in_debug(self):
        signup = start_or_resume_self_serve_onboarding(
            email="safety@debugguard.test",
            password="Password123!",
            brand_name="Debug Safety Brand",
        )
        access = resolve_workspace_access(signup["user"], tenant=signup["tenant"])
        with self.assertRaises(PermissionError):
            create_subscription_checkout_session(
                access, target_plan_code="core", explicit_terms_accepted=True
            )

    def test_seed_standalone_demo_command_is_idempotent(self):
        call_command("seed_standalone_demo")
        suncrest = Tenant.objects.get(slug="suncrest-beverage-demo")
        canyon = Tenant.objects.get(name="Canyon Spritz Co.")
        harbor = Tenant.objects.get(slug="harbor-spritz-demo")
        cascade = Tenant.objects.get(slug="cascade-cold-brew-sow")

        sun_visits_1 = CanonicalVisit.objects.filter(tenant=suncrest).count()
        canyon_usage_1 = compute_monthly_visit_usage(canyon)
        self.assertEqual(canyon_usage_1["completed_visits_used"], 5)
        self.assertTrue(canyon_usage_1["at_or_over_cap"])

        # Second invocation must be completely idempotent
        call_command("seed_standalone_demo")
        self.assertEqual(
            CanonicalVisit.objects.filter(tenant=suncrest).count(), sun_visits_1
        )
        canyon_usage_2 = compute_monthly_visit_usage(canyon)
        self.assertEqual(canyon_usage_2["completed_visits_used"], 5)
        self.assertEqual(
            resolve_brand_entitlements(suncrest).subscription_status,
            "pilot_active",
        )
        self.assertEqual(
            resolve_brand_entitlements(harbor).effective_plan_code, "pro"
        )
        self.assertTrue(resolve_brand_entitlements(cascade).is_managed_sow)

    @override_settings(CLIENT_BILLING_LAUNCHED=True)
    def test_codex_security_and_billing_regressions(self):
        from unittest.mock import MagicMock, patch

        # 1. Invitation email ownership verification via GraphQL (P1)
        owner_res = start_or_resume_self_serve_onboarding(
            email="owner@vaultbev.test",
            password="Password123!",
            brand_name="Vault Beverage",
        )
        vault_tenant = owner_res["tenant"]
        vault_access = resolve_workspace_access(
            owner_res["user"], tenant=vault_tenant
        )
        inv = onboarding_invite_contributor(
            vault_access,
            email="invited-agency@vaultbev.test",
            workspace_role=WorkspaceMembership.ROLE_PROVIDER_MANAGER,
            provider_name="Vault Field Ops",
        )

        # Unauthenticated GraphQL call without token or with wrong token cannot claim invite
        no_tok_gql = _exec_gql(
            """
            mutation($email: String!, $pw: String!, $tok: String!) {
              startSelfServeOnboarding(
                email: $email
                password: $pw
                brandName: "Hijack Attempt"
                acceptTerms: true
                invitationToken: $tok
              ) {
                ok
                error
                joinedInvitation
              }
            }
            """,
            variables={
                "email": "invited-agency@vaultbev.test",
                "pw": "Password123!",
                "tok": "wrong-token-value",
            },
        )
        self.assertIsNone(no_tok_gql.errors)
        self.assertFalse(no_tok_gql.data["startSelfServeOnboarding"]["ok"])
        self.assertFalse(
            User.objects.filter(email="invited-agency@vaultbev.test").exists()
        )

        # Valid invitation token proves ownership and activates membership
        ok_tok_gql = _exec_gql(
            """
            mutation($email: String!, $pw: String!, $tok: String!) {
              startSelfServeOnboarding(
                email: $email
                password: $pw
                brandName: "Ignored Because Invite"
                acceptTerms: true
                invitationToken: $tok
              ) {
                ok
                error
                tenantUuid
                joinedInvitation
              }
            }
            """,
            variables={
                "email": "invited-agency@vaultbev.test",
                "pw": "Password123!",
                "tok": inv.token,
            },
        )
        self.assertTrue(ok_tok_gql.data["startSelfServeOnboarding"]["ok"])
        self.assertTrue(
            ok_tok_gql.data["startSelfServeOnboarding"]["joinedInvitation"]
        )
        self.assertEqual(
            ok_tok_gql.data["startSelfServeOnboarding"]["tenantUuid"],
            str(vault_tenant.uuid),
        )

        # 2. Free workspace cannot bypass Stripe Checkout via change_subscription_plan (P1)
        with self.assertRaises(PlanEntitlementError) as chk_req_err:
            change_subscription_plan(
                vault_access,
                target_plan_code="pro",
                explicit_terms_accepted=True,
            )
        self.assertEqual(chk_req_err.exception.code, "CHECKOUT_REQUIRED")

        # Activate Core first via verified checkout
        chk_core = create_subscription_checkout_session(
            vault_access,
            target_plan_code="core",
            explicit_terms_accepted=True,
            idempotency_key="vault-core-chk-1",
        )
        complete_test_mode_checkout(
            vault_access, stripe_session_id=chk_core.stripe_session_id
        )
        sub = BrandSubscription.objects.get(tenant=vault_tenant)
        sub.stripe_subscription_id = "sub_1RealRemoteCore999"
        sub.stripe_customer_id = "cus_RealVault999"
        sub.save(
            update_fields=[
                "stripe_subscription_id",
                "stripe_customer_id",
                "updated_at",
            ]
        )
        self.assertEqual(
            resolve_brand_entitlements(vault_tenant).effective_plan_code, "core"
        )

        # 3. Real Stripe subscription upgrade updates Stripe BEFORE local entitlements (P1)
        with override_settings(
            STRIPE_BILLING_MODE="test", STRIPE_SECRET_KEY="sk_test_mock_vault"
        ):
            mock_get_resp = MagicMock()
            mock_get_resp.raise_for_status.return_value = None
            mock_get_resp.json.return_value = {
                "id": "sub_1RealRemoteCore999",
                "items": {"data": [{"id": "si_CoreItem123"}]},
            }
            mock_fail_post = MagicMock()
            mock_fail_post.raise_for_status.return_value = None
            mock_fail_post.json.return_value = {
                "id": "sub_1RealRemoteCore999",
                "status": "incomplete",
            }
            with (
                patch("pilot.subscriptions.httpx.get", return_value=mock_get_resp),
                patch("pilot.subscriptions.httpx.post", return_value=mock_fail_post),
            ):
                with self.assertRaises(ValueError):
                    change_subscription_plan(
                        vault_access,
                        target_plan_code="pro",
                        explicit_terms_accepted=True,
                    )

            # Local plan MUST still be Core when Stripe did not confirm active Pro!
            self.assertEqual(
                resolve_brand_entitlements(vault_tenant).effective_plan_code,
                "core",
            )

            now_epoch = int(time_module.time())
            mock_ok_post = MagicMock()
            mock_ok_post.raise_for_status.return_value = None
            mock_ok_post.json.return_value = {
                "id": "sub_1RealRemoteCore999",
                "status": "active",
                "cancel_at_period_end": False,
                "current_period_start": now_epoch,
                "current_period_end": now_epoch + 30 * 86400,
            }
            with (
                patch(
                    "pilot.subscriptions.httpx.get", return_value=mock_get_resp
                ) as m_get,
                patch(
                    "pilot.subscriptions.httpx.post", return_value=mock_ok_post
                ) as m_post,
            ):
                upgraded = change_subscription_plan(
                    vault_access,
                    target_plan_code="pro",
                    explicit_terms_accepted=True,
                )
                self.assertEqual(upgraded.plan_code, "pro")
                m_get.assert_called_once()
                m_post.assert_called_once()
                sent_data = m_post.call_args.kwargs["data"]
                self.assertEqual(sent_data["items[0][id]"], "si_CoreItem123")
                self.assertEqual(
                    sent_data["metadata[target_plan_code]"], "pro"
                )

            # 4. Cancellation propagates to Stripe before updating local DB (P1)
            mock_cancel_post = MagicMock()
            mock_cancel_post.raise_for_status.return_value = None
            mock_cancel_post.json.return_value = {
                "id": "sub_1RealRemoteCore999",
                "status": "active",
                "cancel_at_period_end": True,
                "current_period_end": now_epoch + 30 * 86400,
            }
            with patch(
                "pilot.subscriptions.httpx.post", return_value=mock_cancel_post
            ) as m_cancel:
                canceled = cancel_brand_subscription(
                    vault_access, immediate=False, reason="End of summer wave"
                )
                self.assertTrue(canceled.cancel_at_period_end)
                m_cancel.assert_called_once()
                self.assertEqual(
                    m_cancel.call_args.kwargs["data"],
                    {"cancel_at_period_end": "true"},
                )

        # 5. Reject checkout.session.completed without verified open Spark record (P1)
        free_res = start_or_resume_self_serve_onboarding(
            email="founder@unverifiedchk.test",
            password="Password123!",
            brand_name="Unverified Checkout Brand",
        )
        free_tenant = free_res["tenant"]
        free_sub = ensure_brand_subscription(free_tenant)
        free_sub.stripe_customer_id = "cus_KnownShared123"
        free_sub.save(update_fields=["stripe_customer_id", "updated_at"])

        ts_base = int(time_module.time())
        unrelated_chk = json.dumps(
            {
                "id": "evt_unrelated_checkout_1",
                "type": "checkout.session.completed",
                "created": ts_base,
                "livemode": False,
                "data": {
                    "object": {
                        "id": "cs_test_not_from_spark",
                        "customer": "cus_KnownShared123",
                        "subscription": "sub_1ExternalOtherProduct",
                        "metadata": {},
                    }
                },
            }
        ).encode("utf-8")
        sig_unrelated = sign_stripe_webhook_payload(
            unrelated_chk, timestamp=ts_base
        )
        out_unrelated = process_stripe_webhook(
            unrelated_chk, sig_unrelated, now_ts=ts_base
        )
        self.assertEqual(
            out_unrelated["status"], StripeWebhookEvent.PROC_UNMATCHED
        )
        self.assertEqual(
            resolve_brand_entitlements(free_tenant).effective_plan_code,
            WorkspaceProfile.PLAN_FREE,
        )

        # 6. Per-domain webhook ordering & unsupported event non-advancement (P2)
        sub.refresh_from_db()
        base_sub_wm = int(sub.last_stripe_event_created)

        unsupported_body = json.dumps(
            {
                "id": "evt_unsupported_cust_upd",
                "type": "customer.updated",
                "created": base_sub_wm + 5000,
                "livemode": False,
                "data": {
                    "object": {
                        "id": "cus_RealVault999",
                        "metadata": {"tenant_uuid": str(vault_tenant.uuid)},
                    }
                },
            }
        ).encode("utf-8")
        process_stripe_webhook(
            unsupported_body,
            sign_stripe_webhook_payload(unsupported_body, timestamp=ts_base),
            now_ts=ts_base,
        )
        sub.refresh_from_db()
        self.assertEqual(int(sub.last_stripe_event_created), base_sub_wm)

        # Newer invoice.paid arrives BEFORE slightly older customer.subscription.updated
        inv_paid_ts = base_sub_wm + 200
        sub_upd_ts = base_sub_wm + 150
        inv_paid_body = json.dumps(
            {
                "id": "evt_domain_inv_paid_newer",
                "type": "invoice.paid",
                "created": inv_paid_ts,
                "livemode": False,
                "data": {
                    "object": {
                        "id": "in_vault_200",
                        "customer": "cus_RealVault999",
                        "subscription": "sub_1RealRemoteCore999",
                        "metadata": {"tenant_uuid": str(vault_tenant.uuid)},
                    }
                },
            }
        ).encode("utf-8")
        inv_out = process_stripe_webhook(
            inv_paid_body,
            sign_stripe_webhook_payload(inv_paid_body, timestamp=ts_base),
            now_ts=ts_base,
        )
        self.assertEqual(inv_out["status"], StripeWebhookEvent.PROC_PROCESSED)

        sub_upd_body = json.dumps(
            {
                "id": "evt_domain_sub_upd_slightly_older",
                "type": "customer.subscription.updated",
                "created": sub_upd_ts,
                "livemode": False,
                "data": {
                    "object": {
                        "id": "sub_1RealRemoteCore999",
                        "customer": "cus_RealVault999",
                        "status": "active",
                        "cancel_at_period_end": False,
                        "current_period_start": sub_upd_ts,
                        "current_period_end": sub_upd_ts + 30 * 86400,
                        "metadata": {
                            "tenant_uuid": str(vault_tenant.uuid),
                            "target_plan_code": "core",
                        },
                    }
                },
            }
        ).encode("utf-8")
        sub_out = process_stripe_webhook(
            sub_upd_body,
            sign_stripe_webhook_payload(sub_upd_body, timestamp=ts_base),
            now_ts=ts_base,
        )
        self.assertEqual(sub_out["status"], StripeWebhookEvent.PROC_PROCESSED)
        sub.refresh_from_db()
        self.assertEqual(sub.plan_code, "core")
        self.assertFalse(sub.cancel_at_period_end)
        self.assertEqual(int(sub.last_stripe_event_created), sub_upd_ts)

    def test_admin_only_billing_gating_and_readiness_check_default_mode(self):
        """By default (`CLIENT_BILLING_LAUNCHED=False`), all billing/subscription
        queries & mutations are strictly Ignite-admin-only, client product
        workflows are never blocked by unlaunched plan upsells, and Ignite admins
        have a boolean-only readiness + test-mode check card."""
        from django.conf import settings as dj_settings

        self.assertFalse(dj_settings.CLIENT_BILLING_LAUNCHED)
        self.assertEqual(dj_settings.STRIPE_BILLING_MODE, "test")

        signup = start_or_resume_self_serve_onboarding(
            email="clientowner@oaklandcraft.test",
            password="Password123!",
            first_name="Maya",
            last_name="Chen",
            brand_name="Oakland Craft Sodas",
            business_timezone="America/Los_Angeles",
        )
        tenant = signup["tenant"]
        client_owner = signup["user"]
        client_access = resolve_workspace_access(client_owner, tenant=tenant)
        select_onboarding_starter_program(
            client_access, starter_key="beverage_retail_demo"
        )
        headers = {
            "X-Spark-Tenant": str(tenant.uuid),
            "X-Tenant-ID": str(tenant.uuid),
        }

        # 1. Non-staff client brand owner is rejected across ALL billing queries & mutations
        blocked_queries = _exec_gql(
            """
            query {
              plansAndBillingOverview { effectivePlanCode }
            }
            """,
            user=client_owner,
            headers=headers,
        )
        self.assertIsNotNone(blocked_queries.errors)
        self.assertIn("Ignite", str(blocked_queries.errors[0].message))

        blocked_preview = _exec_gql(
            """
            query {
              previewSubscriptionPlanChange(targetPlanCode: "core") {
                monthlyPriceUsd
              }
            }
            """,
            user=client_owner,
            headers=headers,
        )
        self.assertIsNotNone(blocked_preview.errors)

        blocked_readiness = _exec_gql(
            """
            query {
              billingReadiness {
                billingMode
                clientBillingHidden
              }
            }
            """,
            user=client_owner,
            headers=headers,
        )
        self.assertIsNotNone(blocked_readiness.errors)
        self.assertIn("Ignite", str(blocked_readiness.errors[0].message))

        blocked_muts = _exec_gql(
            """
            mutation($tid: String!) {
              chk: createSubscriptionCheckout(
                targetPlanCode: "core"
                explicitTermsAccepted: true
              ) { ok error }
              comp: completeTestSubscriptionCheckout(
                stripeSessionId: "cs_test_any"
              ) { ok error }
              portal: createSubscriptionPortalSession { ok error }
              chg: changeSubscriptionPlan(
                targetPlanCode: "core"
                explicitTermsAccepted: true
              ) { ok error }
              can: cancelSubscription(immediate: false) { ok error }
              ovg: approveVolumeBandOrOverage(
                overageAgreementApproved: true
              ) { ok error }
              pil: provisionProPilotWorkspace(tenantUuid: $tid) { ok error }
              probe: runBillingTestCheck(tenantUuid: $tid) { ok error }
            }
            """,
            user=client_owner,
            variables={"tid": str(tenant.uuid)},
            headers=headers,
        )
        self.assertIsNone(blocked_muts.errors)
        for key in ("chk", "comp", "portal", "chg", "can", "ovg", "pil", "probe"):
            self.assertFalse(
                blocked_muts.data[key]["ok"], f"Expected {key} to reject client"
            )
            self.assertIn("Ignite", blocked_muts.data[key]["error"] or "")

        # 2. Client workspace features & visit scheduling are NOT blocked while billing is unlaunched
        now = timezone.now()
        for idx in range(1, 7):
            v, _ = schedule_standalone_visit(
                client_access,
                store_name=f"Oakland Market #{idx}",
                visit_date=now.date(),
                city="Oakland",
                state_code="CA",
            )
            approve_canonical_visit_recap(
                client_access, visit_uuid=str(v.uuid), completed_at=now
            )
        # Even at 6 completed visits on Free, 7th visit is NOT blocked while client billing is hidden!
        seventh, _ = schedule_standalone_visit(
            client_access,
            store_name="Oakland Market #7 Unblocked",
            visit_date=now.date(),
            city="Oakland",
            state_code="CA",
        )
        self.assertIsNotNone(seventh.id)

        BrandInsight.objects.create(
            tenant=tenant,
            kind="recommendation",
            code="rebook_high_velocity",
            scope_key="store:oakland-1",
            subject="Oakland Market #1",
            template="Rebook Oakland Market #1.",
            body="Rebook Oakland Market #1.",
            period_start=now.date() - timedelta(days=7),
            period_end=now.date(),
            generated_at=now,
            status="published",
        )
        client_dashboards = _exec_gql(
            """
            query($tid: ID!) {
              tenantStoreInsights(tenantId: $tid) {
                recommendations { id subject }
              }
              brandCostDashboard { planGated }
              financeExceptionQueue { planGated }
              retailerPosDashboard { planGated }
              weeklyBuyerStoreReport { planGated }
            }
            """,
            user=client_owner,
            variables={"tid": str(tenant.id)},
            headers=headers,
        )
        self.assertIsNone(client_dashboards.errors)
        self.assertEqual(
            len(client_dashboards.data["tenantStoreInsights"]["recommendations"]),
            1,
        )
        self.assertFalse(client_dashboards.data["brandCostDashboard"]["planGated"])
        self.assertFalse(
            client_dashboards.data["financeExceptionQueue"]["planGated"]
        )
        self.assertFalse(
            client_dashboards.data["retailerPosDashboard"]["planGated"]
        )
        self.assertFalse(
            client_dashboards.data["weeklyBuyerStoreReport"]["planGated"]
        )

        # 3. Ignite staff sees boolean-only BillingReadiness + runs one-click test check safely
        with override_settings(
            STRIPE_BILLING_MODE="test",
            STRIPE_SECRET_KEY="sk_test_SUPER_SECRET_NEVER_EXPOSE",
            STRIPE_WEBHOOK_SECRET="whsec_SUPER_SECRET_NEVER_EXPOSE",
            STRIPE_PRICE_ID_CORE="price_core_test_123",
            STRIPE_PRICE_ID_PRO="price_pro_test_456",
        ):
            staff_probe = _exec_gql(
                """
                mutation($tid: String!) {
                  runBillingTestCheck(tenantUuid: $tid) {
                    ok
                    error
                    billingMode
                    transport
                    signatureVerified
                    idempotencyVerified
                    probeEventId
                    summary
                  }
                }
                """,
                user=self.ignite_admin,
                variables={"tid": str(tenant.uuid)},
                headers=headers,
            )
            self.assertIsNone(staff_probe.errors)
            probe_data = staff_probe.data["runBillingTestCheck"]
            self.assertTrue(probe_data["ok"])
            self.assertEqual(probe_data["billingMode"], "test")
            self.assertEqual(probe_data["transport"], "stripe_test_api")
            self.assertTrue(probe_data["signatureVerified"])
            self.assertTrue(probe_data["idempotencyVerified"])
            # Customer subscription was NOT altered by readiness probe!
            self.assertEqual(
                resolve_brand_entitlements(tenant).effective_plan_code,
                WorkspaceProfile.PLAN_FREE,
            )

            staff_readiness = _exec_gql(
                """
                query {
                  billingReadiness {
                    billingMode
                    billingEnabled
                    clientBillingHidden
                    secretKeyConfigured
                    publishableKeyConfigured
                    webhookSecretConfigured
                    corePriceConfigured
                    proPriceConfigured
                    scalePriceConfigured
                    testSimulationActive
                    webhookEndpointUrl
                    recentWebhookCount
                    failedWebhookCount
                    lastWebhookAt
                    lastWebhookEventType
                    lastWebhookStatus
                    recentWebhooks {
                      stripeEventId
                      eventType
                      processingStatus
                    }
                  }
                  plansAndBillingOverview {
                    effectivePlanCode
                    isIgniteAdmin
                  }
                }
                """,
                user=self.ignite_admin,
                headers=headers,
            )
            self.assertIsNone(staff_readiness.errors)
            serialized = json.dumps(staff_readiness.data)
            self.assertNotIn("SUPER_SECRET_NEVER_EXPOSE", serialized)
            rd = staff_readiness.data["billingReadiness"]
            self.assertEqual(rd["billingMode"], "test")
            self.assertTrue(rd["clientBillingHidden"])
            self.assertTrue(rd["secretKeyConfigured"])
            self.assertTrue(rd["webhookSecretConfigured"])
            self.assertTrue(rd["corePriceConfigured"])
            self.assertTrue(rd["proPriceConfigured"])
            self.assertFalse(rd["testSimulationActive"])
            self.assertTrue(
                rd["webhookEndpointUrl"].endswith(
                    "/api/public/stripe-billing/webhook"
                )
            )
            self.assertGreaterEqual(rd["recentWebhookCount"], 1)
            self.assertEqual(
                rd["lastWebhookEventType"], "spark.billing.readiness_probe"
            )
            self.assertTrue(
                staff_readiness.data["plansAndBillingOverview"]["isIgniteAdmin"]
            )


