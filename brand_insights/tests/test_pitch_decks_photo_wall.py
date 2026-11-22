"""SPA-35: Live Photo Wall pins + shareable Buyer Pitch Decks.

Covers tenant isolation, passcode hashing, cost stripping on public
payloads, pin uniqueness, and public token resolve (404/403/410).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from unittest.mock import patch

from django.test import Client as HttpClient, TransactionTestCase, override_settings
from django.utils import timezone
from gqlauth.models import UserStatus

from brand_insights.models import BuyerPitchDeck, PhotoWallPin
from brand_insights.photo_wall import toggle_photo_wall_pin
from brand_insights.pitch_decks import (
    create_buyer_pitch_deck,
    hash_passcode,
    list_buyer_pitch_decks,
    locked_pitch_deck_preview,
    resolve_public_pitch_deck,
    revoke_buyer_pitch_deck,
    unlocked_pitch_deck_payload,
    verify_passcode,
)
from events import models as event_models
from pilot.models import WorkspaceMembership
from pilot.permissions import resolve_workspace_access
from pilot.workspaces import create_or_update_provider, upsert_workspace_membership
from recaps import models as recap_models
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


@override_settings(ALLOWED_HOSTS=["*"])
class PitchDeckPhotoWallTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.admin = _make_user(
            "ops-admin-pitch@example.com", Role.SPARK_ADMIN_SLUG
        )
        self.brand_a = Tenant.objects.create(
            name="Brand A Pitch", slug="brand-a-pitch", created_by=self.admin
        )
        self.brand_b = Tenant.objects.create(
            name="Brand B Pitch", slug="brand-b-pitch", created_by=self.admin
        )
        self.brand_admin = _make_user("brand-admin-pitch@example.com")
        staff_a = resolve_workspace_access(self.admin, tenant=self.brand_a)
        upsert_workspace_membership(
            staff_a,
            target_user=self.brand_admin,
            workspace_role=WorkspaceMembership.ROLE_BRAND_ADMIN,
        )
        self.access_a = resolve_workspace_access(
            self.brand_admin, tenant=self.brand_a
        )

        self.provider_user = _make_user("provider-pitch@example.com")
        agency = create_or_update_provider(
            self.access_a,
            name="Agency Pitch Co",
            contact_email="ops@agency-pitch.example",
        )
        upsert_workspace_membership(
            self.access_a,
            target_user=self.provider_user,
            workspace_role=WorkspaceMembership.ROLE_PROVIDER_MANAGER,
            provider=agency,
        )
        self.access_provider = resolve_workspace_access(
            self.provider_user, tenant=self.brand_a
        )

        self.et = event_models.EventType.objects.create(
            name="Retail Sampling Pitch",
            tenant=self.brand_a,
            created_by=self.admin,
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Pitch Retail",
            event_type=self.et,
            tenant=self.brand_a,
            created_by=self.admin,
        )
        self.file_type = recap_models.FileType.objects.create(
            name="image/jpeg", created_by=self.admin
        )
        self.cat_a = recap_models.FileRecapCategory.objects.create(
            name="Sampling photos", tenant=self.brand_a, created_by=self.admin
        )
        self.cat_b = recap_models.FileRecapCategory.objects.create(
            name="Sampling photos", tenant=self.brand_b, created_by=self.admin
        )
        when = timezone.make_aware(datetime(2026, 10, 12, 15))
        self.event = event_models.Event.objects.create(
            name="Sprouts demo",
            tenant=self.brand_a,
            address="1 Main St",
            date=when,
            event_type=self.et,
            created_by=self.admin,
            updated_by=self.admin,
        )
        self.recap = recap_models.CustomRecap.objects.create(
            name="Sprouts recap",
            tenant=self.brand_a,
            event=self.event,
            custom_recap_template=self.template,
            approved=True,
            submitted_at=when,
            created_by=self.admin,
        )
        self.photo = recap_models.CustomRecapFile.objects.create(
            name="demo.jpg",
            url="recap_files/pitch-demo/photo.jpg",
            custom_recap=self.recap,
            file_type=self.file_type,
            file_recap_category=self.cat_a,
            created_by=self.admin,
        )

        other_et = event_models.EventType.objects.create(
            name="Other ET", tenant=self.brand_b, created_by=self.admin
        )
        other_template = recap_models.CustomRecapTemplate.objects.create(
            name="Other Retail",
            event_type=other_et,
            tenant=self.brand_b,
            created_by=self.admin,
        )
        other_event = event_models.Event.objects.create(
            name="Other",
            tenant=self.brand_b,
            address="2 Main St",
            date=when,
            event_type=other_et,
            created_by=self.admin,
            updated_by=self.admin,
        )
        other_recap = recap_models.CustomRecap.objects.create(
            name="Other recap",
            tenant=self.brand_b,
            event=other_event,
            custom_recap_template=other_template,
            approved=True,
            submitted_at=when,
            created_by=self.admin,
        )
        self.other_photo = recap_models.CustomRecapFile.objects.create(
            name="other.jpg",
            url="recap_files/other/photo.jpg",
            custom_recap=other_recap,
            file_type=self.file_type,
            file_recap_category=self.cat_b,
            created_by=self.admin,
        )

    def test_passcode_hash_roundtrip_and_reject(self):
        hashed = hash_passcode("buyer-secret")
        self.assertTrue(hashed)
        self.assertTrue(verify_passcode("buyer-secret", hashed))
        self.assertFalse(verify_passcode("wrong", hashed))
        self.assertFalse(verify_passcode("", hashed))
        with self.assertRaises(ValueError):
            hash_passcode("ab")

    def test_provider_cannot_pin_or_create_deck(self):
        with self.assertRaises(PermissionError):
            toggle_photo_wall_pin(
                self.access_provider, photo_id=self.photo.id, pinned=True
            )
        with self.assertRaises(PermissionError):
            create_buyer_pitch_deck(self.access_provider, title="Nope")

    def test_pin_toggle_unique_and_tenant_scoped(self):
        out = toggle_photo_wall_pin(
            self.access_a, photo_id=self.photo.id, pinned=True, note="Endcap"
        )
        self.assertTrue(out["is_pinned"])
        self.assertEqual(PhotoWallPin.objects.filter(tenant=self.brand_a).count(), 1)
        out2 = toggle_photo_wall_pin(
            self.access_a, photo_id=self.photo.id, pinned=True, note="Cooler"
        )
        self.assertTrue(out2["is_pinned"])
        self.assertEqual(out2["pin_note"], "Cooler")
        self.assertEqual(PhotoWallPin.objects.filter(tenant=self.brand_a).count(), 1)
        with self.assertRaises(PermissionError):
            toggle_photo_wall_pin(
                self.access_a, photo_id=self.other_photo.id, pinned=True
            )
        out3 = toggle_photo_wall_pin(
            self.access_a, photo_id=self.photo.id, pinned=False
        )
        self.assertFalse(out3["is_pinned"])
        self.assertEqual(PhotoWallPin.objects.filter(tenant=self.brand_a).count(), 0)

    def _fake_report(self, cost: str = "250.00"):
        return {
            "period_start": (date.today() - timedelta(days=27)).isoformat(),
            "period_end": date.today().isoformat(),
            "pos_available": False,
            "total_visits": 2,
            "total_supported_cost": cost,
            "rows": [
                {
                    "store_key": "sprouts:101",
                    "store_label": "Sprouts #101",
                    "retailer_banner": "Sprouts Farmers Market",
                    "market": "Austin, TX",
                    "providers": [],
                    "visit_count": 2,
                    "supported_brand_cost": cost,
                    "total_brand_cost": cost,
                    "has_incomplete_cost": False,
                    "samples_distributed": 40,
                    "rep_observed_units": 12,
                    "matched_register_units": None,
                    "adjusted_register_units": None,
                    "pos_coverage_label": "POS feed not connected",
                    "recommendation_id": None,
                    "recommendation_code": None,
                    "recommendation_summary": "Strong endcap",
                    "current_action_uuid": None,
                    "current_action_type": None,
                    "current_action_status": None,
                    "current_action_owner": None,
                    "current_action_provider": None,
                }
            ],
        }

    def _fake_pos(self):
        return {
            "available": False,
            "unavailable_message": "No POS",
            "retailer_banners": [],
            "coverage": {},
            "totals": {},
            "store_weeks": [],
        }

    def test_create_deck_strips_costs_by_default_and_public_resolve(self):
        toggle_photo_wall_pin(self.access_a, photo_id=self.photo.id, pinned=True)

        with (
            patch(
                "brand_insights.pitch_decks.build_weekly_buyer_store_report",
                return_value=self._fake_report(),
            ),
            patch(
                "brand_insights.pitch_decks.build_retailer_pos_measurement",
                return_value=self._fake_pos(),
            ),
            patch(
                "brand_insights.photo_wall._image_url",
                return_value="https://example.com/photo.jpg",
            ),
        ):
            deck = create_buyer_pitch_deck(
                self.access_a,
                title="Sprouts Line Review",
                buyer_name="Category Buyer",
                passcode="open-sesame",
                expires_in_days=14,
                include_costs=False,
            )

        self.assertFalse(deck.include_costs)
        self.assertTrue(deck.has_passcode)
        self.assertEqual(len(list_buyer_pitch_decks(self.access_a)), 1)

        found, reason, status = resolve_public_pitch_deck(deck.token)
        self.assertEqual(status, 200)
        self.assertIsNone(reason)
        self.assertEqual(found.pk, deck.pk)

        locked = locked_pitch_deck_preview(deck)
        self.assertTrue(locked["requiresPasscode"])
        self.assertNotIn("kpis", locked)
        self.assertNotIn("photos", locked)

        self.assertEqual(deck.pinned_photo_ids, [self.photo.id])
        # Public reads remint interactive URLs from durable blob ids — patch
        # the URL helper outside create so stale snapshot tokens are unused.
        with patch(
            "brand_insights.photo_wall._image_url",
            return_value="https://example.com/photo-reminted.jpg",
        ):
            payload = unlocked_pitch_deck_payload(deck)
        self.assertEqual(payload["title"], "Sprouts Line Review")
        self.assertNotIn("totalSupportedCost", payload["kpis"])
        self.assertFalse(payload["kpis"]["includeCosts"])
        for row in payload["retailerBreakdown"]:
            self.assertNotIn("supportedCost", row)
        self.assertGreaterEqual(len(payload["photos"]), 1)
        self.assertEqual(
            payload["photos"][0]["url"],
            "https://example.com/photo-reminted.jpg",
        )

        http = HttpClient()
        locked_res = http.get(f"/api/public/pitch/{deck.token}/")
        self.assertEqual(locked_res.status_code, 200)
        locked_body = locked_res.json()
        self.assertTrue(locked_body["locked"])
        self.assertTrue(locked_body["requiresPasscode"])

        bad = http.post(
            f"/api/public/pitch/{deck.token}/",
            data=json.dumps({"passcode": "wrong"}),
            content_type="application/json",
        )
        self.assertEqual(bad.status_code, 403)

        with patch(
            "brand_insights.photo_wall._image_url",
            return_value="https://example.com/photo-reminted.jpg",
        ):
            ok = http.post(
                f"/api/public/pitch/{deck.token}/",
                data=json.dumps({"passcode": "open-sesame"}),
                content_type="application/json",
            )
        self.assertEqual(ok.status_code, 200)
        body = ok.json()
        self.assertTrue(body["ok"])
        self.assertFalse(body["locked"])
        self.assertNotIn("totalSupportedCost", body["kpis"])
        self.assertGreaterEqual(len(body["photos"]), 1)

        revoke_buyer_pitch_deck(self.access_a, deck_uuid=str(deck.uuid))
        revoked = http.get(f"/api/public/pitch/{deck.token}/")
        self.assertEqual(revoked.status_code, 403)
        self.assertEqual(revoked.json()["error"], "revoked")

    def test_expired_and_missing_tokens(self):
        deck = BuyerPitchDeck.objects.create(
            tenant=self.brand_a,
            token="pd_expired_token_test_xyz",
            title="Expired",
            period_start=date.today() - timedelta(days=7),
            period_end=date.today(),
            expires_at=timezone.now() - timedelta(hours=1),
            snapshot_payload={"brand": {"name": "Brand A"}, "kpis": {}, "photos": []},
            created_by=self.brand_admin,
        )
        _d, reason, status = resolve_public_pitch_deck(deck.token)
        self.assertEqual(reason, "expired")
        self.assertEqual(status, 410)
        _d2, reason2, status2 = resolve_public_pitch_deck("pd_missing")
        self.assertEqual(reason2, "not_found")
        self.assertEqual(status2, 404)

    def test_include_costs_opt_in_keeps_cost_fields(self):
        with (
            patch(
                "brand_insights.pitch_decks.build_weekly_buyer_store_report",
                return_value=self._fake_report("100.00"),
            ),
            patch(
                "brand_insights.pitch_decks.build_retailer_pos_measurement",
                return_value=self._fake_pos(),
            ),
        ):
            deck = create_buyer_pitch_deck(
                self.access_a,
                title="With costs",
                include_costs=True,
            )
        payload = unlocked_pitch_deck_payload(deck)
        self.assertTrue(payload["includeCosts"])
        self.assertEqual(payload["kpis"]["totalSupportedCost"], "100.00")
        self.assertIsNotNone(payload["kpis"]["costPerSample"])

    def test_scoped_empty_report_excludes_tenant_wide_pos(self):
        empty_report = {
            **self._fake_report(),
            "total_visits": 0,
            "rows": [],
        }
        pos_with_other_store = {
            "available": True,
            "unavailable_message": None,
            "retailer_banners": ["Other"],
            "coverage": {
                "matched_store_sku_weeks": 2,
                "eligible_store_sku_weeks": 2,
                "coverage_ratio": 1.0,
            },
            "totals": {},
            "store_weeks": [
                {
                    "store_key": "other:999",
                    "sku_cells": [
                        {
                            "status": "matched",
                            "incremental_lift_units": "12.0",
                            "baseline_units": "10.0",
                        }
                    ],
                }
            ],
        }
        with (
            patch(
                "brand_insights.pitch_decks.build_weekly_buyer_store_report",
                return_value=empty_report,
            ),
            patch(
                "brand_insights.pitch_decks.build_retailer_pos_measurement",
                return_value=pos_with_other_store,
            ),
        ):
            deck = create_buyer_pitch_deck(
                self.access_a,
                title="Empty Sprouts filter",
                retailer_filter="Sprouts",
            )
        kpis = deck.snapshot_payload["kpis"]
        self.assertIsNone(kpis["samplingLiftPct"])
        self.assertIsNone(kpis["incrementalLiftUnits"])
        self.assertIn("No stores", kpis["posCoverageLabel"])

    def test_passcode_attempts_are_rate_limited(self):
        with (
            patch(
                "brand_insights.pitch_decks.build_weekly_buyer_store_report",
                return_value=self._fake_report(),
            ),
            patch(
                "brand_insights.pitch_decks.build_retailer_pos_measurement",
                return_value=self._fake_pos(),
            ),
        ):
            deck = create_buyer_pitch_deck(
                self.access_a,
                title="Rate limit deck",
                passcode="secret-code",
            )
        http = HttpClient()
        with patch(
            "brand_insights.pitch_views.hit",
            return_value=type(
                "R",
                (),
                {
                    "allowed": False,
                    "retry_after": 42,
                    "count": 99,
                    "limit": 20,
                    "window_seconds": 900,
                    "degraded": False,
                    "key": "x",
                },
            )(),
        ):
            res = http.post(
                f"/api/public/pitch/{deck.token}/",
                data=json.dumps({"passcode": "wrong"}),
                content_type="application/json",
            )
        self.assertEqual(res.status_code, 429)
        self.assertEqual(res.json()["error"], "rate_limited")
        self.assertEqual(res["Retry-After"], "42")

    def test_public_payload_remints_photos_from_ids(self):
        toggle_photo_wall_pin(self.access_a, photo_id=self.photo.id, pinned=True)
        with (
            patch(
                "brand_insights.pitch_decks.build_weekly_buyer_store_report",
                return_value=self._fake_report(),
            ),
            patch(
                "brand_insights.pitch_decks.build_retailer_pos_measurement",
                return_value=self._fake_pos(),
            ),
            patch(
                "brand_insights.photo_wall._image_url",
                return_value="https://example.com/stale-token.jpg",
            ),
        ):
            deck = create_buyer_pitch_deck(
                self.access_a,
                title="Remint photos",
            )
        # Stale interactive URL left in the snapshot on purpose.
        deck.snapshot_payload["photos"] = [
            {
                "id": str(self.photo.id),
                "url": "https://example.com/expired-interactive-token.jpg",
                "category": "Sampling photos",
                "caption": "",
                "store": "Sprouts",
                "retailer": "Sprouts",
                "market": "",
                "state": "",
                "eventDate": "",
                "skus": [],
            }
        ]
        deck.save(update_fields=["snapshot_payload"])
        with patch(
            "brand_insights.photo_wall._image_url",
            return_value="https://example.com/fresh-interactive.jpg",
        ):
            payload = unlocked_pitch_deck_payload(deck)
        self.assertEqual(len(payload["photos"]), 1)
        self.assertEqual(
            payload["photos"][0]["url"],
            "https://example.com/fresh-interactive.jpg",
        )
