"""Field-by-field roll-up of every numeric recap answer."""

from recaps.recap_field_totals import field_kind, parse_number, rollup_field_values


def _rows(*items):
    # (section, label, value, recap_id)
    return [(s, 0, label, i, v, rid) for i, (s, label, v, rid) in enumerate(items)]


def test_parse_number_accepts_plain_numbers_only():
    assert parse_number("4,000") == (4000.0, False)
    assert parse_number("$1,253.42") == (1253.42, False)
    assert parse_number("2.5%") == (2.5, True)
    assert parse_number("about 40") is None
    assert parse_number("5:00pm") is None
    assert parse_number("") is None


def test_field_kind():
    assert field_kind("Total number of consumers sampled", False) == "count"
    assert field_kind("Total spend on the card", False) == "money"
    assert field_kind("Costco retail price: Almond Oil", False) == "average"
    assert field_kind("Foot traffic level", False) == "average"
    assert field_kind("Conversion", True) == "average"


def test_counts_sum_across_recaps_and_keep_form_order():
    out = rollup_field_values(
        _rows(
            ("Sampling", "Total number of consumers sampled", "4,000", 1),
            ("Sales", "Units sold: Almond Oil", "44", 1),
            ("Sampling", "Total number of consumers sampled", "350", 2),
            ("Sales", "Units sold: Almond Oil", "6", 2),
        )
    )
    by = {s["section"]: s["fields"] for s in out}
    sampled = by["Sampling"][0]
    assert sampled["label"] == "Total number of consumers sampled"
    assert sampled["kind"] == "count" and sampled["total"] == 4350
    assert sampled["recaps"] == 2
    assert by["Sales"][0]["total"] == 50


def test_prices_and_ratings_are_averaged_not_summed():
    out = rollup_field_values(
        _rows(
            ("Sales", "Retail price", "12.79", 1),
            ("Sales", "Retail price", "13.79", 2),
            ("Visit", "Foot traffic level", "4", 1),
            ("Visit", "Foot traffic level", "2", 2),
        )
    )
    price = out[0]["fields"][0]
    assert price["kind"] == "average" and round(price["total"], 2) == 13.29
    assert out[1]["fields"][0]["total"] == 3


def test_free_text_and_identifiers_are_left_out():
    out = rollup_field_values(
        _rows(
            ("Feedback", "What did members say?", "Loved it", 1),
            ("Feedback", "What did members say?", "Too strong", 2),
            ("Visit", "Store number", "1715", 1),
            ("Visit", "Zip code", "92346", 1),
            # Mostly prose with one stray number: not a metric.
            ("Visit", "Anything to change?", "No", 1),
            ("Visit", "Anything to change?", "No", 2),
            ("Visit", "Anything to change?", "2", 3),
        )
    )
    assert out == []


def test_blank_answers_do_not_count_against_a_field():
    out = rollup_field_values(
        _rows(
            ("Sampling", "Samples handed out", "", 1),
            ("Sampling", "Samples handed out", "120", 2),
        )
    )
    assert out[0]["fields"][0]["total"] == 120
    assert out[0]["fields"][0]["recaps"] == 1


# ─── Through the database and the real schema ───────────────────────

from datetime import datetime  # noqa: E402

import pytest  # noqa: E402
from asgiref.sync import sync_to_async  # noqa: E402
from django.utils import timezone  # noqa: E402

from ambassadors.tests.base import AmbassadorsGraphQLTestCase  # noqa: E402
from config.schema_client import schema_clients  # noqa: E402
from events import models as event_models  # noqa: E402
from recaps import models as recap_models  # noqa: E402

TOTALS_Q = """
query T($tid: ID!, $s: String, $e: String) {
  tenantRecapFieldTotals(tenantId: $tid, startDate: $s, endDate: $e) {
    section
    fields { label kind total average recaps isPercent }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestRecapFieldTotalsSchema(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Totals Brand")
        self.other = self.create_tenant(name="Totals Other")
        self.et = event_models.EventType.objects.create(
            name="Retail Sampling", slug="totals-retail", tenant=self.tenant,
            created_by=self.sys,
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Totals Retail", event_type=self.et, tenant=self.tenant,
            created_by=self.sys,
        )
        self.num = recap_models.CustomRecapFieldType.objects.create(
            name="Number", created_by=self.sys
        )
        self.section = recap_models.RecapSection.objects.create(
            name="Sampling", tenant=self.tenant, created_by=self.sys
        )
        self.field = recap_models.CustomField.objects.create(
            name="Samples handed out",
            custom_recap_template=self.template,
            custom_field_type=self.num,
            recap_section=self.section,
            created_by=self.sys,
        )

    def _recap(self, value, *, day, approved=True, archived=False, tenant=None):
        tenant = tenant or self.tenant
        when = timezone.make_aware(datetime(2026, 10, day, 12))
        event = event_models.Event.objects.create(
            name="walk-in", tenant=tenant, address="1 Main St", date=when,
            event_type=self.et, created_by=self.sys, updated_by=self.sys,
        )
        recap = recap_models.CustomRecap.objects.create(
            name=f"r{day}", approved=approved, submitted_at=when, event=event,
            tenant=tenant, custom_recap_template=self.template,
            archived_at=timezone.now() if archived else None,
            created_by=self.sys,
        )
        recap_models.CustomFieldValue.objects.create(
            custom_recap=recap, custom_field=self.field, value=value,
            created_by=self.sys,
        )
        return recap

    @pytest.mark.asyncio
    async def test_totals_only_approved_live_recaps_in_window_for_own_brand(self):
        def _seed():
            self._recap("100", day=5)
            self._recap("1,250", day=20)
            self._recap("999", day=6, approved=False)   # needs review
            self._recap("888", day=7, archived=True)    # archived
            self._recap("777", day=8, tenant=self.other)  # another brand
            user = self.create_user(
                username="c@totals.com", email="c@totals.com",
                role=self.roles["client"], password="password123",
            )
            self.create_tenanted_user(user=user, tenant=self.tenant)
            return user

        me = await sync_to_async(_seed)()

        res = await self._execute_mutation(
            TOTALS_Q, {"tid": str(self.tenant.id)}, self.endpoint_path, user=me
        )
        assert res.errors is None, res.errors
        out = res.data["tenantRecapFieldTotals"]
        assert [s["section"] for s in out] == ["Sampling"]
        field = out[0]["fields"][0]
        assert field["label"] == "Samples handed out"
        assert field["kind"] == "count"
        assert field["total"] == 1350
        assert field["recaps"] == 2

        windowed = await self._execute_mutation(
            TOTALS_Q,
            {"tid": str(self.tenant.id), "s": "2026-10-01", "e": "2026-10-10"},
            self.endpoint_path,
            user=me,
        )
        assert windowed.data["tenantRecapFieldTotals"][0]["fields"][0]["total"] == 100

        # A client asking for another brand is pinned to their own.
        other = await self._execute_mutation(
            TOTALS_Q, {"tid": str(self.other.id)}, self.endpoint_path, user=me
        )
        assert other.data["tenantRecapFieldTotals"][0]["fields"][0]["total"] == 1350
