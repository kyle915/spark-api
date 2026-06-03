"""Coverage for repair_request_activation_time — sets a request's LOCAL
activation time and stores the correct UTC. The sheet re-sync is stubbed."""

from __future__ import annotations

import datetime as dt
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.utils import timezone as djtz

from events import models as event_models
from events.tests.base import EventsGraphQLTestCase

UPSERT_PATH = (
    "utils.sheets_mirror.upsert_request_row"
)


@pytest.mark.django_db
class TestRepairRequestActivationTime(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Liquid Death")
        self.request_type = self.create_request_type(
            name="Retail Sampling", tenant=self.tenant
        )
        self.pacific = event_models.TimeZone.objects.create(
            name="Pacific Time", code="PST", offset=-8, created_by=self.system_user
        )
        # Stored 12h off: 3:00 AM PDT == 10:00 UTC on 2026-06-13.
        self.start_3am_utc = djtz.make_aware(
            dt.datetime(2026, 6, 13, 10, 0), dt.timezone.utc
        )
        self.req = event_models.Request.objects.create(
            name="Walmart 5886 Encinitas",
            address="1550 Leucadia Blvd Encinitas CA 92024",
            request_type=self.request_type,
            tenant=self.tenant,
            timezone=self.pacific,
            date=self.start_3am_utc,
            start_time=self.start_3am_utc,
            end_time=self.start_3am_utc + dt.timedelta(hours=3),
            created_by=self.system_user,
        )

    def test_execute_sets_local_pm_and_stores_correct_utc(self):
        out = StringIO()
        with patch(UPSERT_PATH, return_value=True) as mock_upsert:
            call_command(
                "repair_request_activation_time",
                request=self.req.id,
                start_local="15:00",
                end_local="18:00",
                execute=True,
                stdout=out,
            )
            mock_upsert.assert_called_once()
        self.req.refresh_from_db()
        # 3 PM PDT == 22:00 UTC; 6 PM PDT == 01:00 UTC next day.
        assert self.req.start_time == djtz.make_aware(
            dt.datetime(2026, 6, 13, 22, 0), dt.timezone.utc
        )
        assert self.req.end_time == djtz.make_aware(
            dt.datetime(2026, 6, 14, 1, 0), dt.timezone.utc
        )
        assert "mode=execute" in out.getvalue()

    def test_dry_run_writes_nothing(self):
        out = StringIO()
        with patch(UPSERT_PATH) as mock_upsert:
            call_command(
                "repair_request_activation_time",
                request=self.req.id,
                start_local="15:00",
                end_local="18:00",
                stdout=out,
            )
            mock_upsert.assert_not_called()
        self.req.refresh_from_db()
        assert self.req.start_time == self.start_3am_utc  # untouched
        assert "DRY RUN" in out.getvalue()
        assert "mode=dry-run" in out.getvalue()
