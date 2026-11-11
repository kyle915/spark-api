"""One recap per store per day in the all-metrics roll-up."""

from __future__ import annotations

from datetime import datetime

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from config.schema_client import schema_clients
from events import models as event_models
from recaps import models as recap_models
from recaps.recap_field_totals import (
    booth_day_key,
    collapse_booth_days,
    rollup_field_values,
)
from tenants.models import Tenant


def _rows(*items):
    # (label, value, recap_id, booth)
    return [("Sampling", 0, label, i, value, rid, booth) for i, (label, value, rid, booth) in enumerate(items)]


def _field(out, label):
    for sec in out:
        for f in sec["fields"]:
            if f["label"] == label:
                return f
    raise AssertionError(f"{label} not in output")


def test_booth_day_key_matches_the_ways_one_booth_gets_typed():
    when = timezone.make_aware(datetime(2026, 10, 11, 12))
    # Real variants filed for the same Costco booth.
    highland = [
        "28000 Greenspot Rd, Highland, CA 92346",
        "28000 Greenspot Road, Highland, California 92346 (Costco)",
        "Greenspot Road, Highland, California 92346",
        "28000 Greenspot Road, Highland, California 92346 recap",
    ]
    assert {booth_day_key(a, "", None, when) for a in highland} == {"z92346|2026-10-11"}
    # The leading street number is not mistaken for a ZIP.
    assert booth_day_key("28000 Greenspot Rd, Highland", "", None, when) == (
        "a28000 greenspot road highland|2026-10-11"
    )
    assert booth_day_key("28000 GREENSPOT ROAD,  Highland", "", None, when) == booth_day_key(
        "28000 Greenspot Rd, Highland", "", None, when
    )
    # Different stores, or different days, stay apart.
    assert booth_day_key("520 N Lone Hill Ave, San Dimas, CA 91773", "", None, when) != (
        "z92346|2026-10-11"
    )
    other_day = timezone.make_aware(datetime(2026, 10, 12, 12))
    assert booth_day_key(highland[0], "", None, other_day) == "z92346|2026-10-12"
    # No ZIP: coordinates, rounded.
    assert booth_day_key("", "", [34.1092, -117.2101], when) == "g34.11,-117.21|2026-10-11"
    # Nothing to go on, or no date: no booth-day.
    assert booth_day_key("", "", None, when) == ""
    assert booth_day_key(highland[0], "", None, None) == ""


def test_two_recaps_for_one_booth_day_count_once_using_the_larger_value():
    rows = _rows(
        ("Total number of consumers sampled", "4000", 1, "r1|2026-10-11"),
        ("Total number of consumers sampled", "3,900", 2, "r1|2026-10-11"),  # second BA
        ("Total number of consumers sampled", "1200", 3, "r1|2026-10-12"),
    )
    out = rollup_field_values(collapse_booth_days(rows))
    sampled = _field(out, "Total number of consumers sampled")
    assert sampled["total"] == 5200  # 4000 + 1200, not 9100
    assert sampled["recaps"] == 2  # two booth-days

    plain = rollup_field_values([r[:6] for r in rows])
    assert _field(plain, "Total number of consumers sampled")["total"] == 9100


def test_a_blank_or_partial_recap_does_not_pull_the_day_down():
    rows = _rows(
        ("Units sold", "54", 1, "r1|2026-10-11"),
        ("Units sold", "0", 2, "r1|2026-10-11"),
        ("Units sold", "", 3, "r1|2026-10-11"),
    )
    out = rollup_field_values(collapse_booth_days(rows))
    assert _field(out, "Units sold")["total"] == 54


def test_prices_average_within_the_day_and_different_stores_stay_separate():
    rows = _rows(
        ("Retail price", "12.79", 1, "r1|2026-10-11"),
        ("Retail price", "13.79", 2, "r1|2026-10-11"),
        ("Units sold", "10", 1, "r1|2026-10-11"),
        ("Units sold", "10", 4, "r2|2026-10-11"),  # another store, same day
    )
    out = rollup_field_values(collapse_booth_days(rows))
    assert round(_field(out, "Retail price")["total"], 2) == 13.29
    assert _field(out, "Units sold")["total"] == 20


def test_recaps_without_a_booth_key_are_not_merged_together():
    rows = _rows(
        ("Units sold", "5", 1, ""),
        ("Units sold", "7", 2, ""),
    )
    out = rollup_field_values(collapse_booth_days(rows))
    assert _field(out, "Units sold")["total"] == 12


TOTALS_Q = """
query T($tid: ID!) {
  tenantBoothDayCounting(tenantId: $tid)
  tenantRecapFieldTotals(tenantId: $tid) { section fields { label total recaps } }
}
"""
SET_M = """
mutation S($input: SetBoothDayCountingInput!) {
  setBoothDayCounting(input: $input) { success message enabled }
}
"""


@pytest.mark.django_db(transaction=True)
class TestBoothDayCountingSchema(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Booth Brand")
        self.et = event_models.EventType.objects.create(
            name="Roadshow", slug="booth-roadshow", tenant=self.tenant, created_by=self.sys,
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Roadshow Recap", event_type=self.et, tenant=self.tenant, created_by=self.sys,
        )
        self.num = recap_models.CustomRecapFieldType.objects.create(
            name="Number", created_by=self.sys
        )
        self.section = recap_models.RecapSection.objects.create(
            name="Sampling", tenant=self.tenant, created_by=self.sys
        )
        self.field = recap_models.CustomField.objects.create(
            name="Total number of consumers sampled",
            custom_recap_template=self.template, custom_field_type=self.num,
            recap_section=self.section, created_by=self.sys,
        )

    def _recap(self, value, *, day, address="28000 Greenspot Rd, Highland"):
        when = timezone.make_aware(datetime(2026, 10, day, 12))
        event = event_models.Event.objects.create(
            name="walk-in", tenant=self.tenant, address=address, date=when,
            event_type=self.et, created_by=self.sys, updated_by=self.sys,
        )
        recap = recap_models.CustomRecap.objects.create(
            name=f"r{day}", approved=True, submitted_at=when, event=event,
            tenant=self.tenant, custom_recap_template=self.template, created_by=self.sys,
        )
        recap_models.CustomFieldValue.objects.create(
            custom_recap=recap, custom_field=self.field, value=value, created_by=self.sys,
        )

    @pytest.mark.asyncio
    async def test_switch_is_staff_only_and_changes_the_totals(self):
        def _seed():
            # Two ambassadors, same booth, same day — each on their own event.
            self._recap("4000", day=11)
            self._recap("4000", day=11, address="28000 GREENSPOT RD,  Highland")
            self._recap("1000", day=12)
            staff = self.create_user(
                username="s@igniteproductions.co", email="s@igniteproductions.co",
                role=self.roles["spark_admin"], password="password123",
            )
            client = self.create_user(
                username="c@booth.com", email="c@booth.com",
                role=self.roles["client"], password="password123",
            )
            self.create_tenanted_user(user=client, tenant=self.tenant)
            return staff, client

        staff, client = await sync_to_async(_seed)()
        tid = str(self.tenant.id)

        before = await self._execute_mutation(TOTALS_Q, {"tid": tid}, self.endpoint_path, user=client)
        assert before.errors is None, before.errors
        assert before.data["tenantBoothDayCounting"] is False
        assert before.data["tenantRecapFieldTotals"][0]["fields"][0]["total"] == 9000

        denied = await self._execute_mutation(
            SET_M, {"input": {"tenantId": tid, "enabled": True}}, self.endpoint_path, user=client
        )
        assert denied.data["setBoothDayCounting"]["success"] is False
        flag = await sync_to_async(
            lambda: Tenant.objects.get(id=self.tenant.id).count_one_recap_per_booth_day
        )()
        assert flag is False

        ok = await self._execute_mutation(
            SET_M, {"input": {"tenantId": tid, "enabled": True}}, self.endpoint_path, user=staff
        )
        assert ok.errors is None, ok.errors
        assert ok.data["setBoothDayCounting"] == {
            "success": True,
            "message": "Counting one recap per store per day.",
            "enabled": True,
        }

        after = await self._execute_mutation(TOTALS_Q, {"tid": tid}, self.endpoint_path, user=client)
        assert after.data["tenantBoothDayCounting"] is True
        field = after.data["tenantRecapFieldTotals"][0]["fields"][0]
        assert field["total"] == 5000  # 4000 (one booth-day) + 1000
        assert field["recaps"] == 2
