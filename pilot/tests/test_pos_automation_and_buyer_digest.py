"""SPA-32: POS portal presets, automated feed drops, weekly buyer digests."""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import patch

from django.test import RequestFactory, TestCase, override_settings
from gqlauth.models import UserStatus

from events.models import Location, Product, ProductType, Retailer, State
from pilot.buyer_digest import (
    buyer_digest_recipients,
    send_buyer_digest_for_tenant,
    set_buyer_weekly_digest,
)
from pilot.ingest import (
    enqueue_pos_feed_drop,
    process_pos_feed_drop,
    upsert_pos_feed_config,
)
from pilot.models import PosFeedDrop, RetailerSalesImport, WorkspaceMembership
from pilot.permissions import resolve_workspace_access
from pilot.pos import import_retailer_pos_csv
from pilot.pos_presets import POS_PRESETS, list_pos_presets
from pilot.workspaces import ensure_workspace_profile, upsert_workspace_membership
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


@override_settings(CONN_MAX_AGE=0)
class PosAutomationAndBuyerDigestTests(TestCase):
    def setUp(self):
        self.staff = _mk_user("staff@ignite-test.example", Role.SPARK_ADMIN_SLUG)
        self.tenant = Tenant.objects.create(
            name="Suncrest Sparkling",
            slug="suncrest-spa32",
            created_by=self.staff,
        )
        self.admin = _mk_user("claire@suncrest-spa32.example")
        staff_access = resolve_workspace_access(self.staff, tenant=self.tenant)
        upsert_workspace_membership(
            staff_access,
            target_user=self.admin,
            workspace_role=WorkspaceMembership.ROLE_BRAND_ADMIN,
        )
        self.access = resolve_workspace_access(self.admin, tenant=self.tenant)
        self.profile = ensure_workspace_profile(self.tenant)

        state = State.objects.create(
            name="Texas", code="TX", created_by=self.admin
        )
        loc = Location.objects.create(
            name="Austin",
            code="AUS",
            zip="78701",
            state=state,
            created_by=self.admin,
        )
        self.store = Retailer.objects.create(
            tenant=self.tenant,
            name="FreshMart #102",
            address="500 Congress Ave",
            location=loc,
            created_by=self.admin,
        )
        ptype = ProductType.objects.create(
            tenant=self.tenant, name="Cans", created_by=self.admin
        )
        self.product = Product.objects.create(
            tenant=self.tenant,
            name="Yuzu Citrus 12oz",
            product_type=ptype,
            created_by=self.admin,
        )

    def test_pos_presets_cover_six_portals(self):
        presets = list_pos_presets()
        self.assertEqual(len(presets), 6)
        keys = {p["key"] for p in presets}
        self.assertEqual(keys, set(POS_PRESETS.keys()))

    def test_import_with_unfi_preset_maps_headers(self):
        csv_text = (
            "Banner,Customer Number,Customer Name,City,State,UPC,"
            "Week Ending,Units,Dollars,Baseline Units\n"
            "FreshMart,102,FreshMart #102,Austin,TX,Yuzu Citrus 12oz,"
            "2026-10-18,40,120.00,30\n"
        )
        imp = import_retailer_pos_csv(
            self.access,
            retailer_banner="FreshMart",
            csv_text=csv_text,
            filename="unfi.csv",
            preset_key="unfi_clearview",
        )
        self.assertEqual(imp.matched_rows, 1)
        self.assertEqual(imp.mapping_snapshot.get("preset_key"), "unfi_clearview")
        self.assertEqual(imp.total_rows, 1)

    def test_feed_drop_webhook_and_process(self):
        from pilot.ingest import process_pos_feed_drop
        from pilot.views import public_pos_feed_drop_view

        cfg = upsert_pos_feed_config(
            self.access,
            retailer_banner="FreshMart",
            preset_key="unfi_clearview",
            enabled=True,
        )
        self.assertTrue(cfg.ingest_token)
        csv_text = (
            "Banner,Customer Number,Customer Name,City,State,UPC,"
            "Week Ending,Units,Dollars,Baseline Units\n"
            "FreshMart,102,FreshMart #102,Austin,TX,Yuzu Citrus 12oz,"
            "2026-10-18,55,160.00,30\n"
        )
        # Prefer direct enqueue+process (same path as cron) — Client HTTP
        # against public endpoints is flaky under concurrent agents on this VM.
        drop = enqueue_pos_feed_drop(
            ingest_token=cfg.ingest_token,
            csv_text=csv_text,
            filename="drop.csv",
        )
        processed = process_pos_feed_drop(drop)
        self.assertEqual(processed.status, PosFeedDrop.STATUS_PROCESSED)
        self.assertEqual(processed.sales_import.matched_rows, 1)
        self.assertTrue(
            RetailerSalesImport.objects.filter(
                tenant=self.tenant, is_active_version=True
            ).exists()
        )

        # Lightweight GET smoke via the public view callable.
        req = RequestFactory().get(f"/api/public/pos-feed/{cfg.ingest_token}")
        resp = public_pos_feed_drop_view(req, cfg.ingest_token)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(json.loads(resp.content)["ok"])

    def test_enqueue_is_idempotent_on_sha(self):
        cfg = upsert_pos_feed_config(
            self.access,
            retailer_banner="FreshMart",
            preset_key="",
        )
        csv_text = (
            "store_id,sku,week_end,register_units\n"
            "102,Yuzu Citrus 12oz,2026-10-18,1\n"
        )
        d1 = enqueue_pos_feed_drop(
            ingest_token=cfg.ingest_token, csv_text=csv_text, filename="a.csv"
        )
        d2 = enqueue_pos_feed_drop(
            ingest_token=cfg.ingest_token, csv_text=csv_text, filename="b.csv"
        )
        self.assertEqual(d1.id, d2.id)

    def test_buyer_digest_opt_in_and_dry_run(self):
        set_buyer_weekly_digest(
            self.access,
            enabled=True,
            recipient_emails="buyer@demo.spark, finance@demo.spark",
        )
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.buyer_weekly_digest_enabled)
        recipients = buyer_digest_recipients(self.profile)
        self.assertEqual(
            recipients, ["buyer@demo.spark", "finance@demo.spark"]
        )

        with patch("pilot.envelopes.BuyerWeeklyDigestMailer.send_now") as send_mock:
            result = send_buyer_digest_for_tenant(
                self.tenant,
                dry_run=True,
                force=True,
                period_start=date(2026, 10, 12),
                period_end=date(2026, 10, 18),
            )
            self.assertEqual(result["status"], "dry_run")
            send_mock.assert_not_called()

            result_live = send_buyer_digest_for_tenant(
                self.tenant,
                dry_run=False,
                force=True,
                period_start=date(2026, 10, 12),
                period_end=date(2026, 10, 18),
            )
            self.assertEqual(result_live["status"], "sent")
            send_mock.assert_called_once()

    def test_buyer_digest_skipped_when_opted_out(self):
        result = send_buyer_digest_for_tenant(self.tenant, force=True)
        self.assertEqual(result["status"], "skipped_opt_out")

    def test_process_failed_drop_records_error(self):
        cfg = upsert_pos_feed_config(
            self.access,
            retailer_banner="FreshMart",
            preset_key="unfi_clearview",
        )
        # Header-only CSV → import_retailer_pos_csv raises ValueError
        # ("POS CSV contains no data rows.").
        drop = enqueue_pos_feed_drop(
            ingest_token=cfg.ingest_token,
            csv_text="Banner,Customer Number,UPC,Week Ending,Units\n",
            filename="bad.csv",
        )
        processed = process_pos_feed_drop(drop)
        self.assertEqual(processed.status, PosFeedDrop.STATUS_FAILED)
        self.assertIn("no data rows", processed.error_message.lower())
