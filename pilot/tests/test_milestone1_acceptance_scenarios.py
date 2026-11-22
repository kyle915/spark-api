"""End-to-end Acceptance Scenarios 1-4 for Standalone Spark Milestone 1 using
fictional brands, agencies, stores, and products."""

from __future__ import annotations

import base64
import io
from datetime import date, datetime, time
from decimal import Decimal
from unittest.mock import patch

import openpyxl
from django.test import TestCase
from django.utils import timezone
from gqlauth.models import UserStatus

from billing.models import Invoice, InvoiceLineItem, recompute_invoice_totals
from brand_insights.models import BrandInsight
from brand_insights.service import generate_for_tenant
from events.models import Event, EventType, Location, Product, ProductType, Retailer, State
from utils.ai_text import AiUnavailable
from pilot.actions import (
    build_weekly_buyer_store_report,
    complete_rebook_return_visit,
    create_store_recommendation_action,
    export_weekly_buyer_report_csv,
    export_weekly_buyer_report_xlsx_base64,
    reassign_store_action_provider,
    respond_store_action,
    verify_restock_action,
)
from pilot.costs import (
    allocate_shared_cost,
    build_brand_cost_rollups,
    compute_visit_cost_summary,
    link_receipt_evidence_to_cost_line,
    upsert_brand_cost_line,
    upsert_provider_agreement,
)
from pilot.finance import (
    evaluate_finance_exceptions,
    list_finance_exceptions,
    list_operational_field_notices,
    resolve_finance_exception,
)
from pilot.imports import (
    brand_included_visits_queryset,
    commit_provider_import,
    preview_provider_import,
    resolve_provider_import_row,
)
from pilot.models import (
    BrandCostLine,
    CanonicalVisit,
    FinanceException,
    ProviderImportRow,
    StoreRecommendationAction,
    VisitSubmissionSource,
    WorkspaceMembership,
)
from pilot.permissions import resolve_workspace_access
from pilot.pos import (
    RETAILER_UNAVAILABLE_MESSAGE,
    build_retailer_pos_measurement,
    import_retailer_pos_csv,
    record_crew_purchase_adjustment,
)
from pilot.workspaces import (
    create_or_update_provider,
    ensure_workspace_profile,
    set_provider_source_inclusion,
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
from tenants.models import Role, Tenant, User


from tenants.tests.base import ensure_role


def _mk_user(email: str, role_slug: str = Role.CLIENT_SLUG) -> User:
    role = ensure_role(role_slug.replace("-", " ").title(), slug=role_slug)
    u = User.objects.create_user(
        email=email,
        username=email,
        password="test-password-123",
        first_name=email.split("@")[0].title(),
        last_name="Pilot",
        role=role,
    )
    UserStatus.objects.update_or_create(user=u, defaults={"verified": True})
    return u


class Milestone1AcceptanceScenariosTests(TestCase):
    def setUp(self):
        self.staff = _mk_user("staff@ignite-test.example", Role.SPARK_ADMIN_SLUG)
        self.brand_a = Tenant.objects.create(
            name="Suncrest Sparkling",
            slug="suncrest-pilot",
            created_by=self.staff,
        )
        self.brand_b = Tenant.objects.create(
            name="Cascade Tonics",
            slug="cascade-pilot",
            created_by=self.staff,
        )

        self.admin_a = _mk_user("claire@suncrest.example")
        self.reviewer_no_spend = _mk_user("ops-reviewer@suncrest.example")
        self.finance_a = _mk_user("controller@suncrest.example")
        self.admin_b = _mk_user("ben@cascade.example")
        self.ag1_user = _mk_user("lead@fieldpulse.example")
        self.ag2_user = _mk_user("lead@promoworks.example")

        staff_a = resolve_workspace_access(self.staff, tenant=self.brand_a)
        staff_b = resolve_workspace_access(self.staff, tenant=self.brand_b)

        upsert_workspace_membership(
            staff_a,
            target_user=self.admin_a,
            workspace_role=WorkspaceMembership.ROLE_BRAND_ADMIN,
        )
        upsert_workspace_membership(
            staff_b,
            target_user=self.admin_b,
            workspace_role=WorkspaceMembership.ROLE_BRAND_ADMIN,
        )

        self.access_a = resolve_workspace_access(
            self.admin_a, tenant=self.brand_a
        )
        self.access_b = resolve_workspace_access(
            self.admin_b, tenant=self.brand_b
        )

        upsert_workspace_membership(
            self.access_a,
            target_user=self.reviewer_no_spend,
            workspace_role=WorkspaceMembership.ROLE_BRAND_REVIEWER,
            can_approve_recaps=True,
            can_approve_spend=False,
        )
        upsert_workspace_membership(
            self.access_a,
            target_user=self.finance_a,
            workspace_role=WorkspaceMembership.ROLE_FINANCE_REVIEWER,
            can_approve_finance=True,
        )
        self.access_reviewer_no_spend = resolve_workspace_access(
            self.reviewer_no_spend, tenant=self.brand_a
        )
        self.access_finance_a = resolve_workspace_access(
            self.finance_a, tenant=self.brand_a
        )

        self.agency_1 = create_or_update_provider(
            self.access_a, name="FieldPulse Agency"
        )
        self.agency_2 = create_or_update_provider(
            self.access_a, name="PromoWorks Agency"
        )
        upsert_workspace_membership(
            self.access_a,
            target_user=self.ag1_user,
            workspace_role=WorkspaceMembership.ROLE_PROVIDER_MANAGER,
            provider=self.agency_1,
        )
        upsert_workspace_membership(
            self.access_a,
            target_user=self.ag2_user,
            workspace_role=WorkspaceMembership.ROLE_PROVIDER_MANAGER,
            provider=self.agency_2,
        )
        self.access_ag1 = resolve_workspace_access(
            self.ag1_user, tenant=self.brand_a
        )
        self.access_ag2 = resolve_workspace_access(
            self.ag2_user, tenant=self.brand_a
        )

        # Seed canonical State, Location, Retailers, and Products for Brand A.
        self.state_tx = State.objects.create(
            name="Texas", code="TX", created_by=self.admin_a
        )
        self.loc_austin = Location.objects.create(
            name="Austin",
            code="AUS",
            zip="78701",
            state=self.state_tx,
            created_by=self.admin_a,
        )
        self.loc_dallas = Location.objects.create(
            name="Dallas",
            code="DAL",
            zip="75201",
            state=self.state_tx,
            created_by=self.admin_a,
        )
        self.store_102 = Retailer.objects.create(
            tenant=self.brand_a,
            name="FreshMart #102",
            address="500 Congress Ave",
            location=self.loc_austin,
            created_by=self.admin_a,
        )
        self.store_205 = Retailer.objects.create(
            tenant=self.brand_a,
            name="FreshMart #205",
            address="900 Main St",
            location=self.loc_dallas,
            created_by=self.admin_a,
        )
        self.pt_a = ProductType.objects.create(
            tenant=self.brand_a, name="Sparkling Water", created_by=self.admin_a
        )
        self.sku_yuzu = Product.objects.create(
            tenant=self.brand_a,
            product_type=self.pt_a,
            name="Yuzu Citrus 12oz",
            created_by=self.admin_a,
        )
        self.sku_berry = Product.objects.create(
            tenant=self.brand_a,
            product_type=self.pt_a,
            name="Wild Berry 12oz",
            created_by=self.admin_a,
        )
        self.prog_retail = EventType.objects.create(
            tenant=self.brand_a,
            name="Retail Sampling",
            slug="retail-sampling",
            is_default=True,
            created_by=self.admin_a,
        )

    def test_scenario_1_two_agencies_one_visit_and_second_shift_idempotency(self):
        """Scenario 1:
        Brand A schedules one visit. Agency 1 and Agency 2 upload differently
        formatted files referencing that visit, plus a second shift on the same
        day at the same store. Both link to the one canonical visit with each
        provider's source data preserved; the second shift remains a distinct
        visit; re-uploading creates no duplicates; single-row correction updates
        cleanly with version history; uncertain match resolves in place."""
        morning_start = timezone.make_aware(
            datetime.combine(date(2026, 10, 10), time(11, 0)),
            timezone.get_current_timezone(),
        )
        morning_end = timezone.make_aware(
            datetime.combine(date(2026, 10, 10), time(15, 0)),
            timezone.get_current_timezone(),
        )
        from brand_insights.facts import store_identity

        s_key, s_label, c_key, c_label = store_identity(
            "FreshMart #102", "500 Congress Ave", "Austin", "TX"
        )
        scheduled_visit = CanonicalVisit.objects.create(
            tenant=self.brand_a,
            retailer=self.store_102,
            event_type=self.prog_retail,
            store_key=s_key,
            store_label=s_label,
            chain_key=c_key,
            chain_label=c_label,
            store_number="102",
            address="500 Congress Ave",
            city="Austin",
            state_code="TX",
            market="Austin",
            visit_date=date(2026, 10, 10),
            start_time=morning_start,
            end_time=morning_end,
            shift_key="11:00-15:00",
            status=CanonicalVisit.STATUS_SCHEDULED,
        )

        # Agency 1 CSV (FieldPulse layout): morning shift (11:00-15:00) +
        # distinct evening shift (16:00-20:00) on the same store & day!
        csv_agency_1 = (
            "Event ID,Activation Date,Start Time,End Time,Retail Banner,Store #,Store Address,City,State,Program,SKU Sampled,Cans Sampled,Consumers Engaged,Units Sold Observed,Rep Notes\n"
            "FP-1001,2026-10-10,11:00,15:00,FreshMart,102,500 Congress Ave,Austin,TX,Retail Sampling,Yuzu Citrus 12oz,80,65,14,Morning crew lead\n"
            "FP-1002,2026-10-10,16:00,20:00,FreshMart,102,500 Congress Ave,Austin,TX,Retail Sampling,Wild Berry 12oz,90,72,19,Evening rush shift\n"
        )

        # Zero-write preview check!
        preview = preview_provider_import(
            self.access_ag1,
            provider_uuid=str(self.agency_1.uuid),
            csv_text=csv_agency_1,
            preset_key="agency_fieldpulse",
        )
        self.assertEqual(preview["total_rows"], 2)
        self.assertEqual(preview["created_count"], 2)
        self.assertEqual(preview["error_count"], 0)
        self.assertEqual(
            CanonicalVisit.objects.filter(tenant=self.brand_a).count(), 1
        )
        self.assertEqual(
            VisitSubmissionSource.objects.filter(tenant=self.brand_a).count(), 0
        )

        # Commit Agency 1 CSV.
        job_1 = commit_provider_import(
            self.access_ag1,
            provider_uuid=str(self.agency_1.uuid),
            csv_text=csv_agency_1,
            filename="fieldpulse-oct10.csv",
            preset_key="agency_fieldpulse",
            save_mapping_name="FieldPulse Standard",
        )
        self.assertEqual(job_1.created_count, 2)
        # Scheduled morning visit + new distinct evening shift = 2 canonical visits!
        self.assertEqual(
            CanonicalVisit.objects.filter(tenant=self.brand_a).count(), 2
        )

        # Agency 2 CSV (PromoWorks layout): differently formatted columns
        # referencing the SAME morning visit (11:00-15:00) at FreshMart #102!
        csv_agency_2 = (
            "shift_ref,shift_date,window_start,window_end,account_name,location_code,street,town,region,tactic_name,product_code,samples_out,shopper_count,bottles_sold,summary\n"
            "PW-8801,10/10/2026,11:00 AM,03:00 PM,FreshMart,102,500 Congress Ave,Austin,TX,Retail Sampling,Wild Berry 12oz,40,35,8,Co-staffed tasting bar\n"
        )
        job_2 = commit_provider_import(
            self.access_ag2,
            provider_uuid=str(self.agency_2.uuid),
            csv_text=csv_agency_2,
            filename="promoworks-oct10.csv",
            preset_key="agency_promoworks",
        )
        self.assertEqual(job_2.created_count, 1)

        # Still ONLY 2 canonical visits total (morning reconciled, evening separate)!
        self.assertEqual(
            CanonicalVisit.objects.filter(tenant=self.brand_a).count(), 2
        )
        scheduled_visit.refresh_from_db()
        morning_subs = list(
            VisitSubmissionSource.objects.filter(
                canonical_visit=scheduled_visit
            ).order_by("id")
        )
        self.assertEqual(len(morning_subs), 2)
        self.assertEqual(
            {s.provider_id for s in morning_subs},
            {self.agency_1.id, self.agency_2.id},
        )
        # Each provider's raw row is preserved intact!
        self.assertEqual(morning_subs[0].raw_row["Event ID"], "FP-1001")
        self.assertEqual(morning_subs[1].raw_row["shift_ref"], "PW-8801")
        self.assertEqual(scheduled_visit.samples_distributed, 120)
        self.assertEqual(scheduled_visit.rep_observed_units, 22)

        # Re-uploading Agency 1's identical file creates ZERO duplicates!
        job_1_reupload = commit_provider_import(
            self.access_ag1,
            provider_uuid=str(self.agency_1.uuid),
            csv_text=csv_agency_1,
            filename="fieldpulse-oct10-reupload.csv",
            preset_key="agency_fieldpulse",
        )
        self.assertEqual(job_1_reupload.created_count, 0)
        self.assertEqual(job_1_reupload.updated_count, 0)
        self.assertEqual(job_1_reupload.unchanged_count, 2)
        self.assertEqual(
            CanonicalVisit.objects.filter(tenant=self.brand_a).count(), 2
        )
        self.assertEqual(
            VisitSubmissionSource.objects.filter(tenant=self.brand_a).count(), 3
        )

        # Uploading a single-row correction for FP-1001 updates cleanly with version history!
        csv_agency_1_corrected = (
            "Event ID,Activation Date,Start Time,End Time,Retail Banner,Store #,Store Address,City,State,Program,SKU Sampled,Cans Sampled,Consumers Engaged,Units Sold Observed,Rep Notes\n"
            "FP-1001,2026-10-10,11:00,15:00,FreshMart,102,500 Congress Ave,Austin,TX,Retail Sampling,Yuzu Citrus 12oz,85,68,16,Corrected count from tally sheet\n"
        )
        job_1_corr = commit_provider_import(
            self.access_ag1,
            provider_uuid=str(self.agency_1.uuid),
            csv_text=csv_agency_1_corrected,
            preset_key="agency_fieldpulse",
        )
        self.assertEqual(job_1_corr.updated_count, 1)
        sub_fp1001 = VisitSubmissionSource.objects.get(
            tenant=self.brand_a,
            provider=self.agency_1,
            provider_event_id="FP-1001",
        )
        self.assertEqual(sub_fp1001.version, 2)
        self.assertEqual(len(sub_fp1001.previous_versions), 1)
        self.assertEqual(
            sub_fp1001.previous_versions[0]["samples_distributed"], 80
        )
        scheduled_visit.refresh_from_db()
        self.assertEqual(scheduled_visit.samples_distributed, 125)
        self.assertEqual(scheduled_visit.rep_observed_units, 24)

        # Uncertain SKU match goes to review queue rather than guessing, and
        # single-row retry resolves it cleanly!
        csv_uncertain = (
            "shift_ref,shift_date,account_name,location_code,street,town,region,product_code,samples_out,shopper_count,bottles_sold\n"
            "PW-9900,10/11/2026,FreshMart,205,900 Main St,Dallas,TX,YUZU-PROMO-CODE,60,50,11\n"
        )
        job_unc = commit_provider_import(
            self.access_ag2,
            provider_uuid=str(self.agency_2.uuid),
            csv_text=csv_uncertain,
            preset_key="agency_promoworks",
        )
        self.assertEqual(job_unc.review_count, 1)
        review_row = ProviderImportRow.objects.get(job=job_unc, row_number=1)
        self.assertEqual(review_row.status, ProviderImportRow.STATUS_NEEDS_REVIEW)

        resolved_row = resolve_provider_import_row(
            self.access_a,
            row_uuid=str(review_row.uuid),
            product_id=self.sku_yuzu.id,
            save_product_alias=True,
        )
        self.assertEqual(resolved_row.status, ProviderImportRow.STATUS_CREATED)
        self.assertEqual(
            CanonicalVisit.objects.filter(tenant=self.brand_a).count(), 3
        )

        # Provider source inclusion toggle excludes Agency 2-only visit when turned off
        # and restores it when turned back on!
        self.assertEqual(
            brand_included_visits_queryset(self.brand_a).count(), 3
        )
        set_provider_source_inclusion(
            self.access_a,
            provider_uuid=str(self.agency_2.uuid),
            include_in_combined_reporting=False,
        )
        self.assertEqual(
            brand_included_visits_queryset(self.brand_a).count(), 2
        )
        set_provider_source_inclusion(
            self.access_a,
            provider_uuid=str(self.agency_2.uuid),
            include_in_combined_reporting=True,
        )
        self.assertEqual(
            brand_included_visits_queryset(self.brand_a).count(), 3
        )

    def test_scenario_2_reconcile_290_demo_cobrand_allocation_and_finance_queue(self):
        """Scenario 2:
        A demo has $250 contracted demo fee, $40 product, $10 mileage, 5% admin
        fee on product only ($2), $12 credit, and a $40 store receipt matching
        the product charge -> brand-visible cost = $290.00 with zero double-count.
        Also verifies $40 co-brand 75/25 allocation ($30/$10) with strict brand
        isolation, unitemized receipt exception visibility, idempotent evaluation,
        and decoupling from recap approval."""
        ev_dt = timezone.make_aware(
            datetime.combine(date(2026, 10, 15), time(12, 0)),
            timezone.get_current_timezone(),
        )
        ev = Event.objects.create(
            tenant=self.brand_a,
            event_type=self.prog_retail,
            retailer=self.store_102,
            name="FreshMart #102 Demo",
            date=ev_dt,
            start_time=ev_dt,
            end_time=ev_dt,
            address="500 Congress Ave",
            created_by=self.admin_a,
        )
        tmpl = CustomRecapTemplate.objects.create(
            tenant=self.brand_a,
            event_type=self.prog_retail,
            name="Standard Retail Recap",
            created_by=self.admin_a,
        )
        recap = CustomRecap.objects.create(
            tenant=self.brand_a,
            event=ev,
            custom_recap_template=tmpl,
            name="FreshMart #102 Recap",
            approved=False,
            total_engagements=80,
            created_by=self.admin_a,
        )
        ft = FileType.objects.create(name="image/jpeg", created_by=self.admin_a)
        recap_file = CustomRecapFile.objects.create(
            custom_recap=recap,
            file_type=ft,
            name="product-receipt.jpg",
            created_by=self.admin_a,
        )
        reading = ReceiptReading.objects.create(
            custom_recap_file=recap_file,
            status=ReceiptReading.STATUS_READ,
            total=Decimal("40.00"),
            merchant="FreshMart #102",
            line_items=[
                {"name": "Yuzu Citrus 12pk", "qty": 2, "amount": "40.00"}
            ],
        )

        visit_a = CanonicalVisit.objects.create(
            tenant=self.brand_a,
            event=ev,
            primary_provider=self.agency_1,
            retailer=self.store_102,
            event_type=self.prog_retail,
            store_key="freshmart-102-austin",
            store_label="FreshMart #102",
            chain_key="freshmart",
            chain_label="FreshMart",
            visit_date=date(2026, 10, 15),
            status=CanonicalVisit.STATUS_COMPLETED,
            samples_distributed=100,
            rep_observed_units=29,
        )

        # Configure provider agreement: $250 demo fee, 5% admin fee on PRODUCT ONLY!
        agreement = upsert_provider_agreement(
            self.access_finance_a,
            provider_uuid=str(self.agency_1.uuid),
            name="FieldPulse 2026 Rate Card",
            contracted_demo_fee=Decimal("250.00"),
            admin_fee_percent=Decimal("5.00"),
            admin_fee_bases=[BrandCostLine.CAT_PRODUCT],
        )

        inv = Invoice.objects.create(
            tenant=self.brand_a, status=Invoice.STATUS_SENT
        )
        inv_line_prod = InvoiceLineItem.objects.create(
            invoice=inv,
            event=ev,
            description="Product reimbursement",
            quantity=Decimal("1"),
            unit_price=Decimal("40.00"),
            amount=Decimal("40.00"),
        )
        recompute_invoice_totals(inv, [inv_line_prod])
        inv.save()

        # Enter cost lines: $250 demo fee, $40 product (invoice line), $10 mileage, $12 credit.
        upsert_brand_cost_line(
            self.access_finance_a,
            visit_uuid=str(visit_a.uuid),
            category=BrandCostLine.CAT_DEMO_FEE,
            source_type=BrandCostLine.SOURCE_AGREEMENT,
            amount=Decimal("250.00"),
            settlement_status=BrandCostLine.SETTLEMENT_INVOICED,
            evidence_status=BrandCostLine.EVIDENCE_SUPPORTED,
            agreement_uuid=str(agreement.uuid),
        )
        prod_line = upsert_brand_cost_line(
            self.access_finance_a,
            visit_uuid=str(visit_a.uuid),
            category=BrandCostLine.CAT_PRODUCT,
            source_type=BrandCostLine.SOURCE_INVOICE_LINE,
            amount=Decimal("40.00"),
            settlement_status=BrandCostLine.SETTLEMENT_INVOICED,
            evidence_status=BrandCostLine.EVIDENCE_MISSING,
            invoice_line_id=inv_line_prod.id,
            agreement_uuid=str(agreement.uuid),
        )
        upsert_brand_cost_line(
            self.access_finance_a,
            visit_uuid=str(visit_a.uuid),
            category=BrandCostLine.CAT_MILEAGE,
            source_type=BrandCostLine.SOURCE_INVOICE_LINE,
            amount=Decimal("10.00"),
            settlement_status=BrandCostLine.SETTLEMENT_INVOICED,
            evidence_status=BrandCostLine.EVIDENCE_SUPPORTED,
            agreement_uuid=str(agreement.uuid),
        )
        upsert_brand_cost_line(
            self.access_finance_a,
            visit_uuid=str(visit_a.uuid),
            category=BrandCostLine.CAT_CREDIT,
            source_type=BrandCostLine.SOURCE_CREDIT,
            amount=Decimal("12.00"),
            settlement_status=BrandCostLine.SETTLEMENT_INVOICED,
            evidence_status=BrandCostLine.EVIDENCE_SUPPORTED,
            agreement_uuid=str(agreement.uuid),
        )

        # Link the $40 store receipt to the $40 product invoice line ->
        # proof is linked, total stays $290.00 (NEVER $330.00!).
        link_receipt_evidence_to_cost_line(
            self.access_finance_a,
            cost_line_uuid=str(prod_line.uuid),
            recap_file_id=recap_file.id,
            receipt_reading_id=reading.id,
            label="FreshMart #102 register receipt",
        )

        visit_a.refresh_from_db()
        summary = compute_visit_cost_summary(visit_a)
        self.assertEqual(summary.total_cost, Decimal("290.00"))
        self.assertEqual(summary.supported_cost, Decimal("290.00"))
        self.assertFalse(summary.is_incomplete)
        self.assertEqual(summary.evidence_status, BrandCostLine.EVIDENCE_SUPPORTED)
        self.assertEqual(summary.cost_per_sample, Decimal("2.90"))
        self.assertEqual(summary.cost_per_observed_unit, Decimal("10.00"))
        self.assertEqual(
            summary.category_totals[BrandCostLine.CAT_SERVICE_ADMIN_FEE],
            "2.00",
        )

        # Finance queue + advisory separation + unitemized proof check:
        # Add an operational RecapFlag (short_clock) -> advisory only, not FinanceException!
        RecapFlag.objects.create(
            tenant=self.brand_a,
            custom_recap=recap,
            kind=RecapFlag.KIND_SHORT_CLOCK,
            severity=RecapFlag.SEVERITY_MEDIUM,
            explanation="Clocked in for 18 minutes.",
        )
        evaluate_finance_exceptions(self.brand_a)
        open_excs = [
            e
            for e in list_finance_exceptions(self.access_finance_a)
            if e.status == FinanceException.STATUS_OPEN
        ]
        self.assertEqual(len(open_excs), 0)
        notices = list_operational_field_notices(
            self.access_finance_a, visit_uuid=str(visit_a.uuid)
        )
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["kind"], RecapFlag.KIND_SHORT_CLOCK)

        # Now add a parking charge with UNITEMIZED $15 receipt (total matches $15,
        # but slip has no itemization) -> stays visible as unitemized_proof!
        parking_line = upsert_brand_cost_line(
            self.access_finance_a,
            visit_uuid=str(visit_a.uuid),
            category=BrandCostLine.CAT_PARKING,
            source_type=BrandCostLine.SOURCE_EXPENSE_RECEIPT,
            amount=Decimal("15.00"),
            settlement_status=BrandCostLine.SETTLEMENT_INVOICED,
        )
        link_receipt_evidence_to_cost_line(
            self.access_finance_a,
            cost_line_uuid=str(parking_line.uuid),
            proof_url="https://cdn.example/summary-slip.jpg",
            label="Credit card summary slip",
            verified_amount=Decimal("15.00"),
            is_itemized=False,
        )
        evaluate_finance_exceptions(self.brand_a)
        evaluate_finance_exceptions(self.brand_a)  # Idempotent second run!
        unitemized_list = list_finance_exceptions(
            self.access_finance_a,
            exception_type=FinanceException.TYPE_UNITEMIZED_PROOF,
        )
        self.assertEqual(len(unitemized_list), 1)
        exc_item = unitemized_list[0]
        self.assertEqual(exc_item.status, FinanceException.STATUS_OPEN)

        # Resolving finance exception does NOT approve the unapproved recap,
        # and approving recap does NOT alter finance exceptions!
        self.assertFalse(CustomRecap.objects.get(id=recap.id).approved)
        resolve_finance_exception(
            self.access_finance_a,
            exception_uuid=str(exc_item.uuid),
            resolution_status=FinanceException.STATUS_EXCEPTION_GRANTED,
            reason="Garage meter only prints summary receipt.",
        )
        self.assertFalse(CustomRecap.objects.get(id=recap.id).approved)

        # Part 2 of Scenario 2: Co-branded $40 shared receipt allocated 75% to
        # Brand A ($30.00) and 25% to Brand B ($10.00)!
        visit_co_a = CanonicalVisit.objects.create(
            tenant=self.brand_a,
            store_key="freshmart-205-dallas",
            store_label="FreshMart #205",
            chain_key="freshmart",
            chain_label="FreshMart",
            visit_date=date(2026, 10, 16),
            status=CanonicalVisit.STATUS_COMPLETED,
        )
        visit_co_b = CanonicalVisit.objects.create(
            tenant=self.brand_b,
            store_key="freshmart-205-dallas",
            store_label="FreshMart #205",
            chain_key="freshmart",
            chain_label="FreshMart",
            visit_date=date(2026, 10, 16),
            status=CanonicalVisit.STATUS_COMPLETED,
        )
        shared_src, alloc_lines = allocate_shared_cost(
            actor=self.staff,
            total_amount=Decimal("40.00"),
            category=BrandCostLine.CAT_PRODUCT,
            allocations=[
                {"visit": visit_co_a, "percentage": Decimal("75")},
                {"visit": visit_co_b, "percentage": Decimal("25")},
            ],
            allocation_mode="percentage",
            description="Co-branded ice & garnish receipt",
        )
        self.assertEqual(len(alloc_lines), 2)
        self.assertEqual(
            alloc_lines[0].amount + alloc_lines[1].amount,
            shared_src.total_amount,
        )
        sum_co_a = compute_visit_cost_summary(visit_co_a)
        sum_co_b = compute_visit_cost_summary(visit_co_b)
        self.assertEqual(sum_co_a.total_cost, Decimal("30.00"))
        self.assertEqual(sum_co_b.total_cost, Decimal("10.00"))

        # Brand A rollups never leak Brand B's $10 share or Brand B's identity!
        rollups_a = build_brand_cost_rollups(self.access_a, group_by="store")
        dallas_row = next(
            r for r in rollups_a if r["key"] == "freshmart-205-dallas"
        )
        self.assertEqual(dallas_row["total_cost"], "30.00")

    def test_scenario_3_imperfect_pos_data_crew_subtraction_dedup_and_roi(self):
        """Scenario 3:
        Retailer POS CSV has 122 register units in the demo week vs 100 baseline,
        2 linked crew-purchased units -> 120 adjusted units (+20 observed diff),
        an explicit 0-unit store-SKU-week, a missing store-SKU-week, and an
        unmatched SKU row. Adding a second demo in the same store-week does NOT
        double-count the 122/120 store-week POS sales. Verifies clean version
        replacement on reimport, lift/ROI gating when incomplete vs complete,
        and 'Retailer sales unavailable' on Brand B."""
        # Brand B has no POS feed -> clean unavailable state!
        pos_b = build_retailer_pos_measurement(self.access_b)
        self.assertFalse(pos_b["available"])
        self.assertEqual(
            pos_b["unavailable_message"], RETAILER_UNAVAILABLE_MESSAGE
        )

        from brand_insights.facts import store_identity

        sk_102, lbl_102, ck_102, cl_102 = store_identity(
            "FreshMart #102", "500 Congress Ave", "Austin", "TX"
        )
        sk_205, lbl_205, ck_205, cl_205 = store_identity(
            "FreshMart #205", "900 Main St", "Dallas", "TX"
        )

        # Demo #1 at FreshMart #102 in week of Monday 2026-10-12 (visit on 2026-10-14)
        visit_1 = CanonicalVisit.objects.create(
            tenant=self.brand_a,
            primary_provider=self.agency_1,
            retailer=self.store_102,
            store_key=sk_102,
            store_label=lbl_102,
            chain_key=ck_102,
            chain_label=cl_102,
            store_number="102",
            address="500 Congress Ave",
            city="Austin",
            state_code="TX",
            visit_date=date(2026, 10, 14),
            shift_key="morning",
            status=CanonicalVisit.STATUS_COMPLETED,
            rep_observed_units=25,
        )
        VisitSubmissionSource.objects.create(
            tenant=self.brand_a,
            canonical_visit=visit_1,
            provider=self.agency_1,
            recap_status=VisitSubmissionSource.RECAP_APPROVED,
            rep_observed_units=25,
            sku_breakdown=[
                {
                    "product_id": self.sku_yuzu.id,
                    "product_name": self.sku_yuzu.name,
                }
            ],
        )

        # Demo at FreshMart #205 in week of 2026-10-12 with BOTH Yuzu (explicit 0)
        # and Berry (missing from v1 POS feed!)
        visit_205 = CanonicalVisit.objects.create(
            tenant=self.brand_a,
            primary_provider=self.agency_1,
            retailer=self.store_205,
            store_key=sk_205,
            store_label=lbl_205,
            chain_key=ck_205,
            chain_label=cl_205,
            store_number="205",
            address="900 Main St",
            city="Dallas",
            state_code="TX",
            visit_date=date(2026, 10, 15),
            status=CanonicalVisit.STATUS_COMPLETED,
            rep_observed_units=4,
        )
        VisitSubmissionSource.objects.create(
            tenant=self.brand_a,
            canonical_visit=visit_205,
            provider=self.agency_1,
            recap_status=VisitSubmissionSource.RECAP_APPROVED,
            rep_observed_units=4,
            sku_breakdown=[
                {
                    "product_id": self.sku_yuzu.id,
                    "product_name": self.sku_yuzu.name,
                },
                {
                    "product_id": self.sku_berry.id,
                    "product_name": self.sku_berry.name,
                },
            ],
        )

        # Link 2 crew-purchased units of Yuzu on 2026-10-14 at FreshMart #102!
        record_crew_purchase_adjustment(
            self.access_a,
            visit_uuid=str(visit_1.uuid),
            product_id=self.sku_yuzu.id,
            units_purchased=2,
            purchase_date=date(2026, 10, 14),
            retailer_banner="FreshMart",
            transaction_ref="TXN-CREW-01",
        )

        # Import v1 POS CSV:
        # - Row 1: FreshMart #102, Yuzu Citrus 12oz, 122 register units, 100 baseline (4 wks)
        # - Row 2: FreshMart #205, Yuzu Citrus 12oz, 0 register units (explicit zero!)
        # - Row 3: FreshMart #102, MYSTERY-SKU-999, 15 register units (unmatched SKU!)
        # (Note: FreshMart #205 Wild Berry 12oz is missing from v1!)
        pos_csv_v1 = (
            "retailer,store_id,store_name,city,state,sku,week_start,week_end,register_units,baseline_units,baseline_weeks\n"
            "FreshMart,102,FreshMart #102,Austin,TX,Yuzu Citrus 12oz,2026-10-12,2026-10-18,122,100,4\n"
            "FreshMart,205,FreshMart #205,Dallas,TX,Yuzu Citrus 12oz,2026-10-12,2026-10-18,0,12,4\n"
            "FreshMart,102,FreshMart #102,Austin,TX,MYSTERY-SKU-999,2026-10-12,2026-10-18,15,10,4\n"
        )
        imp_v1 = import_retailer_pos_csv(
            self.access_a,
            retailer_banner="FreshMart",
            csv_text=pos_csv_v1,
            filename="freshmart-pos-v1.csv",
        )
        self.assertEqual(imp_v1.matched_rows, 2)
        self.assertEqual(imp_v1.unmatched_rows, 1)

        m_v1 = build_retailer_pos_measurement(
            self.access_a,
            period_start=date(2026, 10, 12),
            period_end=date(2026, 10, 18),
        )
        self.assertTrue(m_v1["available"])
        # Coverage: 2 matched out of 3 eligible store-SKU-weeks (Berry at #205 is missing)!
        self.assertEqual(m_v1["coverage"]["matched_store_sku_weeks"], 2)
        self.assertEqual(m_v1["coverage"]["eligible_store_sku_weeks"], 3)
        self.assertEqual(m_v1["coverage"]["unmatched_row_count"], 1)
        self.assertEqual(len(m_v1["coverage"]["missing_rows"]), 1)
        self.assertEqual(
            m_v1["coverage"]["missing_rows"][0]["status"], "missing_from_feed"
        )

        # Unmatched row (15 units) NEVER counted in matched register_units (122 + 0 = 122)!
        self.assertEqual(m_v1["totals"]["register_units"], 122)
        self.assertEqual(m_v1["totals"]["crew_purchased_units"], 2)
        self.assertEqual(m_v1["totals"]["adjusted_register_units"], 120)
        # Rep-observed units (25 + 4 = 29) stay completely separate from register units!
        self.assertEqual(m_v1["totals"]["rep_observed_units"], 29)
        # Overall period incremental lift & ROI are WITHHELD while Berry at #205 is missing & costs missing!
        self.assertIsNone(m_v1["totals"]["incremental_lift_units"])
        self.assertIsNone(m_v1["totals"]["roi"])

        # Inspect FreshMart #102 store-week specifically: 122 orig, 2 crew, 120 adj, +20.00 diff!
        sw_102 = next(
            sw for sw in m_v1["store_weeks"] if sw["store_key"] == sk_102
        )
        self.assertEqual(sw_102["register_units"], 122)
        self.assertEqual(sw_102["adjusted_register_units"], 120)
        self.assertEqual(sw_102["observed_vs_baseline_diff"], "20.00")
        self.assertEqual(sw_102["incremental_lift_units"], "20.00")

        # Inspect FreshMart #205 Yuzu explicit zero cell vs Berry missing cell!
        sw_205 = next(
            sw for sw in m_v1["store_weeks"] if sw["store_key"] == sk_205
        )
        yuzu_cell = next(
            c
            for c in sw_205["sku_cells"]
            if c["product_id"] == self.sku_yuzu.id
        )
        berry_cell = next(
            c
            for c in sw_205["sku_cells"]
            if c["product_id"] == self.sku_berry.id
        )
        self.assertEqual(yuzu_cell["status"], "explicit_zero")
        self.assertEqual(yuzu_cell["register_units"], 0)
        self.assertEqual(berry_cell["status"], "missing_from_feed")
        self.assertIsNone(berry_cell["register_units"])

        # Add a SECOND demo in the SAME store-week at FreshMart #102 (2026-10-16)!
        visit_2_same_week = CanonicalVisit.objects.create(
            tenant=self.brand_a,
            primary_provider=self.agency_2,
            retailer=self.store_102,
            store_key=sk_102,
            store_label=lbl_102,
            chain_key=ck_102,
            chain_label=cl_102,
            store_number="102",
            address="500 Congress Ave",
            city="Austin",
            state_code="TX",
            visit_date=date(2026, 10, 16),
            shift_key="weekend",
            status=CanonicalVisit.STATUS_COMPLETED,
            rep_observed_units=18,
        )
        VisitSubmissionSource.objects.create(
            tenant=self.brand_a,
            canonical_visit=visit_2_same_week,
            provider=self.agency_2,
            recap_status=VisitSubmissionSource.RECAP_APPROVED,
            rep_observed_units=18,
            sku_breakdown=[
                {
                    "product_id": self.sku_yuzu.id,
                    "product_name": self.sku_yuzu.name,
                }
            ],
        )

        m_two_demos = build_retailer_pos_measurement(
            self.access_a,
            period_start=date(2026, 10, 12),
            period_end=date(2026, 10, 18),
            store_key=sk_102,
        )
        # Store-week register sales stay 122 / 120 (NEVER double-counted as 244 / 240)!
        self.assertEqual(m_two_demos["totals"]["register_units"], 122)
        self.assertEqual(m_two_demos["totals"]["adjusted_register_units"], 120)
        self.assertEqual(m_two_demos["store_weeks"][0]["demo_count"], 2)
        self.assertTrue(m_two_demos["store_weeks"][0]["shared_store_week"])

        # Re-import corrected v2 POS CSV for the same period -> supersedes v1 cleanly!
        pos_csv_v2 = (
            "retailer,store_id,store_name,city,state,sku,week_start,week_end,register_units,baseline_units,baseline_weeks\n"
            "FreshMart,102,FreshMart #102,Austin,TX,Yuzu Citrus 12oz,2026-10-12,2026-10-18,122,100,4\n"
            "FreshMart,205,FreshMart #205,Dallas,TX,Yuzu Citrus 12oz,2026-10-12,2026-10-18,0,12,4\n"
            "FreshMart,205,FreshMart #205,Dallas,TX,Wild Berry 12oz,2026-10-12,2026-10-18,35,25,4\n"
        )
        imp_v2 = import_retailer_pos_csv(
            self.access_a,
            retailer_banner="FreshMart",
            csv_text=pos_csv_v2,
            filename="freshmart-pos-v2.csv",
        )
        imp_v1.refresh_from_db()
        self.assertFalse(imp_v1.is_active_version)
        self.assertEqual(imp_v2.version, 2)
        self.assertTrue(imp_v2.is_active_version)

        # Complete demo costs ($145 + $145 = $290 on #102) & set unit contribution = $20.00
        # -> Incremental Lift = 20.00 units, Incremental Contribution = $400.00,
        # ROI = (400 - 290) / 290 = 0.3793!
        for v_item in (visit_1, visit_2_same_week):
            upsert_brand_cost_line(
                self.access_finance_a,
                visit_uuid=str(v_item.uuid),
                category=BrandCostLine.CAT_DEMO_FEE,
                source_type=BrandCostLine.SOURCE_INVOICE_LINE,
                amount=Decimal("145.00"),
                settlement_status=BrandCostLine.SETTLEMENT_INVOICED,
                evidence_status=BrandCostLine.EVIDENCE_SUPPORTED,
            )
        profile = ensure_workspace_profile(self.brand_a)
        profile.default_contribution_per_unit = Decimal("20.00")
        profile.save()

        m_complete = build_retailer_pos_measurement(
            self.access_a,
            period_start=date(2026, 10, 12),
            period_end=date(2026, 10, 18),
            store_key=sk_102,
        )
        self.assertEqual(m_complete["totals"]["incremental_lift_units"], "20.00")
        self.assertEqual(
            m_complete["totals"]["incremental_contribution"], "400.00"
        )
        self.assertEqual(m_complete["totals"]["eligible_brand_cost"], "290.00")
        self.assertEqual(m_complete["totals"]["roi"], "0.3793")

    def test_scenario_4_act_on_published_recommendation_refresh_survival_and_buyer_report(self):
        """Scenario 4:
        Published BrandInsight recommends rebooking a top store.
        - Draft insight and ad-hoc AI chat strings are blocked.
        - Non-spend-approver reviewer is blocked from Rebook.
        - Spend approver clicks Rebook twice (idempotent -> 1 request/visit/assignment)
          assigning Agency 2.
        - Agency 2 acknowledges, completes return visit & recap.
        - Weekly BrandInsight refresh runs -> full chain remains intact in store
          history and weekly buyer budget-meeting report + CSV/XLSX exports!
        - Also verifies chain-scoped store picker, Hold (0 visits), and verified Restock."""
        from brand_insights.facts import store_identity

        sk_102, lbl_102, ck_102, _ = store_identity(
            "FreshMart #102", "500 Congress Ave", "Austin", "TX"
        )
        sk_205, lbl_205, _, _ = store_identity(
            "FreshMart #205", "900 Main St", "Dallas", "TX"
        )

        # 1. Draft BrandInsight & ad-hoc AI answer are strictly blocked!
        draft_ins = BrandInsight.objects.create(
            tenant=self.brand_a,
            kind=BrandInsight.KIND_RECOMMENDATION,
            code="top_store",
            scope_key=f"store:{sk_102}",
            period_start=date(2026, 10, 5),
            period_end=date(2026, 10, 11),
            subject=lbl_102,
            template="Draft template",
            body="Draft body",
            facts={"visits": 3, "units": 90},
            status=BrandInsight.STATUS_DRAFT,
            generated_at=timezone.now(),
        )
        with self.assertRaises(PermissionError):
            create_store_recommendation_action(
                self.access_a,
                finding_id=f"BrandInsight:{draft_ins.id}",
                action_type=StoreRecommendationAction.ACTION_REBOOK,
            )
        with self.assertRaises(PermissionError):
            create_store_recommendation_action(
                self.access_a,
                finding_id="ai-chat-turn:999",
                action_type=StoreRecommendationAction.ACTION_REBOOK,
            )

        # Publish the finding!
        draft_ins.status = BrandInsight.STATUS_PUBLISHED
        draft_ins.save(update_fields=["status"])

        # 2. Reviewer WITHOUT spend approval is blocked from Rebook!
        with self.assertRaises(PermissionError):
            create_store_recommendation_action(
                self.access_reviewer_no_spend,
                finding_id=f"BrandInsight:{draft_ins.id}",
                action_type=StoreRecommendationAction.ACTION_REBOOK,
            )

        # 3. Spend approver clicks Rebook TWICE (double-click idempotency) assigning Agency 2!
        act_1 = create_store_recommendation_action(
            self.access_a,
            finding_id=f"BrandInsight:{draft_ins.id}",
            action_type=StoreRecommendationAction.ACTION_REBOOK,
            provider_uuid=str(self.agency_2.uuid),
            target_date=date(2026, 10, 24),
            budget_amount=Decimal("290.00"),
            idempotency_key="ui-click-rebook-102",
        )
        act_2_dup = create_store_recommendation_action(
            self.access_a,
            finding_id=f"BrandInsight:{draft_ins.id}",
            action_type=StoreRecommendationAction.ACTION_REBOOK,
            provider_uuid=str(self.agency_2.uuid),
            target_date=date(2026, 10, 24),
            budget_amount=Decimal("290.00"),
            idempotency_key="ui-click-rebook-102",
        )
        self.assertEqual(act_1.id, act_2_dup.id)
        self.assertEqual(
            StoreRecommendationAction.objects.filter(tenant=self.brand_a).count(),
            1,
        )
        self.assertIsNotNone(act_1.linked_request_id)
        self.assertIsNotNone(act_1.provider_assignment_id)
        self.assertIsNotNone(act_1.outcome_visit_id)

        # 4. Agency 1 cannot respond to Agency 2's action; Agency 2 acknowledges!
        with self.assertRaises(PermissionError):
            respond_store_action(
                self.access_ag1,
                action_uuid=str(act_1.uuid),
                decision="accepted",
            )
        act_accepted = respond_store_action(
            self.access_ag2,
            action_uuid=str(act_1.uuid),
            decision="accepted",
            note="Crew scheduled for Saturday 11am.",
        )
        self.assertEqual(
            act_accepted.status, StoreRecommendationAction.STATUS_ACCEPTED
        )

        # 5. Complete the return visit + link recap!
        tmpl = CustomRecapTemplate.objects.create(
            tenant=self.brand_a,
            event_type=self.prog_retail,
            name="Return Visit Template",
            created_by=self.admin_a,
        )
        return_recap = CustomRecap.objects.create(
            tenant=self.brand_a,
            event=act_1.outcome_visit.event,
            custom_recap_template=tmpl,
            name="FreshMart #102 Return Recap",
            approved=True,
            total_engagements=92,
            created_by=self.admin_a,
        )
        act_done = complete_rebook_return_visit(
            self.access_ag2,
            action_uuid=str(act_1.uuid),
            samples_distributed=110,
            consumers_sampled=92,
            rep_observed_units=34,
            custom_recap_id=return_recap.id,
            notes="Sold out endcap tray.",
        )
        self.assertEqual(
            act_done.status, StoreRecommendationAction.STATUS_COMPLETED
        )
        upsert_brand_cost_line(
            self.access_finance_a,
            visit_uuid=str(act_done.outcome_visit.uuid),
            category=BrandCostLine.CAT_DEMO_FEE,
            source_type=BrandCostLine.SOURCE_INVOICE_LINE,
            amount=Decimal("265.00"),
            settlement_status=BrandCostLine.SETTLEMENT_INVOICED,
            evidence_status=BrandCostLine.EVIDENCE_SUPPORTED,
        )

        # 6. Chain-scoped finding (`chain:freshmart`) requires store selection:
        # test Hold (creates 0 visits!) and Restock + verification on #205!
        chain_ins = BrandInsight.objects.create(
            tenant=self.brand_a,
            kind=BrandInsight.KIND_ALERT,
            code="chain_conversion_drop",
            scope_key=f"chain:{ck_102}",
            period_start=date(2026, 10, 12),
            period_end=date(2026, 10, 18),
            subject="FreshMart",
            template="Chain alert",
            body="FreshMart conversion dipped across 2 stores.",
            facts={"visits": 4},
            status=BrandInsight.STATUS_PUBLISHED,
            generated_at=timezone.now(),
        )
        with self.assertRaises(ValueError):
            create_store_recommendation_action(
                self.access_a,
                finding_id=f"BrandInsight:{chain_ins.id}",
                action_type=StoreRecommendationAction.ACTION_HOLD,
                hold_reason="Awaiting buyer reset",
                next_review_date=date(2026, 11, 1),
            )

        visits_before_hold = CanonicalVisit.objects.filter(
            tenant=self.brand_a
        ).count()
        hold_act = create_store_recommendation_action(
            self.access_reviewer_no_spend,
            finding_id=f"BrandInsight:{chain_ins.id}",
            selected_store_key=sk_205,
            action_type=StoreRecommendationAction.ACTION_HOLD,
            hold_reason="Buyer resetting cold-box planogram",
            next_review_date=date(2026, 11, 1),
        )
        self.assertEqual(
            hold_act.status, StoreRecommendationAction.STATUS_ON_HOLD
        )
        self.assertEqual(
            CanonicalVisit.objects.filter(tenant=self.brand_a).count(),
            visits_before_hold,
        )

        restock_ins = BrandInsight.objects.create(
            tenant=self.brand_a,
            kind=BrandInsight.KIND_ALERT,
            code="repeat_no_product",
            scope_key=f"store:{sk_205}",
            period_start=date(2026, 10, 12),
            period_end=date(2026, 10, 18),
            subject=lbl_205,
            template="OOS alert",
            body="FreshMart #205 reported low Yuzu inventory.",
            facts={"visits": 2},
            status=BrandInsight.STATUS_PUBLISHED,
            generated_at=timezone.now(),
        )
        restock_act = create_store_recommendation_action(
            self.access_a,
            finding_id=f"BrandInsight:{restock_ins.id}",
            action_type=StoreRecommendationAction.ACTION_RESTOCK,
            provider_uuid=str(self.agency_1.uuid),
            due_date=date(2026, 10, 22),
        )
        # Reassign provider from Agency 1 -> Agency 2 and verify prior attribution stays!
        restock_act = reassign_store_action_provider(
            self.access_a,
            action_uuid=str(restock_act.uuid),
            new_provider_uuid=str(self.agency_2.uuid),
            note="PromoWorks rep lives 5 min from store.",
        )
        self.assertEqual(restock_act.previous_provider_id, self.agency_1.id)
        self.assertEqual(restock_act.provider_id, self.agency_2.id)

        restock_done = verify_restock_action(
            self.access_ag2,
            action_uuid=str(restock_act.uuid),
            verification_method=StoreRecommendationAction.VERIFY_NOTE,
            verification_note="Confirmed 6 cases stocked in cold box with grocery manager.",
        )
        self.assertEqual(
            restock_done.status, StoreRecommendationAction.STATUS_COMPLETED
        )

        # 7. Run weekly BrandInsight refresh (`generate_for_tenant`) ->
        # published insight + full action -> assignment -> return visit -> recap
        # chain survives completely intact!
        with patch(
            "utils.ai_text._post_json", side_effect=AiUnavailable("offline")
        ):
            generate_for_tenant(self.brand_a.id, today=date(2026, 10, 26))

        act_done.refresh_from_db()
        self.assertEqual(
            act_done.finding_id_snapshot, f"BrandInsight:{draft_ins.id}"
        )
        self.assertEqual(act_done.decision_snapshot["facts"]["visits"], 3)
        self.assertEqual(act_done.outcome_visit.rep_observed_units, 34)
        self.assertEqual(act_done.outcome_recap_id, return_recap.id)

        # 8. Weekly buyer budget-meeting report + CSV and XLSX exports!
        buyer_report = build_weekly_buyer_store_report(
            self.access_a,
            period_start=date(2026, 10, 1),
            period_end=date(2026, 10, 31),
        )
        self.assertEqual(buyer_report["total_visits"], 1)
        self.assertEqual(buyer_report["total_supported_cost"], "265.00")
        row_102 = next(
            r for r in buyer_report["rows"] if r["store_key"] == sk_102
        )
        self.assertEqual(row_102["visit_count"], 1)
        self.assertEqual(row_102["rep_observed_units"], 34)
        self.assertEqual(row_102["current_action_type"], "rebook")
        self.assertEqual(row_102["current_action_status"], "completed")

        csv_out = export_weekly_buyer_report_csv(buyer_report)
        self.assertIn("FreshMart #102", csv_out)
        self.assertIn("265.00", csv_out)
        self.assertIn("rebook", csv_out)

        xlsx_b64 = export_weekly_buyer_report_xlsx_base64(buyer_report)
        wb = openpyxl.load_workbook(io.BytesIO(base64.b64decode(xlsx_b64)))
        ws = wb.active
        sheet_values = [
            [cell.value for cell in row] for row in ws.iter_rows()
        ]
        self.assertEqual(sheet_values[0][0], "Store")
        self.assertTrue(
            any("FreshMart #102" in str(r[0]) for r in sheet_values[1:])
        )
