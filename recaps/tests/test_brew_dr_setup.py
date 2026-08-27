"""Brew Dr. Kombucha check-in photo buckets: seeder shape + apply."""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command
from django.test import Client, override_settings

from recaps.management.commands.setup_brew_dr_checkin import (
    CODE_PREFIX,
    PHOTO_BUCKETS,
)
from tenants.tests.base import BaseGraphQLTestCase

VALID_SECRET = "test-cron-secret-value-only-for-tests"
BREW_DR_CRON_URL = "/internal/cron/setup-brew-dr-checkin"


class TestBrewDrPhotoBucketSpec:
    def test_photo_buckets_match_kyles_retail_sampling_shot_list(self):
        assert [b["name"] for b in PHOTO_BUCKETS] == [
            "Set Before",
            "Set After",
            "Demo Table Before Demo (Far Back)",
            "Demo Table (Close Up)",
            "Demo Table Area",
            "Displays (if applicable)",
        ]

    def test_required_buckets_carry_min_one(self):
        required = PHOTO_BUCKETS[:-1]
        assert all(b.get("min") == 1 for b in required)
        assert "min" not in PHOTO_BUCKETS[-1]

    def test_code_prefix_is_brand_scoped(self):
        assert CODE_PREFIX == "BD-"


@pytest.mark.django_db(transaction=True)
class TestBrewDrSetupCommand(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from events.models import EventType

        self.roles = self.setup_default_roles()
        self.user = self.get_system_user()
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        self.tenant = self.create_tenant(name="Brew Dr. Kombucha", slug="brew-dr")
        self.event_type = EventType.objects.create(
            name="Retail Sampling", tenant=self.tenant, created_by=self.user
        )

    def _run(self, **kw):
        out = io.StringIO()
        call_command("setup_brew_dr_checkin", stdout=out, **kw)
        return out.getvalue()

    def test_dry_run_creates_nothing(self):
        from recaps.models import FileRecapCategory

        log = self._run(tenant="brew")
        assert "DRY-RUN" in log
        assert "Set Before" in log
        assert not FileRecapCategory.objects.filter(tenant=self.tenant).exists()
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_photo_buckets is None

    def test_apply_sets_buckets_and_pins_program(self):
        from recaps.models import FileRecapCategory

        log = self._run(tenant="brew", apply=True)
        assert "checkin_photo_buckets set" in log
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_photo_buckets == PHOTO_BUCKETS
        assert self.tenant.checkin_event_type_id == self.event_type.id
        assert list(
            self.tenant.checkin_event_types.values_list("name", flat=True)
        ) == ["Retail Sampling"]
        for bucket in PHOTO_BUCKETS:
            assert FileRecapCategory.objects.filter(
                tenant=self.tenant, name=bucket["name"]
            ).exists()
        assert self.tenant.checkin_code and self.tenant.checkin_code.startswith("BD-")

    def test_apply_is_idempotent_when_code_already_set(self):
        self.tenant.checkin_code = "BD-AQRACD"
        self.tenant.save(update_fields=["checkin_code"])
        log = self._run(tenant="brew", apply=True)
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_code == "BD-AQRACD"
        assert "already set" in log
        assert self.tenant.checkin_photo_buckets == PHOTO_BUCKETS

    def test_serialize_photo_buckets_for_event(self):
        from django.utils import timezone
        from events.models import Event

        from ambassadors import checkin_web

        self._run(tenant="brew", apply=True)
        self.tenant.refresh_from_db()
        event = Event.objects.create(
            name="HEB Demo",
            tenant=self.tenant,
            address="123 Main St",
            event_type=self.event_type,
            date=timezone.now(),
            created_by=self.user,
        )
        buckets = checkin_web.serialize_photo_buckets(event)
        assert [b["name"] for b in buckets] == [b["name"] for b in PHOTO_BUCKETS]
        mins = {b["name"]: b["min"] for b in buckets}
        assert mins["Set Before"] == 1
        assert mins["Displays (if applicable)"] == 0


@pytest.mark.django_db
class TestBrewDrSetupCronView:
    def test_valid_secret_fires_command(self):
        client = Client()
        with override_settings(INTERNAL_CRON_SECRET=VALID_SECRET):
            from unittest.mock import patch

            with patch("digest.cron_views.call_command") as mock_call:
                resp = client.post(
                    BREW_DR_CRON_URL,
                    {"tenant": "brew"},
                    HTTP_X_CRON_SECRET=VALID_SECRET,
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["apply"] is False
        mock_call.assert_called_once()
        assert mock_call.call_args[0][0] == "setup_brew_dr_checkin"
