"""Idempotent persisted demo seed for Standalone Spark Milestones 1 & 2.

Seeds fictional beverage brands, agencies, stores, SKUs, rate cards, receipts,
POS sales feeds, published BrandInsight recommendations, self-service Free
workspaces at cap, Stripe Test Mode Pro subscriptions, and a $1,500 60-day Pro
Pilot exercising all 5 acceptance scenarios end-to-end.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand
from django.utils import timezone
from gqlauth.models import UserStatus

from billing.models import Invoice, InvoiceLineItem, recompute_invoice_totals
from brand_insights.facts import store_identity
from brand_insights.models import BrandInsight
from events.models import (
    Event,
    EventType,
    Location,
    Product,
    ProductType,
    Retailer,
    State,
)
from legal import versions as legal_versions
from legal.service import accept_current as accept_legal_terms
from pilot.actions import (
    complete_rebook_return_visit,
    create_store_recommendation_action,
    respond_store_action,
    verify_restock_action,
)
from pilot.costs import (
    allocate_shared_cost,
    link_receipt_evidence_to_cost_line,
    upsert_brand_cost_line,
    upsert_provider_agreement,
)
from pilot.entitlements import ensure_brand_subscription, ensure_plan_catalog
from pilot.finance import evaluate_finance_exceptions
from pilot.imports import commit_provider_import
from pilot.models import (
    BrandCostLine,
    BrandOnboardingProgress,
    BrandSubscription,
    CanonicalVisit,
    CrewPurchaseAdjustment,
    ProviderAssignment,
    ProviderImportJob,
    RetailerSalesImport,
    StoreRecommendationAction,
    VisitSubmissionSource,
    WorkspaceMembership,
    WorkspaceProfile,
)
from pilot.onboarding import (
    approve_canonical_visit_recap,
    ensure_role_by_slug,
    schedule_standalone_visit,
    select_onboarding_starter_program,
    start_or_resume_self_serve_onboarding,
)
from pilot.permissions import resolve_workspace_access
from pilot.pos import import_retailer_pos_csv, record_crew_purchase_adjustment
from pilot.subscriptions import (
    complete_test_mode_checkout,
    create_subscription_checkout_session,
    provision_pro_pilot,
)
from pilot.workspaces import (
    _mint_submission_code,
    create_or_update_provider,
    ensure_workspace_profile,
    upsert_workspace_membership,
)
from recaps.models import (
    CustomRecap,
    CustomRecapFile,
    CustomRecapTemplate,
    FileType,
    ReceiptReading,
    RecapFlag,
)
from tenants.models import Role, Tenant, TenantedUser, User

DEMO_PASSWORD = "SparkDemo2026!"
DEMO_TZ = ZoneInfo("America/Chicago")


def _ensure_role(slug: str, name: str) -> Role:
    return ensure_role_by_slug(slug, name)


def _ensure_demo_user(
    *,
    email: str,
    first_name: str,
    last_name: str,
    role: Role,
    password: str = DEMO_PASSWORD,
) -> User:
    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        user = User.objects.create_user(
            email=email,
            username=email,
            password=password,
            first_name=first_name,
            last_name=last_name,
            role=role,
            is_active=True,
        )
    else:
        user.first_name = first_name
        user.last_name = last_name
        user.role = role
        user.is_active = True
        user.set_password(password)
        user.save(
            update_fields=[
                "first_name",
                "last_name",
                "role",
                "is_active",
                "password",
            ]
        )
    UserStatus.objects.update_or_create(
        user=user, defaults={"verified": True, "archived": False}
    )
    accept_legal_terms(
        user.id,
        version=legal_versions.current_version(),
        ip="127.0.0.1",
        ua="seed_standalone_demo",
    )
    return user


def _current_month_anchor() -> tuple[date, date]:
    """Return a Monday week_start inside the current month so monthly visit
    metering, POS week alignment, and buyer reports all show current-month data."""
    today = datetime.now(DEMO_TZ).date()
    first_of_month = today.replace(day=1)
    offset = (0 - first_of_month.weekday()) % 7
    week_monday = first_of_month + timedelta(days=offset)
    week_sunday = week_monday + timedelta(days=6)
    return week_monday, week_sunday


class Command(BaseCommand):
    help = "Seed idempotent fictional 5-scenario demo workspaces for Milestones 1 & 2."

    def handle(self, *args, **options):
        ensure_plan_catalog()
        admin_role = _ensure_role(Role.SPARK_ADMIN_SLUG, "Spark Admin")
        client_role = _ensure_role(Role.CLIENT_SLUG, "Client")

        staff_user = _ensure_demo_user(
            email="ignite-ops@demo.spark",
            first_name="Ignite",
            last_name="OpsAdmin",
            role=admin_role,
        )
        suncrest_admin = _ensure_demo_user(
            email="suncrest-admin@demo.spark",
            first_name="Claire",
            last_name="Suncrest",
            role=client_role,
        )
        suncrest_finance = _ensure_demo_user(
            email="suncrest-finance@demo.spark",
            first_name="Maya",
            last_name="Controller",
            role=client_role,
        )
        suncrest_reviewer = _ensure_demo_user(
            email="suncrest-reviewer@demo.spark",
            first_name="Devon",
            last_name="Reviewer",
            role=client_role,
        )
        fieldpulse_user = _ensure_demo_user(
            email="fieldpulse@demo.spark",
            first_name="Jordan",
            last_name="FieldPulse",
            role=client_role,
        )
        promoworks_user = _ensure_demo_user(
            email="promoworks@demo.spark",
            first_name="Taylor",
            last_name="PromoWorks",
            role=client_role,
        )
        harbor_admin = _ensure_demo_user(
            email="harbor-admin@demo.spark",
            first_name="Ben",
            last_name="Harbor",
            role=client_role,
        )
        sow_client = _ensure_demo_user(
            email="managed-sow@demo.spark",
            first_name="Elena",
            last_name="CascadeSOW",
            role=client_role,
        )

        # ------------------------------------------------------------------
        # 1. Suncrest Beverage Co. ($1,500, 60-day Pro Pilot) — Scenarios 1-4
        # ------------------------------------------------------------------
        suncrest, _ = Tenant.objects.get_or_create(
            slug="suncrest-beverage-demo",
            defaults={
                "name": "Suncrest Beverage Co.",
                "request_url_name": "suncrest-beverage-demo",
                "created_by": staff_user,
                "updated_by": staff_user,
            },
        )
        suncrest_profile = ensure_workspace_profile(
            suncrest,
            mode=WorkspaceProfile.MODE_PILOT,
            plan_tier=WorkspaceProfile.PLAN_PILOT_PRO,
        )
        suncrest_profile.business_timezone = "America/Chicago"
        suncrest_profile.default_contribution_per_unit = Decimal("20.00")
        suncrest_profile.save()

        for u in (suncrest_admin, suncrest_finance, suncrest_reviewer):
            TenantedUser.objects.get_or_create(
                user=u,
                tenant=suncrest,
                defaults={"created_by": staff_user, "updated_by": staff_user},
            )

        staff_access_sun = resolve_workspace_access(staff_user, tenant=suncrest)
        upsert_workspace_membership(
            staff_access_sun,
            target_user=suncrest_admin,
            workspace_role=WorkspaceMembership.ROLE_BRAND_ADMIN,
        )
        access_sun_admin = resolve_workspace_access(suncrest_admin, tenant=suncrest)
        upsert_workspace_membership(
            access_sun_admin,
            target_user=suncrest_finance,
            workspace_role=WorkspaceMembership.ROLE_FINANCE_REVIEWER,
            can_approve_finance=True,
        )
        upsert_workspace_membership(
            access_sun_admin,
            target_user=suncrest_reviewer,
            workspace_role=WorkspaceMembership.ROLE_BRAND_REVIEWER,
            can_approve_recaps=True,
            can_approve_spend=False,
        )
        access_sun_finance = resolve_workspace_access(suncrest_finance, tenant=suncrest)

        provision_pro_pilot(
            actor=staff_user,
            tenant=suncrest,
            pilot_fee_usd=Decimal("1500.00"),
            duration_days=60,
            included_visits_monthly=100,
        )
        BrandOnboardingProgress.objects.update_or_create(
            tenant=suncrest,
            defaults={
                "owner": suncrest_admin,
                "starter_template_key": "retail_sampling",
                "step_workspace_created": True,
                "step_program_selected": True,
                "step_contributor_invited": True,
                "step_mobile_link_opened": True,
                "step_first_visit_completed": True,
                "completed_at": timezone.now(),
            },
        )

        agency_fp = create_or_update_provider(
            access_sun_admin, name="FieldPulse Agency", contact_email="fieldpulse@demo.spark"
        )
        agency_pw = create_or_update_provider(
            access_sun_admin, name="PromoWorks Agency", contact_email="promoworks@demo.spark"
        )
        for u, prov in ((fieldpulse_user, agency_fp), (promoworks_user, agency_pw)):
            TenantedUser.objects.get_or_create(
                user=u,
                tenant=suncrest,
                defaults={"created_by": suncrest_admin, "updated_by": suncrest_admin},
            )
            upsert_workspace_membership(
                access_sun_admin,
                target_user=u,
                workspace_role=WorkspaceMembership.ROLE_PROVIDER_MANAGER,
                provider=prov,
            )
        access_fp = resolve_workspace_access(fieldpulse_user, tenant=suncrest)
        access_pw = resolve_workspace_access(promoworks_user, tenant=suncrest)

        state_tx, _ = State.objects.get_or_create(
            code="TX",
            defaults={"name": "Texas", "created_by": staff_user, "updated_by": staff_user},
        )
        loc_austin, _ = Location.objects.get_or_create(
            code="AUS",
            defaults={
                "name": "Austin",
                "zip": "78701",
                "state": state_tx,
                "created_by": staff_user,
                "updated_by": staff_user,
            },
        )
        loc_dallas, _ = Location.objects.get_or_create(
            code="DAL",
            defaults={
                "name": "Dallas",
                "zip": "75201",
                "state": state_tx,
                "created_by": staff_user,
                "updated_by": staff_user,
            },
        )
        store_102, _ = Retailer.objects.get_or_create(
            tenant=suncrest,
            name="FreshMart #102",
            defaults={
                "address": "500 Congress Ave",
                "location": loc_austin,
                "created_by": suncrest_admin,
                "updated_by": suncrest_admin,
            },
        )
        store_205, _ = Retailer.objects.get_or_create(
            tenant=suncrest,
            name="FreshMart #205",
            defaults={
                "address": "900 Main St",
                "location": loc_dallas,
                "created_by": suncrest_admin,
                "updated_by": suncrest_admin,
            },
        )
        pt_sun, _ = ProductType.objects.get_or_create(
            tenant=suncrest,
            name="Sparkling Botanicals",
            defaults={"created_by": suncrest_admin, "updated_by": suncrest_admin},
        )
        sku_yuzu, _ = Product.objects.get_or_create(
            tenant=suncrest,
            name="Yuzu Citrus 12oz",
            defaults={
                "product_type": pt_sun,
                "created_by": suncrest_admin,
                "updated_by": suncrest_admin,
            },
        )
        sku_berry, _ = Product.objects.get_or_create(
            tenant=suncrest,
            name="Wild Berry 12oz",
            defaults={
                "product_type": pt_sun,
                "created_by": suncrest_admin,
                "updated_by": suncrest_admin,
            },
        )
        prog_retail, _ = EventType.objects.get_or_create(
            tenant=suncrest,
            slug="retail-sampling",
            defaults={
                "name": "Retail Sampling",
                "is_default": True,
                "created_by": suncrest_admin,
                "updated_by": suncrest_admin,
            },
        )

        week_monday, week_sunday = _current_month_anchor()
        day_wed = week_monday + timedelta(days=2)
        day_thu = week_monday + timedelta(days=3)

        sk_102, lbl_102, ck_102, cl_102 = store_identity(
            "FreshMart #102", "500 Congress Ave", "Austin", "TX"
        )
        sk_205, lbl_205, ck_205, cl_205 = store_identity(
            "FreshMart #205", "900 Main St", "Dallas", "TX"
        )

        # Scenario 1: Scheduled morning visit at FreshMart #102 + Agency 1 & Agency 2 CSVs
        cur_tz = timezone.get_current_timezone()
        morning_start = timezone.make_aware(
            datetime.combine(day_wed, time(11, 0)), cur_tz
        )
        morning_end = timezone.make_aware(
            datetime.combine(day_wed, time(15, 0)), cur_tz
        )
        morning_visit, _ = CanonicalVisit.objects.get_or_create(
            tenant=suncrest,
            store_key=sk_102,
            visit_date=day_wed,
            shift_key="11:00-15:00",
            defaults={
                "retailer": store_102,
                "event_type": prog_retail,
                "primary_provider": agency_fp,
                "store_label": lbl_102,
                "chain_key": ck_102,
                "chain_label": cl_102,
                "store_number": "102",
                "address": "500 Congress Ave",
                "city": "Austin",
                "state_code": "TX",
                "market": "Austin",
                "start_time": morning_start,
                "end_time": morning_end,
                "status": CanonicalVisit.STATUS_SCHEDULED,
            },
        )

        csv_fp_v1 = (
            "Event ID,Activation Date,Start Time,End Time,Retail Banner,Store #,Store Address,City,State,Program,SKU Sampled,Cans Sampled,Consumers Engaged,Units Sold Observed,Rep Notes\n"
            f"FP-1001,{day_wed.isoformat()},11:00,15:00,FreshMart,102,500 Congress Ave,Austin,TX,Retail Sampling,Yuzu Citrus 12oz,80,65,14,Morning crew lead\n"
            f"FP-1002,{day_wed.isoformat()},16:00,20:00,FreshMart,102,500 Congress Ave,Austin,TX,Retail Sampling,Yuzu Citrus 12oz,90,72,19,Evening rush shift\n"
        )
        commit_provider_import(
            access_fp,
            provider_uuid=str(agency_fp.uuid),
            csv_text=csv_fp_v1,
            filename="fieldpulse-demo-shifts.csv",
            preset_key="agency_fieldpulse",
            save_mapping_name="FieldPulse Standard",
        )

        csv_pw_v1 = (
            "shift_ref,shift_date,window_start,window_end,account_name,location_code,street,town,region,tactic_name,product_code,samples_out,shopper_count,bottles_sold,summary\n"
            f"PW-8801,{day_wed.strftime('%m/%d/%Y')},11:00 AM,03:00 PM,FreshMart,102,500 Congress Ave,Austin,TX,Retail Sampling,Yuzu Citrus 12oz,40,35,8,Co-staffed tasting bar\n"
        )
        commit_provider_import(
            access_pw,
            provider_uuid=str(agency_pw.uuid),
            csv_text=csv_pw_v1,
            filename="promoworks-demo-shifts.csv",
            preset_key="agency_promoworks",
            save_mapping_name="PromoWorks Standard",
        )

        # Single-row correction on FP-1001 preserving v1 in version history
        csv_fp_corrected = (
            "Event ID,Activation Date,Start Time,End Time,Retail Banner,Store #,Store Address,City,State,Program,SKU Sampled,Cans Sampled,Consumers Engaged,Units Sold Observed,Rep Notes\n"
            f"FP-1001,{day_wed.isoformat()},11:00,15:00,FreshMart,102,500 Congress Ave,Austin,TX,Retail Sampling,Yuzu Citrus 12oz,85,68,16,Corrected count from tally sheet\n"
        )
        commit_provider_import(
            access_fp,
            provider_uuid=str(agency_fp.uuid),
            csv_text=csv_fp_corrected,
            filename="fieldpulse-fp1001-correction.csv",
            preset_key="agency_fieldpulse",
        )

        # Keep 1 uncertain SKU row in review queue so /provider-imports shows live review resolution
        if not ProviderImportJob.objects.filter(
            tenant=suncrest, filename="promoworks-needs-review.csv"
        ).exists():
            csv_uncertain = (
                "shift_ref,shift_date,account_name,location_code,street,town,region,product_code,samples_out,shopper_count,bottles_sold\n"
                f"PW-9900,{day_thu.strftime('%m/%d/%Y')},FreshMart,205,900 Main St,Dallas,TX,YUZU-PROMO-CODE,60,50,11\n"
            )
            commit_provider_import(
                access_pw,
                provider_uuid=str(agency_pw.uuid),
                csv_text=csv_uncertain,
                filename="promoworks-needs-review.csv",
                preset_key="agency_promoworks",
            )

        evening_visit = (
            CanonicalVisit.objects.filter(
                tenant=suncrest, store_key=sk_102, visit_date=day_wed
            )
            .exclude(pk=morning_visit.pk)
            .first()
        )

        # Scenario 2: Reconcile $290.00 demo on morning_visit + unitemized parking exception on visit_205
        ev_102_dt = morning_start
        ev_102, _ = Event.objects.get_or_create(
            tenant=suncrest,
            name="FreshMart #102 Morning Demo",
            date=ev_102_dt,
            defaults={
                "event_type": prog_retail,
                "retailer": store_102,
                "start_time": ev_102_dt,
                "end_time": morning_end,
                "address": "500 Congress Ave",
                "created_by": suncrest_admin,
                "updated_by": suncrest_admin,
            },
        )
        if morning_visit.event_id != ev_102.id:
            morning_visit.event = ev_102
            morning_visit.save(update_fields=["event", "updated_at"])

        tmpl_sun, _ = CustomRecapTemplate.objects.get_or_create(
            tenant=suncrest,
            event_type=prog_retail,
            name="Suncrest Retail Sampling Recap",
            defaults={"created_by": suncrest_admin, "updated_by": suncrest_admin},
        )
        recap_102, _ = CustomRecap.objects.get_or_create(
            tenant=suncrest,
            event=ev_102,
            name="FreshMart #102 Morning Recap",
            defaults={
                "custom_recap_template": tmpl_sun,
                "approved": False,
                "total_engagements": 103,
                "created_by": suncrest_admin,
                "updated_by": suncrest_admin,
            },
        )
        ft_jpg, _ = FileType.objects.get_or_create(
            name="image/jpeg",
            defaults={"created_by": staff_user, "updated_by": staff_user},
        )
        recap_file_102, _ = CustomRecapFile.objects.get_or_create(
            custom_recap=recap_102,
            name="freshmart-102-itemized-receipt.jpg",
            defaults={
                "file_type": ft_jpg,
                "created_by": suncrest_admin,
                "updated_by": suncrest_admin,
            },
        )
        reading_102, _ = ReceiptReading.objects.update_or_create(
            custom_recap_file=recap_file_102,
            defaults={
                "status": ReceiptReading.STATUS_READ,
                "total": Decimal("40.00"),
                "merchant": "FreshMart #102",
                "line_items": [
                    {"name": "Suncrest Yuzu Citrus 12pk", "qty": 2, "amount": "40.00"}
                ],
            },
        )
        RecapFlag.objects.get_or_create(
            tenant=suncrest,
            custom_recap=recap_102,
            kind=RecapFlag.KIND_SHORT_CLOCK,
            defaults={
                "severity": RecapFlag.SEVERITY_MEDIUM,
                "explanation": "Rep check-in GPS drifted 180m indoors (operational advisory only).",
            },
        )

        agreement_fp = upsert_provider_agreement(
            access_sun_finance,
            provider_uuid=str(agency_fp.uuid),
            name="FieldPulse 2026 Rate Card",
            contracted_demo_fee=Decimal("250.00"),
            admin_fee_percent=Decimal("5.00"),
            admin_fee_bases=[BrandCostLine.CAT_PRODUCT],
        )

        inv_sun, _ = Invoice.objects.get_or_create(
            tenant=suncrest,
            notes="DEMO-SCENARIO-2-INVOICE",
            defaults={"status": Invoice.STATUS_SENT},
        )
        inv_line_prod, _ = InvoiceLineItem.objects.get_or_create(
            invoice=inv_sun,
            event=ev_102,
            description="Product reimbursement (2x Yuzu 12pk)",
            defaults={
                "quantity": Decimal("1"),
                "unit_price": Decimal("40.00"),
                "amount": Decimal("40.00"),
            },
        )
        recompute_invoice_totals(inv_sun, [inv_line_prod])
        inv_sun.save()

        # Idempotent $290.00 cost structure on morning_visit ($250 + $40 + $10 + $2 admin - $12 credit = $290.00)
        BrandCostLine.objects.filter(canonical_visit=morning_visit).delete()
        upsert_brand_cost_line(
            access_sun_finance,
            visit_uuid=str(morning_visit.uuid),
            category=BrandCostLine.CAT_DEMO_FEE,
            source_type=BrandCostLine.SOURCE_AGREEMENT,
            amount=Decimal("250.00"),
            settlement_status=BrandCostLine.SETTLEMENT_INVOICED,
            evidence_status=BrandCostLine.EVIDENCE_SUPPORTED,
            agreement_uuid=str(agreement_fp.uuid),
        )
        prod_line = upsert_brand_cost_line(
            access_sun_finance,
            visit_uuid=str(morning_visit.uuid),
            category=BrandCostLine.CAT_PRODUCT,
            source_type=BrandCostLine.SOURCE_INVOICE_LINE,
            amount=Decimal("40.00"),
            settlement_status=BrandCostLine.SETTLEMENT_INVOICED,
            evidence_status=BrandCostLine.EVIDENCE_MISSING,
            invoice_line_id=inv_line_prod.id,
            agreement_uuid=str(agreement_fp.uuid),
        )
        upsert_brand_cost_line(
            access_sun_finance,
            visit_uuid=str(morning_visit.uuid),
            category=BrandCostLine.CAT_MILEAGE,
            source_type=BrandCostLine.SOURCE_INVOICE_LINE,
            amount=Decimal("10.00"),
            settlement_status=BrandCostLine.SETTLEMENT_INVOICED,
            evidence_status=BrandCostLine.EVIDENCE_SUPPORTED,
            agreement_uuid=str(agreement_fp.uuid),
        )
        upsert_brand_cost_line(
            access_sun_finance,
            visit_uuid=str(morning_visit.uuid),
            category=BrandCostLine.CAT_CREDIT,
            source_type=BrandCostLine.SOURCE_CREDIT,
            amount=Decimal("12.00"),
            settlement_status=BrandCostLine.SETTLEMENT_INVOICED,
            evidence_status=BrandCostLine.EVIDENCE_SUPPORTED,
            agreement_uuid=str(agreement_fp.uuid),
        )
        link_receipt_evidence_to_cost_line(
            access_sun_finance,
            cost_line_uuid=str(prod_line.uuid),
            recap_file_id=recap_file_102.id,
            receipt_reading_id=reading_102.id,
            label="FreshMart #102 itemized register receipt ($40.00)",
        )

        if evening_visit is not None:
            BrandCostLine.objects.filter(canonical_visit=evening_visit).delete()
            upsert_brand_cost_line(
                access_sun_finance,
                visit_uuid=str(evening_visit.uuid),
                category=BrandCostLine.CAT_DEMO_FEE,
                source_type=BrandCostLine.SOURCE_AGREEMENT,
                amount=Decimal("0.00"),
                settlement_status=BrandCostLine.SETTLEMENT_INVOICED,
                evidence_status=BrandCostLine.EVIDENCE_SUPPORTED,
                description="Included second tasting window in Austin day rate",
            )

        # Store #205 completed demo + open $15.00 unitemized parking slip exception
        visit_205, _ = CanonicalVisit.objects.get_or_create(
            tenant=suncrest,
            store_key=sk_205,
            visit_date=day_thu,
            shift_key="12:00-16:00",
            defaults={
                "primary_provider": agency_fp,
                "retailer": store_205,
                "event_type": prog_retail,
                "store_label": lbl_205,
                "chain_key": ck_205,
                "chain_label": cl_205,
                "store_number": "205",
                "address": "900 Main St",
                "city": "Dallas",
                "state_code": "TX",
                "market": "Dallas",
                "status": CanonicalVisit.STATUS_COMPLETED,
                "samples_distributed": 64,
                "consumers_sampled": 52,
                "rep_observed_units": 9,
            },
        )
        VisitSubmissionSource.objects.update_or_create(
            tenant=suncrest,
            canonical_visit=visit_205,
            provider=agency_fp,
            provider_event_id="FP-2050",
            defaults={
                "source_kind": VisitSubmissionSource.KIND_PROVIDER_CSV,
                "recap_status": VisitSubmissionSource.RECAP_APPROVED,
                "samples_distributed": 64,
                "consumers_sampled": 52,
                "rep_observed_units": 9,
                "sku_breakdown": [
                    {"product_id": sku_yuzu.id, "product_name": sku_yuzu.name},
                    {"product_id": sku_berry.id, "product_name": sku_berry.name},
                ],
            },
        )
        BrandCostLine.objects.filter(canonical_visit=visit_205).exclude(
            source_type=BrandCostLine.SOURCE_ALLOCATION
        ).delete()
        parking_205 = upsert_brand_cost_line(
            access_sun_finance,
            visit_uuid=str(visit_205.uuid),
            category=BrandCostLine.CAT_PARKING,
            source_type=BrandCostLine.SOURCE_EXPENSE_RECEIPT,
            amount=Decimal("15.00"),
            settlement_status=BrandCostLine.SETTLEMENT_INVOICED,
            description="Downtown Dallas garage slip (summary only)",
        )
        link_receipt_evidence_to_cost_line(
            access_sun_finance,
            cost_line_uuid=str(parking_205.uuid),
            proof_url="https://client.igniteproductions.co/demo/dallas-parking-summary-slip.jpg",
            label="Credit card summary slip ($15.00, unitemized)",
            verified_amount=Decimal("15.00"),
            is_itemized=False,
        )

        # ------------------------------------------------------------------
        # 2. Harbor Spritz Co. (Active Pro via Stripe Test Mode + 25% Co-Brand Share)
        # ------------------------------------------------------------------
        harbor, _ = Tenant.objects.get_or_create(
            slug="harbor-spritz-demo",
            defaults={
                "name": "Harbor Spritz Co.",
                "request_url_name": "harbor-spritz-demo",
                "created_by": harbor_admin,
                "updated_by": harbor_admin,
            },
        )
        harbor_profile = ensure_workspace_profile(
            harbor,
            mode=WorkspaceProfile.MODE_SELF_SERVE,
            plan_tier=WorkspaceProfile.PLAN_FREE,
        )
        harbor_profile.business_timezone = "America/Chicago"
        harbor_profile.save()
        TenantedUser.objects.get_or_create(
            user=harbor_admin,
            tenant=harbor,
            defaults={"created_by": harbor_admin, "updated_by": harbor_admin},
        )
        staff_access_harbor = resolve_workspace_access(staff_user, tenant=harbor)
        upsert_workspace_membership(
            staff_access_harbor,
            target_user=harbor_admin,
            workspace_role=WorkspaceMembership.ROLE_BRAND_ADMIN,
        )
        access_harbor = resolve_workspace_access(harbor_admin, tenant=harbor)
        harbor_sub = ensure_brand_subscription(harbor)
        if harbor_sub.status != BrandSubscription.STATUS_ACTIVE:
            chk_harbor = create_subscription_checkout_session(
                access_harbor,
                target_plan_code=WorkspaceProfile.PLAN_PRO,
                explicit_terms_accepted=True,
                success_url="http://localhost:3000/plans-billing",
                cancel_url="http://localhost:3000/plans-billing",
            )
            complete_test_mode_checkout(
                access_harbor,
                stripe_session_id=chk_harbor.stripe_session_id,
            )

        visit_harbor_co, _ = CanonicalVisit.objects.get_or_create(
            tenant=harbor,
            store_key=sk_205,
            visit_date=day_thu,
            shift_key="12:00-16:00",
            defaults={
                "store_label": lbl_205,
                "chain_key": ck_205,
                "chain_label": cl_205,
                "store_number": "205",
                "address": "900 Main St",
                "city": "Dallas",
                "state_code": "TX",
                "market": "Dallas",
                "status": CanonicalVisit.STATUS_COMPLETED,
                "samples_distributed": 48,
                "consumers_sampled": 39,
                "rep_observed_units": 11,
            },
        )

        if not BrandCostLine.objects.filter(
            canonical_visit=visit_205,
            source_type=BrandCostLine.SOURCE_ALLOCATION,
        ).exists():
            allocate_shared_cost(
                actor=staff_user,
                total_amount=Decimal("40.00"),
                category=BrandCostLine.CAT_PRODUCT,
                allocations=[
                    {"visit": visit_205, "percentage": Decimal("75")},
                    {"visit": visit_harbor_co, "percentage": Decimal("25")},
                ],
                allocation_mode="percentage",
                description="Co-branded tasting bar ice & citrus garnish receipt",
            )

        evaluate_finance_exceptions(suncrest)

        # ------------------------------------------------------------------
        # Scenario 3: Retailer POS CSV (122 reg - 2 crew = 120 adj vs 100 base = +20 lift!)
        # ------------------------------------------------------------------
        if not CrewPurchaseAdjustment.objects.filter(
            canonical_visit=morning_visit
        ).exists():
            record_crew_purchase_adjustment(
                access_sun_admin,
                visit_uuid=str(morning_visit.uuid),
                product_id=sku_yuzu.id,
                units_purchased=2,
                purchase_date=day_wed,
                retailer_banner="FreshMart",
                transaction_ref="TXN-CREW-102",
            )

        if not RetailerSalesImport.objects.filter(
            tenant=suncrest, is_active_version=True
        ).exists():
            pos_csv = (
                "retailer,store_id,store_name,city,state,sku,week_start,week_end,register_units,baseline_units,baseline_weeks\n"
                f"FreshMart,102,FreshMart #102,Austin,TX,Yuzu Citrus 12oz,{week_monday.isoformat()},{week_sunday.isoformat()},122,100,4\n"
                f"FreshMart,205,FreshMart #205,Dallas,TX,Yuzu Citrus 12oz,{week_monday.isoformat()},{week_sunday.isoformat()},0,12,4\n"
                f"FreshMart,102,FreshMart #102,Austin,TX,UNMAPPED-SEASONAL-SKU,{week_monday.isoformat()},{week_sunday.isoformat()},15,10,4\n"
            )
            import_retailer_pos_csv(
                access_sun_admin,
                retailer_banner="FreshMart",
                csv_text=pos_csv,
                filename="freshmart-weekly-pos.csv",
            )

        # ------------------------------------------------------------------
        # Scenario 4: Published BrandInsight Recommendations -> Rebook, Hold, Verified Restock
        # ------------------------------------------------------------------
        ins_top, _ = BrandInsight.objects.update_or_create(
            tenant=suncrest,
            kind=BrandInsight.KIND_RECOMMENDATION,
            code="top_store",
            scope_key=f"store:{sk_102}",
            period_start=week_monday,
            period_end=week_sunday,
            defaults={
                "subject": lbl_102,
                "template": "Rebook top velocity store",
                "body": "FreshMart #102 delivered +20.00 incremental POS lift units (+37.9% ROI) with 125 samples across reconciled shifts. Rebook weekend endcap demo.",
                "facts": {"visits": 2, "units": 120, "lift_units": 20},
                "status": BrandInsight.STATUS_PUBLISHED,
                "generated_at": timezone.now(),
            },
        )
        ins_chain, _ = BrandInsight.objects.update_or_create(
            tenant=suncrest,
            kind=BrandInsight.KIND_ALERT,
            code="chain_stockout_watch",
            scope_key=f"chain:{ck_102}",
            period_start=week_monday,
            period_end=week_sunday,
            defaults={
                "subject": "FreshMart",
                "template": "Chain shelf-availability alert",
                "body": "FreshMart #205 recorded 0 register units on Yuzu Citrus 12oz; verify backroom restock before scheduling next tasting.",
                "facts": {"stores": 2, "zero_velocity_stores": 1},
                "status": BrandInsight.STATUS_PUBLISHED,
                "generated_at": timezone.now(),
            },
        )

        # Rebook on a week AFTER week_sunday so #102's POS week keeps its exact $290.00 / +37.9% ROI!
        rebook_date = week_sunday + timedelta(days=2)
        rebook_act = create_store_recommendation_action(
            access_sun_admin,
            finding_id=f"BrandInsight:{ins_top.id}",
            action_type=StoreRecommendationAction.ACTION_REBOOK,
            provider_uuid=str(agency_pw.uuid),
            target_date=rebook_date,
            budget_amount=Decimal("265.00"),
            idempotency_key="demo-seed-rebook-102",
        )
        if rebook_act.status == StoreRecommendationAction.STATUS_ASSIGNED:
            respond_store_action(
                access_pw,
                action_uuid=str(rebook_act.uuid),
                decision="accepted",
                note="PromoWorks weekend lead confirmed for return endcap.",
            )
            return_recap, _ = CustomRecap.objects.get_or_create(
                tenant=suncrest,
                event=rebook_act.outcome_visit.event,
                name="FreshMart #102 Rebook Return Recap",
                defaults={
                    "custom_recap_template": tmpl_sun,
                    "approved": True,
                    "total_engagements": 94,
                    "created_by": suncrest_admin,
                    "updated_by": suncrest_admin,
                },
            )
            rebook_act = complete_rebook_return_visit(
                access_pw,
                action_uuid=str(rebook_act.uuid),
                samples_distributed=110,
                consumers_sampled=94,
                rep_observed_units=34,
                custom_recap_id=return_recap.id,
                notes="Sold out Yuzu endcap display during return visit.",
            )
            upsert_brand_cost_line(
                access_sun_finance,
                visit_uuid=str(rebook_act.outcome_visit.uuid),
                category=BrandCostLine.CAT_DEMO_FEE,
                source_type=BrandCostLine.SOURCE_INVOICE_LINE,
                amount=Decimal("265.00"),
                settlement_status=BrandCostLine.SETTLEMENT_INVOICED,
                evidence_status=BrandCostLine.EVIDENCE_SUPPORTED,
            )

        restock_act = create_store_recommendation_action(
            access_sun_admin,
            finding_id=f"BrandInsight:{ins_chain.id}",
            selected_store_key=sk_205,
            action_type=StoreRecommendationAction.ACTION_RESTOCK,
            provider_uuid=str(agency_fp.uuid),
            due_date=week_sunday + timedelta(days=3),
            idempotency_key="demo-seed-restock-205",
        )
        if restock_act.status != StoreRecommendationAction.STATUS_COMPLETED:
            verify_restock_action(
                access_fp,
                action_uuid=str(restock_act.uuid),
                verification_method=StoreRecommendationAction.VERIFY_NOTE,
                verification_note="Distributor rep packed out 4 cases of Yuzu Citrus 12oz on eye-level shelf.",
            )

        create_store_recommendation_action(
            access_sun_admin,
            finding_id=f"BrandInsight:{ins_chain.id}",
            selected_store_key=sk_205,
            action_type=StoreRecommendationAction.ACTION_HOLD,
            hold_reason="Pause paid sampling at #205 until next week's POS confirms shelf pull-through.",
            next_review_date=week_sunday + timedelta(days=7),
            idempotency_key="demo-seed-hold-205",
        )

        # ------------------------------------------------------------------
        # 3. Canyon Spritz Co. (Scenario 5: Self-Serve Free Tier at 5/5 Cap + Active Field Shift)
        # ------------------------------------------------------------------
        canyon_res = start_or_resume_self_serve_onboarding(
            email="canyon-admin@demo.spark",
            password=DEMO_PASSWORD,
            first_name="Riley",
            last_name="Canyon",
            brand_name="Canyon Spritz Co.",
            business_timezone="America/Chicago",
            accept_terms=True,
            client_ip="127.0.0.1",
            user_agent="seed_standalone_demo",
        )
        canyon_user = canyon_res["user"]
        canyon_user.set_password(DEMO_PASSWORD)
        canyon_user.save(update_fields=["password"])
        canyon_tenant = canyon_res["tenant"]
        access_canyon = resolve_workspace_access(canyon_user, tenant=canyon_tenant)

        canyon_progress = select_onboarding_starter_program(
            access_canyon,
            starter_key="retail_sampling",
            starter_sku_name="Blood Orange Spritz 12oz",
        )

        # Pre-create 1 already-scheduled in-progress shift BEFORE hitting the 5/5 monthly cap
        # so field crew can still submit & complete its recap at cap!
        open_shift, _ = CanonicalVisit.objects.get_or_create(
            tenant=canyon_tenant,
            store_key="sunset-market-19-austin-tx",
            visit_date=week_monday + timedelta(days=5),
            shift_key="13:00-17:00",
            defaults={
                "primary_provider": canyon_progress.default_provider,
                "event_type": canyon_progress.starter_event_type,
                "store_label": "Sunset Market #19",
                "chain_key": "sunset-market",
                "chain_label": "Sunset Market",
                "store_number": "19",
                "address": "4200 S Congress Ave",
                "city": "Austin",
                "state_code": "TX",
                "market": "Austin",
                "status": CanonicalVisit.STATUS_IN_PROGRESS,
            },
        )
        if canyon_progress.default_provider_id:
            ProviderAssignment.objects.get_or_create(
                tenant=canyon_tenant,
                canonical_visit=open_shift,
                provider=canyon_progress.default_provider,
                defaults={
                    "scope_type": ProviderAssignment.SCOPE_VISIT,
                    "status": ProviderAssignment.STATUS_ACCEPTED,
                    "submission_code": _mint_submission_code(),
                    "created_by": canyon_user,
                },
            )

        canyon_completed = CanonicalVisit.objects.filter(
            tenant=canyon_tenant,
            status=CanonicalVisit.STATUS_COMPLETED,
            visit_date__gte=week_monday.replace(day=1),
        ).count()

        canyon_stores = [
            ("Sunset Market #11", "Austin", "TX", "101 S Lamar Blvd"),
            ("Sunset Market #14", "Austin", "TX", "2400 Guadalupe St"),
            ("GreenBasket #04", "San Antonio", "TX", "810 Broadway"),
            ("GreenBasket #09", "Houston", "TX", "1900 Westheimer Rd"),
            ("Mesa Natural Foods #02", "Dallas", "TX", "3100 Knox St"),
        ]
        for idx in range(canyon_completed, 5):
            st_name, city_n, st_code, addr = canyon_stores[idx]
            v_date = week_monday + timedelta(days=min(idx, 5))
            visit_obj, _ = schedule_standalone_visit(
                access_canyon,
                store_name=st_name,
                visit_date=v_date,
                city=city_n,
                state_code=st_code,
                address=addr,
            )
            VisitSubmissionSource.objects.update_or_create(
                tenant=canyon_tenant,
                canonical_visit=visit_obj,
                provider=visit_obj.primary_provider,
                provider_event_id=f"CANYON-VISIT-{idx + 1}",
                defaults={
                    "source_kind": VisitSubmissionSource.KIND_WALKUP_CHECKIN,
                    "recap_status": VisitSubmissionSource.RECAP_SUBMITTED,
                    "samples_distributed": 72 + idx * 5,
                    "consumers_sampled": 58 + idx * 4,
                    "rep_observed_units": 14 + idx * 2,
                    "notes": f"Strong citrus repeat taste conversion at {st_name}.",
                },
            )
            visit_obj.samples_distributed = 72 + idx * 5
            visit_obj.consumers_sampled = 58 + idx * 4
            visit_obj.rep_observed_units = 14 + idx * 2
            visit_obj.save(
                update_fields=[
                    "samples_distributed",
                    "consumers_sampled",
                    "rep_observed_units",
                    "updated_at",
                ]
            )
            approve_canonical_visit_recap(
                access_canyon,
                visit_uuid=str(visit_obj.uuid),
            )
        for idx, cv in enumerate(
            CanonicalVisit.objects.filter(
                tenant=canyon_tenant, status=CanonicalVisit.STATUS_COMPLETED
            ).order_by("visit_date", "id")
        ):
            if not cv.samples_distributed:
                cv.samples_distributed = 72 + idx * 5
                cv.consumers_sampled = 58 + idx * 4
                cv.rep_observed_units = 14 + idx * 2
                cv.save(
                    update_fields=[
                        "samples_distributed",
                        "consumers_sampled",
                        "rep_observed_units",
                        "updated_at",
                    ]
                )

        # ------------------------------------------------------------------
        # 4. Cascade Cold Brew (Existing Ignite-Managed SOW Client — Uncapped)
        # ------------------------------------------------------------------
        cascade, _ = Tenant.objects.get_or_create(
            slug="cascade-cold-brew-sow",
            defaults={
                "name": "Cascade Cold Brew (Managed SOW)",
                "request_url_name": "cascade-cold-brew-sow",
                "created_by": staff_user,
                "updated_by": staff_user,
            },
        )
        cascade_profile = ensure_workspace_profile(
            cascade,
            mode=WorkspaceProfile.MODE_MANAGED,
            plan_tier=WorkspaceProfile.PLAN_MANAGED_SOW,
        )
        cascade_profile.business_timezone = "America/Chicago"
        cascade_profile.save()
        TenantedUser.objects.get_or_create(
            user=sow_client,
            tenant=cascade,
            defaults={"created_by": staff_user, "updated_by": staff_user},
        )
        ensure_brand_subscription(cascade)

        canyon_progress.refresh_from_db()
        summary_payload = {
            "password": DEMO_PASSWORD,
            "accounts": {
                "suncrest_pro_pilot_admin": "suncrest-admin@demo.spark",
                "suncrest_finance_reviewer": "suncrest-finance@demo.spark",
                "suncrest_recap_reviewer": "suncrest-reviewer@demo.spark",
                "fieldpulse_agency_manager": "fieldpulse@demo.spark",
                "promoworks_agency_manager": "promoworks@demo.spark",
                "canyon_free_at_cap_admin": "canyon-admin@demo.spark",
                "harbor_active_pro_admin": "harbor-admin@demo.spark",
                "cascade_managed_sow_client": "managed-sow@demo.spark",
                "ignite_ops_admin": "ignite-ops@demo.spark",
            },
            "suncrest_tenant_slug": suncrest.slug,
            "canyon_tenant_slug": canyon_tenant.slug,
            "canyon_mobile_code": (
                canyon_progress.starter_assignment.submission_code
                if canyon_progress.starter_assignment
                else None
            ),
            "canyon_shared_token": (
                canyon_progress.first_shared_link.token
                if canyon_progress.first_shared_link
                else None
            ),
        }
        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded 5-scenario standalone demo workspaces:\n{json.dumps(summary_payload, indent=2)}"
            )
        )
