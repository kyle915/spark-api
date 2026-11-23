"""Conservative OCR receipt → product BrandCostLine auto-match."""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from gqlauth.models import UserStatus

from events.models import Event, EventType, Location, Retailer, State
from pilot.costs import compute_visit_cost_summary, upsert_brand_cost_line
from pilot.finance import (
    evaluate_finance_exceptions,
    list_finance_exceptions,
    maybe_auto_match_receipt_reading,
)
from pilot.models import (
    BrandCostLine,
    CanonicalVisit,
    CostEvidenceLink,
    FinanceException,
    WorkspaceMembership,
)
from pilot.permissions import resolve_workspace_access
from pilot.workspaces import (
    create_or_update_provider,
    upsert_workspace_membership,
)
from recaps.models import (
    CustomField,
    CustomFieldValue,
    CustomRecap,
    CustomRecapFieldType,
    CustomRecapFile,
    CustomRecapTemplate,
    FileType,
    ReceiptReading,
    RecapSection,
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


class AutoMatchReceiptProductSpendTests(TestCase):
    def setUp(self):
        self.staff = _mk_user("staff-auto@ignite-test.example", Role.SPARK_ADMIN_SLUG)
        self.brand = Tenant.objects.create(
            name="AutoMatch Brand",
            slug="automatch-pilot",
            created_by=self.staff,
        )
        self.admin = _mk_user("admin-auto@automatch.example")
        staff_access = resolve_workspace_access(self.staff, tenant=self.brand)
        upsert_workspace_membership(
            staff_access,
            target_user=self.admin,
            workspace_role=WorkspaceMembership.ROLE_BRAND_ADMIN,
        )
        self.access = resolve_workspace_access(self.admin, tenant=self.brand)
        self.agency = create_or_update_provider(self.access, name="AutoMatch Agency")

        self.state = State.objects.create(
            name="Texas", code="TX", created_by=self.admin
        )
        self.loc = Location.objects.create(
            name="Austin",
            code="AUS",
            zip="78701",
            state=self.state,
            created_by=self.admin,
        )
        self.store = Retailer.objects.create(
            tenant=self.brand,
            name="FreshMart #501",
            address="100 Congress Ave",
            location=self.loc,
            created_by=self.admin,
        )
        self.prog = EventType.objects.create(
            tenant=self.brand,
            name="Retail Sampling",
            created_by=self.admin,
        )
        ev_dt = timezone.make_aware(
            datetime.combine(date(2026, 11, 20), time(12, 0)),
            timezone.get_current_timezone(),
        )
        self.event = Event.objects.create(
            tenant=self.brand,
            event_type=self.prog,
            retailer=self.store,
            name="FreshMart #501 Demo",
            date=ev_dt,
            start_time=ev_dt,
            end_time=ev_dt,
            address="100 Congress Ave",
            created_by=self.admin,
        )
        self.tmpl = CustomRecapTemplate.objects.create(
            tenant=self.brand,
            event_type=self.prog,
            name="Retail Recap",
            created_by=self.admin,
        )
        self.recap = CustomRecap.objects.create(
            tenant=self.brand,
            event=self.event,
            custom_recap_template=self.tmpl,
            name="FreshMart #501 Recap",
            approved=False,
            created_by=self.admin,
        )
        self.ft = FileType.objects.create(name="image/jpeg", created_by=self.admin)
        self.recap_file = CustomRecapFile.objects.create(
            custom_recap=self.recap,
            file_type=self.ft,
            name="product-receipt.jpg",
            created_by=self.admin,
        )
        self.visit = CanonicalVisit.objects.create(
            tenant=self.brand,
            event=self.event,
            primary_provider=self.agency,
            retailer=self.store,
            event_type=self.prog,
            store_key="freshmart-501",
            store_label="FreshMart #501",
            chain_key="freshmart",
            chain_label="FreshMart",
            visit_date=date(2026, 11, 20),
            status=CanonicalVisit.STATUS_COMPLETED,
        )

    def _reading(self, *, status=ReceiptReading.STATUS_READ, total="40.00", merchant="HEB"):
        return ReceiptReading.objects.create(
            custom_recap_file=self.recap_file,
            status=status,
            total=Decimal(total) if total is not None else None,
            merchant=merchant,
            line_items=[{"name": "SKU", "qty": 1, "amount": total}] if total else [],
        )

    def _product_line(self, amount="40.00", **kwargs):
        defaults = dict(
            visit_uuid=str(self.visit.uuid),
            category=BrandCostLine.CAT_PRODUCT,
            source_type=BrandCostLine.SOURCE_INVOICE_LINE,
            amount=Decimal(amount),
            settlement_status=BrandCostLine.SETTLEMENT_INVOICED,
            evidence_status=BrandCostLine.EVIDENCE_MISSING,
        )
        defaults.update(kwargs)
        return upsert_brand_cost_line(self.access, **defaults)

    def _set_product_spend(self, amount: str):
        field_type = CustomRecapFieldType.objects.create(
            name="number", created_by=self.admin
        )
        section = RecapSection.objects.create(
            name="Spend", tenant=self.brand, created_by=self.admin
        )
        field = CustomField.objects.create(
            name="Product Spend",
            custom_recap_template=self.tmpl,
            custom_field_type=field_type,
            recap_section=section,
            created_by=self.admin,
        )
        CustomFieldValue.objects.create(
            custom_recap=self.recap,
            custom_field=field,
            value=amount,
            created_by=self.admin,
        )

    def test_happy_path_auto_links_unique_product_line(self):
        prod = self._product_line("40.00")
        reading = self._reading(total="40.00")

        link = maybe_auto_match_receipt_reading(reading)

        self.assertIsNotNone(link)
        self.assertEqual(link.cost_line_id, prod.id)
        self.assertEqual(link.receipt_reading_id, reading.id)
        prod.refresh_from_db()
        self.assertEqual(prod.evidence_status, BrandCostLine.EVIDENCE_SUPPORTED)

        evaluate_finance_exceptions(self.brand)
        open_unmatched = [
            e
            for e in list_finance_exceptions(self.access)
            if e.exception_type == FinanceException.TYPE_UNMATCHED_TXN
            and e.status == FinanceException.STATUS_OPEN
        ]
        self.assertEqual(open_unmatched, [])

        summary = compute_visit_cost_summary(self.visit)
        self.assertEqual(summary.total_cost, Decimal("40.00"))

    def test_ambiguous_multi_product_lines_no_link(self):
        self._product_line("40.00")
        upsert_brand_cost_line(
            self.access,
            visit_uuid=str(self.visit.uuid),
            category=BrandCostLine.CAT_PRODUCT,
            source_type=BrandCostLine.SOURCE_EXPENSE_RECEIPT,
            amount=Decimal("40.00"),
            settlement_status=BrandCostLine.SETTLEMENT_ESTIMATED,
            evidence_status=BrandCostLine.EVIDENCE_MISSING,
            description="Second product line",
        )
        reading = self._reading(total="40.00")

        self.assertIsNone(maybe_auto_match_receipt_reading(reading))
        self.assertFalse(
            CostEvidenceLink.objects.filter(receipt_reading=reading).exists()
        )

        evaluate_finance_exceptions(self.brand)
        open_unmatched = [
            e
            for e in list_finance_exceptions(self.access)
            if e.exception_type == FinanceException.TYPE_UNMATCHED_TXN
            and e.status == FinanceException.STATUS_OPEN
            and e.dedupe_key == f"unmatched-receipt:{reading.id}"
        ]
        self.assertEqual(len(open_unmatched), 1)

    def test_zero_or_failed_ocr_no_link(self):
        self._product_line("40.00")
        failed = self._reading(status=ReceiptReading.STATUS_FAILED, total="40.00")
        self.assertIsNone(maybe_auto_match_receipt_reading(failed))

        # OneToOne on recap_file — use a second file for the zero-total case.
        zero_file = CustomRecapFile.objects.create(
            custom_recap=self.recap,
            file_type=self.ft,
            name="zero-receipt.jpg",
            created_by=self.admin,
        )
        zero = ReceiptReading.objects.create(
            custom_recap_file=zero_file,
            status=ReceiptReading.STATUS_READ,
            total=Decimal("0.00"),
            merchant="HEB",
        )
        self.assertIsNone(maybe_auto_match_receipt_reading(zero))
        self.assertEqual(CostEvidenceLink.objects.count(), 0)

    def test_already_linked_idempotent(self):
        prod = self._product_line("40.00")
        reading = self._reading(total="40.00")
        first = maybe_auto_match_receipt_reading(reading)
        self.assertIsNotNone(first)

        second = maybe_auto_match_receipt_reading(reading)
        self.assertEqual(second.id, first.id)
        self.assertEqual(
            CostEvidenceLink.objects.filter(receipt_reading=reading).count(), 1
        )
        prod.refresh_from_db()
        self.assertEqual(prod.evidence_status, BrandCostLine.EVIDENCE_SUPPORTED)

    def test_evaluate_heals_orphan_receipt_when_product_line_exists(self):
        self._product_line("39.50")
        reading = self._reading(total="40.00")  # within $1 tolerance

        evaluate_finance_exceptions(self.brand)

        self.assertTrue(
            CostEvidenceLink.objects.filter(receipt_reading=reading).exists()
        )
        open_unmatched = [
            e
            for e in list_finance_exceptions(self.access)
            if e.exception_type == FinanceException.TYPE_UNMATCHED_TXN
            and e.status == FinanceException.STATUS_OPEN
        ]
        self.assertEqual(open_unmatched, [])

    def test_creates_product_line_from_recap_spend_when_none_exist(self):
        self._set_product_spend("40.00")
        reading = self._reading(total="40.00")

        link = maybe_auto_match_receipt_reading(reading)

        self.assertIsNotNone(link)
        line = BrandCostLine.objects.get(pk=link.cost_line_id)
        self.assertEqual(line.category, BrandCostLine.CAT_PRODUCT)
        self.assertEqual(line.amount, Decimal("40.00"))
        self.assertEqual(line.source_type, BrandCostLine.SOURCE_EXPENSE_RECEIPT)
        self.assertEqual(line.evidence_status, BrandCostLine.EVIDENCE_SUPPORTED)
        summary = compute_visit_cost_summary(self.visit)
        self.assertEqual(summary.total_cost, Decimal("40.00"))
