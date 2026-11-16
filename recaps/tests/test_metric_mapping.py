"""Recap field → standard metric mapping.

What these prove:

  * a staff-confirmed field is counted by that confirmation, whatever it is
    called, and a field confirmed as "not a standard number" stops being
    counted even when its wording matches the vocabulary;
  * a tenant with nothing confirmed reads exactly what it read before — the
    dashboard query, with the front end's own field set, is identical;
  * the model is sent field labels, types and options only, its proposal is
    checked in code, and nothing is stored until a staff member saves;
  * a brand user cannot read or write a mapping.

The model is never called: ``utils.ai_text._post_json`` is mocked.
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from asgiref.sync import sync_to_async
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import metric_mapping as mm
from recaps import models as recap_models
from recaps.types import (
    _consumers_sampled_from_fields,
    _engagement_totals_from_field_pairs,
    _samples_given_from_fields,
    _sold_unit_lines,
    _sold_units_from_fields,
)


# ─── Matchers: pure, no DB ──────────────────────────────────────────


def test_untagged_pairs_take_the_vocabulary_path():
    pairs = [
        ("Total number of consumers sampled", "350"),
        ("Single Cans", "100"),
        ("Packs Sold", "20"),
        ("Willing to purchase", "40"),
        ("Total Samples Given Out", "300"),
    ]
    assert _consumers_sampled_from_fields(pairs) == 350
    assert _sold_units_from_fields(pairs) == 120
    assert _samples_given_from_fields(pairs) == 300
    assert _engagement_totals_from_field_pairs(pairs)["willing_to_purchase_consumers"] == 40
    decided, undecided = mm.split_decided(pairs)
    assert decided == [] and undecided == pairs


def test_confirmed_field_counts_whatever_it_is_called():
    pairs = [
        (mm.MappedName("People who tried it", mm.CONSUMERS_SAMPLED), "40"),
        (mm.MappedName("Bottles moved", mm.UNITS_SOLD_SINGLE), "12"),
        (mm.MappedName("Cases moved", mm.UNITS_SOLD_PACK, 6), "3"),
        (mm.MappedName("Said they would buy", mm.WILLING_TO_BUY), "20"),
        (mm.MappedName("Already heard of us", mm.BRAND_AWARE), "10"),
        (mm.MappedName("New to the brand", mm.FIRST_TIME), "25"),
        (mm.MappedName("Tasters handed out", mm.SAMPLES_GIVEN), "44"),
    ]
    assert _consumers_sampled_from_fields(pairs) == 40
    assert _sold_units_from_fields(pairs) == 15
    assert _samples_given_from_fields(pairs) == 44
    assert _engagement_totals_from_field_pairs(pairs) == {
        "total_consumer": 40,
        "first_time_consumers": 25,
        "brand_aware_consumers": 10,
        "willing_to_purchase_consumers": 20,
    }


def test_confirmed_mapping_overrides_the_vocabulary():
    # Worded like sold / sampled / willing, but staff said otherwise.
    pairs = [
        (mm.MappedName("Single Cans", mm.NONE), "9"),
        (mm.MappedName("Total consumers sampled", mm.NONE), "500"),
        (mm.MappedName("Willing to purchase", mm.NOT_WILLING), "7"),
        (mm.MappedName("Total Samples Given Out", mm.NONE), "80"),
    ]
    assert _sold_units_from_fields(pairs) is None
    assert _consumers_sampled_from_fields(pairs) is None
    assert _samples_given_from_fields(pairs) is None
    assert _engagement_totals_from_field_pairs(pairs)["willing_to_purchase_consumers"] == 0


def test_undecided_fields_still_use_the_vocabulary_beside_confirmed_ones():
    pairs = [
        (mm.MappedName("Bottles moved", mm.UNITS_SOLD_SINGLE), "12"),
        ("Packs Sold", "3"),  # added to the form later, nobody confirmed it
    ]
    assert _sold_units_from_fields(pairs) == 15
    assert [str(name) for name, _ in _sold_unit_lines(pairs)] == [
        "Bottles moved",
        "Packs Sold",
    ]


def test_a_confirmed_count_is_never_read_out_of_prose():
    # The digit-mash that once produced a 2,208-consumer total.
    pairs = [
        (mm.MappedName("People who tried it", mm.CONSUMERS_SAMPLED), "ages 22 to 08, mostly women"),
        (mm.MappedName("Bottles moved", mm.UNITS_SOLD_SINGLE), "12.5"),
    ]
    assert _consumers_sampled_from_fields(pairs) is None
    assert _sold_units_from_fields(pairs) is None


def test_vocabulary_metrics_reports_what_the_matchers_count():
    current = mm.vocabulary_metrics(
        [
            "Total number of consumers sampled",
            "General demographics of consumers sampled (age range)",
            "Single Cans",
            "Packs Sold",
            "# of Total Cans Sold",
            "Willing to purchase",
            "Not willing to purchase",
            "First time trying",
            "Knew about the brand",
            "Total Samples Given Out",
            "People who tried it",
        ]
    )
    assert current == {
        "Total number of consumers sampled": mm.CONSUMERS_SAMPLED,
        "Single Cans": mm.UNITS_SOLD_SINGLE,
        "Packs Sold": mm.UNITS_SOLD_PACK,
        "Willing to purchase": mm.WILLING_TO_BUY,
        "First time trying": mm.FIRST_TIME,
        "Knew about the brand": mm.BRAND_AWARE,
        "Total Samples Given Out": mm.SAMPLES_GIVEN,
    }


# ─── Through the clients schema ─────────────────────────────────────

MAPPING = """
query Mapping($templateId: ID!) {
  recapTemplateMetricMapping(templateId: $templateId) {
    templateId
    fields {
      fieldId
      name
      fieldType
      section
      currentMetric
      confirmedMetric
      packSize
      confirmedBy
      confirmedAt
      canCount
    }
    missingMetrics { key label }
    metricOptions { key label }
  }
}
"""

SUGGEST = """
mutation Suggest($templateId: ID!) {
  suggestRecapMetricMapping(templateId: $templateId) {
    ok
    reason
    suggestions { fieldId metric packSize reason }
  }
}
"""

SAVE = """
mutation Save($templateId: ID!, $fields: [RecapFieldMetricInput!]!) {
  saveRecapMetricMapping(templateId: $templateId, fields: $fields) {
    ok
    message
    mapping {
      templateId
      fields {
        fieldId
        name
        fieldType
        section
        currentMetric
        confirmedMetric
        packSize
        confirmedBy
        confirmedAt
        canCount
      }
      missingMetrics { key label }
      metricOptions { key label }
    }
  }
}
"""

# The front end's EventDashboardQuery, field for field.
EVENT_DASHBOARD = """
query EventDashboardQuery($filters: EventDashboardFiltersInput) {
  eventDashboard(filters: $filters) {
    globalKpis {
      singleCansSold
      multiPacksSold
      packCansEquivalent
      productsSold
      byRmm { rmmId rmmName singleCansSold multiPacksSold }
    }
    metrics {
      totalEvents
      consumersSampled
      brandAwareness
      purchaseIntent
      brandAwarenessBase
      purchaseIntentBase
      comparisonPeriod
      comparisonValues { totalEvents consumersSampled brandAwareness purchaseIntent }
    }
    monthlyTrends {
      dataPoints { month consumersSampled willingToPurchase conversionRate eventsCount }
    }
    performanceInsights {
      knewAboutBrand
      knewAboutBrandPercentage
      willingToPurchase
      willingToPurchasePercentage
      bestMonth { month eventsCount consumersCount }
      growthRate
    }
    goalsProgress { name target current percentageComplete }
    recentEvents { id name date location consumers intentRate status }
  }
  eventDashboardFilterOptions {
    distributors { id name }
    rmms { id name }
    quarters { value label }
    tenants { id name }
  }
}
"""

RECAP_CARDS = """
query CustomRecaps($tenantId: ID, $first: Int) {
  customRecaps(filters: { tenantId: $tenantId }, first: $first) {
    edges { node { uuid soldUnits consumersSampled } }
  }
}
"""

# Field name → (type, the answer on the one filed recap).
FIELDS = {
    "People who tried it": ("number", "40"),
    "Bottles moved": ("number", "12"),
    "Cases moved": ("number", "3"),
    "Said they would buy": ("number", "20"),
    "Already heard of us": ("number", "10"),
    "New to the brand": ("number", "25"),
    "Single Cans": ("number", "9"),
    "What did people say?": ("longtext", "Jordan Rivera said 22 people loved it"),
    "Store manager name": ("text", "Dana Whitfield"),
    "Which flavour went first?": ("select", "Peach"),
}


def _openai_reply(payload: dict) -> dict:
    return {"choices": [{"message": {"content": json.dumps(payload)}}]}


@pytest.mark.django_db(transaction=True)
class TestMetricMapping(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        cache.clear()
        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()

        self.tenant = self.create_tenant(name="Orchard Cider")
        self.other = self.create_tenant(name="Beta Brand")
        self.admin = self.create_user(
            username="map-admin",
            email="map-admin@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="map-client",
            email="map-client@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        self.other_client = self.create_user(
            username="map-other-client",
            email="map-other-client@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.other_client, self.other)

        self.types = {
            kind: recap_models.CustomRecapFieldType.objects.create(
                name=kind, created_by=self.system_user
            )
            for kind in ("number", "text", "longtext", "select")
        }
        self.template, self.fields = self._template(self.tenant, "Cider demo recap")
        self.other_template, self.other_fields = self._template(
            self.other, "Beta demo recap"
        )
        self.recap = self._recap(self.tenant, self.template, self.fields)
        self._recap(self.other, self.other_template, self.other_fields)

    # ── fixtures ──────────────────────────────────────────────────────

    def _template(self, tenant, name):
        event_type = self.create_event_type(name="Sampling", tenant=tenant)
        template = recap_models.CustomRecapTemplate.objects.create(
            name=name,
            event_type=event_type,
            tenant=tenant,
            created_by=self.system_user,
        )
        section = recap_models.RecapSection.objects.create(
            name="Sampling", tenant=tenant, created_by=self.system_user
        )
        fields = {}
        for order, (label, (kind, _answer)) in enumerate(FIELDS.items()):
            fields[label] = recap_models.CustomField.objects.create(
                name=label,
                order=order,
                options=["Peach", "Mango"] if kind == "select" else [],
                custom_recap_template=template,
                custom_field_type=self.types[kind],
                recap_section=section,
                created_by=self.system_user,
            )
        return template, fields

    def _recap(self, tenant, template, fields):
        now = timezone.now()
        event = self.create_event(
            name=f"{tenant.name} store demo",
            tenant=tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )
        recap = recap_models.CustomRecap.objects.create(
            name=f"{tenant.name} recap",
            approved=True,
            event=event,
            tenant=tenant,
            custom_recap_template=template,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        for label, (_kind, answer) in FIELDS.items():
            recap_models.CustomFieldValue.objects.create(
                value=answer,
                custom_recap=recap,
                custom_field=fields[label],
                created_by=self.system_user,
            )
        return recap

    def _choices(self, fields=None):
        fields = fields or self.fields
        return [
            {"fieldId": str(fields["People who tried it"].id), "metric": "consumers_sampled"},
            {"fieldId": str(fields["Bottles moved"].id), "metric": "units_sold_single"},
            {"fieldId": str(fields["Cases moved"].id), "metric": "units_sold_pack", "packSize": 6},
            {"fieldId": str(fields["Said they would buy"].id), "metric": "willing_to_buy"},
            {"fieldId": str(fields["Already heard of us"].id), "metric": "brand_aware"},
            {"fieldId": str(fields["New to the brand"].id), "metric": "first_time"},
            {"fieldId": str(fields["Single Cans"].id), "metric": "none"},
        ]

    # ── calls ─────────────────────────────────────────────────────────

    async def _run(self, query, variables, user):
        result = await self._execute_query_authenticated(
            query, variables, user, self.endpoint_path
        )
        assert result.errors is None, result.errors
        return result.data

    async def _mapping(self, user, template=None):
        data = await self._run(
            MAPPING, {"templateId": str((template or self.template).id)}, user
        )
        return data["recapTemplateMetricMapping"]

    async def _save(self, user, fields, template=None):
        data = await self._run(
            SAVE,
            {"templateId": str((template or self.template).id), "fields": fields},
            user,
        )
        return data["saveRecapMetricMapping"]

    async def _dashboard(self, user):
        cache.clear()
        return await self._run(EVENT_DASHBOARD, {}, user)

    @sync_to_async
    def _rows(self):
        return list(
            recap_models.RecapFieldMetricMapping.objects.values_list(
                "custom_field__name", "metric", "pack_size", "confirmed_at"
            )
        )

    # ── read ──────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_staff_see_what_each_field_counts_as_today_and_what_is_missing(self):
        mapping = await self._mapping(self.admin)
        by_name = {f["name"]: f for f in mapping["fields"]}
        assert list(by_name) == list(FIELDS)
        # Only "Single Cans" is in Spark's vocabulary on this form.
        assert by_name["Single Cans"]["currentMetric"] == "units_sold_single"
        assert by_name["People who tried it"]["currentMetric"] is None
        assert all(f["confirmedMetric"] is None for f in mapping["fields"])
        assert by_name["People who tried it"]["canCount"] is True
        assert by_name["What did people say?"]["canCount"] is False
        assert by_name["Which flavour went first?"]["canCount"] is False
        # A text box whose answers are words is not a count.
        assert by_name["Store manager name"]["canCount"] is False
        assert [m["key"] for m in mapping["missingMetrics"]] == [
            "consumers_sampled",
            "willing_to_buy",
            "first_time",
            "brand_aware",
            "samples_given",
        ]
        assert [o["key"] for o in mapping["metricOptions"]][-1] == "none"

    @pytest.mark.asyncio
    async def test_template_can_be_named_by_its_relay_id(self):
        from strawberry.relay import to_base64

        data = await self._run(
            MAPPING,
            {"templateId": to_base64("CustomRecapTemplate", self.template.id)},
            self.admin,
        )
        assert data["recapTemplateMetricMapping"]["templateId"] == str(self.template.id)

    # ── brand users ───────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_brand_user_cannot_read_a_mapping_not_even_their_own(self):
        assert await self._mapping(self.client_user) is None
        assert await self._mapping(self.client_user, self.other_template) is None

    @pytest.mark.asyncio
    async def test_brand_user_cannot_save_a_mapping(self):
        before = await self._dashboard(self.client_user)
        payload = await self._save(self.client_user, self._choices())
        assert payload == {"ok": False, "message": "Not allowed.", "mapping": None}
        assert await self._rows() == []
        assert await self._dashboard(self.client_user) == before

    @pytest.mark.asyncio
    async def test_brand_user_cannot_ask_for_a_suggestion(self):
        with override_settings(OPENAI_API_KEY="test-key"), patch(
            "utils.ai_text._post_json"
        ) as post:
            data = await self._run(
                SUGGEST, {"templateId": str(self.template.id)}, self.client_user
            )
        assert data["suggestRecapMetricMapping"] == {
            "ok": False,
            "reason": "Not allowed.",
            "suggestions": [],
        }
        post.assert_not_called()

    # ── suggest ───────────────────────────────────────────────────────

    def _model_reply(self):
        ref = {label: f"f{i}" for i, label in enumerate(FIELDS)}
        return _openai_reply(
            {
                "fields": [
                    {"ref": ref["People who tried it"], "metric": "consumers_sampled", "pack_size": None, "reason": "Counts people who tried the product."},
                    {"ref": ref["Bottles moved"], "metric": "units_sold_single", "pack_size": None, "reason": "Single bottles sold."},
                    {"ref": ref["Cases moved"], "metric": "units_sold_pack", "pack_size": 6, "reason": "Cases sold."},
                    {"ref": ref["Said they would buy"], "metric": "willing_to_buy", "pack_size": None, "reason": "Purchase intent."},
                    # Rule: one field per single-valued metric.
                    {"ref": ref["New to the brand"], "metric": "consumers_sampled", "pack_size": None, "reason": "Also people."},
                    # Rule: a count cannot come from a text box of sentences.
                    {"ref": ref["What did people say?"], "metric": "brand_aware", "pack_size": None, "reason": "Mentions people."},
                    {"ref": ref["Store manager name"], "metric": "none", "pack_size": None, "reason": "A name."},
                    {"ref": "f99", "metric": "first_time", "pack_size": None, "reason": "No such field."},
                    {"ref": ref["Single Cans"], "metric": "made_up_metric", "pack_size": None, "reason": "?"},
                ]
            }
        )

    @pytest.mark.asyncio
    async def test_suggestion_sends_labels_types_and_options_only(self):
        with override_settings(OPENAI_API_KEY="test-key"), patch(
            "utils.ai_text._post_json", return_value=self._model_reply()
        ) as post:
            data = await self._run(
                SUGGEST, {"templateId": str(self.template.id)}, self.admin
            )
        assert post.call_count == 1
        sent = json.dumps(post.call_args[0][1])
        for label in FIELDS:
            assert label in sent
        assert "Peach" in sent and "Mango" in sent  # choice options
        # No answer from any recap, and no person.
        assert "Jordan Rivera" not in sent
        assert "Dana Whitfield" not in sent
        assert "loved it" not in sent
        assert "Orchard Cider" not in sent
        assert data["suggestRecapMetricMapping"]["ok"] is True

    @pytest.mark.asyncio
    async def test_suggestion_is_checked_in_code_and_stores_nothing(self):
        with override_settings(OPENAI_API_KEY="test-key"), patch(
            "utils.ai_text._post_json", return_value=self._model_reply()
        ):
            data = await self._run(
                SUGGEST, {"templateId": str(self.template.id)}, self.admin
            )
        payload = data["suggestRecapMetricMapping"]
        ids = {str(f.id): label for label, f in self.fields.items()}
        got = {ids[s["fieldId"]]: s for s in payload["suggestions"]}
        assert got["People who tried it"]["metric"] == "consumers_sampled"
        assert got["Bottles moved"]["metric"] == "units_sold_single"
        assert got["Cases moved"]["metric"] == "units_sold_pack"
        assert got["Cases moved"]["packSize"] == 6
        assert got["Said they would buy"]["metric"] == "willing_to_buy"
        # A second "consumers sampled" is turned down, with the reason.
        assert got["New to the brand"]["metric"] == "none"
        assert "Only one field" in got["New to the brand"]["reason"]
        # A count proposed on a free-text field is turned down.
        assert got["What did people say?"]["metric"] == "none"
        assert "does not hold a number" in got["What did people say?"]["reason"]
        # An unknown metric and an unknown field are dropped or turned down.
        assert got["Single Cans"]["metric"] == "none"
        assert len(payload["suggestions"]) == 8
        # A proposal is not a decision: nothing is stored.
        assert await self._rows() == []

    @pytest.mark.asyncio
    async def test_no_model_leaves_the_screen_usable_by_hand(self):
        with override_settings(OPENAI_API_KEY=""), patch(
            "utils.ai_text._post_json"
        ) as post:
            data = await self._run(
                SUGGEST, {"templateId": str(self.template.id)}, self.admin
            )
        post.assert_not_called()
        payload = data["suggestRecapMetricMapping"]
        assert payload["ok"] is False and payload["suggestions"] == []
        assert "by hand" in payload["reason"]
        # …and a save still works.
        saved = await self._save(self.admin, self._choices())
        assert saved["ok"] is True

    @pytest.mark.asyncio
    async def test_a_failed_model_call_is_a_plain_reason_not_an_error(self):
        with override_settings(OPENAI_API_KEY="test-key"), patch(
            "utils.ai_text._post_json", return_value={"error": {"message": "boom"}}
        ):
            data = await self._run(
                SUGGEST, {"templateId": str(self.template.id)}, self.admin
            )
        assert data["suggestRecapMetricMapping"]["ok"] is False

    # ── save ──────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_save_records_who_confirmed_and_when(self):
        saved = await self._save(self.admin, self._choices())
        assert saved["ok"] is True, saved["message"]
        by_name = {f["name"]: f for f in saved["mapping"]["fields"]}
        assert by_name["People who tried it"]["confirmedMetric"] == "consumers_sampled"
        assert by_name["Cases moved"]["packSize"] == 6
        assert by_name["Single Cans"]["confirmedMetric"] == "none"
        assert by_name["People who tried it"]["confirmedBy"] == "map-admin@test.com"
        assert by_name["People who tried it"]["confirmedAt"]
        assert by_name["What did people say?"]["confirmedMetric"] is None
        # Every standard number but samples now has a field, and samples
        # fall back to consumers sampled.
        assert saved["mapping"]["missingMetrics"] == []

        @sync_to_async
        def _who():
            row = recap_models.RecapFieldMetricMapping.objects.get(
                custom_field=self.fields["People who tried it"]
            )
            return row.confirmed_by_id, row.confirmed_at

        who, when = await _who()
        assert who == self.admin.id and when is not None

    @pytest.mark.asyncio
    async def test_save_refuses_a_count_on_a_field_that_is_not_a_number(self):
        saved = await self._save(
            self.admin,
            [{"fieldId": str(self.fields["What did people say?"].id), "metric": "consumers_sampled"}],
        )
        assert saved["ok"] is False
        assert "does not hold a number" in saved["message"]
        assert await self._rows() == []

    @pytest.mark.asyncio
    async def test_save_refuses_two_fields_for_one_single_valued_metric(self):
        assert (await self._save(self.admin, self._choices()))["ok"] is True
        saved = await self._save(
            self.admin,
            [{"fieldId": str(self.fields["New to the brand"].id), "metric": "consumers_sampled"}],
        )
        assert saved["ok"] is False
        assert "Only one field" in saved["message"]
        rows = {name: metric for name, metric, _size, _at in await self._rows()}
        assert rows["New to the brand"] == "first_time"

    @pytest.mark.asyncio
    async def test_save_refuses_a_field_from_another_template(self):
        saved = await self._save(
            self.admin,
            [{"fieldId": str(self.other_fields["Bottles moved"].id), "metric": "units_sold_single"}],
        )
        assert saved["ok"] is False
        assert await self._rows() == []

    @pytest.mark.asyncio
    async def test_setting_a_field_back_keeps_the_row_and_restores_the_vocabulary(self):
        await self._save(self.admin, self._choices())
        saved = await self._save(
            self.admin,
            [{"fieldId": str(self.fields["Single Cans"].id), "metric": None}],
        )
        assert saved["ok"] is True
        rows = {name: (metric, at) for name, metric, _size, at in await self._rows()}
        assert rows["Single Cans"] == (None, None)  # kept, not deleted
        assert len(rows) == 7
        by_name = {f["name"]: f for f in saved["mapping"]["fields"]}
        assert by_name["Single Cans"]["confirmedMetric"] is None
        assert by_name["Single Cans"]["currentMetric"] == "units_sold_single"

    # ── the numbers ───────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_unmapped_tenant_dashboard_is_unchanged(self):
        """Byte for byte: the tenant's own dashboard query returns the same
        document after ANOTHER tenant confirms a mapping, and after rows
        exist for this tenant that nobody confirmed."""
        before = await self._dashboard(self.client_user)
        kpis = before["eventDashboard"]["globalKpis"]
        metrics = before["eventDashboard"]["metrics"]
        # The vocabulary reading: only "Single Cans" counts on this form.
        assert kpis["productsSold"] == 9 and kpis["singleCansSold"] == 9
        assert metrics["consumersSampled"] == 0

        assert (
            await self._save(
                self.admin, self._choices(self.other_fields), self.other_template
            )
        )["ok"] is True

        @sync_to_async
        def _unconfirmed_rows():
            for label in ("People who tried it", "Bottles moved"):
                recap_models.RecapFieldMetricMapping.objects.create(
                    custom_field=self.fields[label],
                    metric="consumers_sampled",
                    confirmed_at=None,
                )

        await _unconfirmed_rows()
        after = await self._dashboard(self.client_user)
        assert json.dumps(after, sort_keys=True) == json.dumps(before, sort_keys=True)

    @pytest.mark.asyncio
    async def test_confirmed_mapping_changes_the_dashboard(self):
        assert (await self._save(self.admin, self._choices()))["ok"] is True
        after = await self._dashboard(self.client_user)
        kpis = after["eventDashboard"]["globalKpis"]
        metrics = after["eventDashboard"]["metrics"]
        assert metrics["consumersSampled"] == 40
        assert kpis["productsSold"] == 15  # 12 bottles + 3 cases; not the 9
        assert kpis["singleCansSold"] == 12
        assert kpis["multiPacksSold"] == 3
        assert kpis["packCansEquivalent"] == 18  # 3 cases of the confirmed 6
        assert metrics["brandAwareness"] == 25.0  # 10 of 40
        assert metrics["purchaseIntent"] == 50.0  # 20 of 40
        assert metrics["brandAwarenessBase"] == 40
        assert metrics["purchaseIntentBase"] == 40

    @pytest.mark.asyncio
    async def test_confirmed_mapping_changes_the_recap_cards(self):
        variables = {"tenantId": str(self.tenant.id), "first": 10}
        before = await self._run(RECAP_CARDS, variables, self.admin)
        node = before["customRecaps"]["edges"][0]["node"]
        assert node["soldUnits"] == 9 and node["consumersSampled"] is None

        assert (await self._save(self.admin, self._choices()))["ok"] is True
        after = await self._run(RECAP_CARDS, variables, self.admin)
        node = after["customRecaps"]["edges"][0]["node"]
        assert node["soldUnits"] == 15 and node["consumersSampled"] == 40

    @pytest.mark.asyncio
    async def test_confirmed_fields_pass_the_sql_name_gate(self):
        """Tenant totals, the consumer breakdown and the campaign report
        fetch only KPI-named rows. A confirmed field is worded any way at
        all and must still be read."""
        from recaps.consumer_breakdown import tenant_consumer_breakdown
        from recaps.report_service import CampaignReportKpis, _accumulate_custom
        from recaps.tenant_overview import tenant_kpi_totals

        @sync_to_async
        def _read():
            totals = tenant_kpi_totals(self.tenant.id)
            breakdown = tenant_consumer_breakdown(self.tenant.id, None, None)
            kpis = CampaignReportKpis()
            recap = recap_models.CustomRecap.objects.prefetch_related(
                "custom_field_value__custom_field", "custom_recap_product_sample"
            ).get(id=self.recap.id)
            _accumulate_custom(recap, kpis)
            return totals, breakdown, kpis

        totals, breakdown, kpis = await _read()
        assert (totals.consumers_reached, totals.products_sold) == (0, 9)
        assert breakdown["total"] == 0
        assert (kpis.consumers_reached, kpis.products_sold) == (0, 9)

        assert (await self._save(self.admin, self._choices()))["ok"] is True
        totals, breakdown, kpis = await _read()
        assert totals.consumers_reached == 40
        assert totals.products_sold == 15
        assert (totals.cans_sold, totals.packs_sold) == (12, 3)
        assert totals.first_time_consumers == 25
        assert totals.brand_aware_consumers == 10
        assert totals.willing_to_purchase == 20
        assert breakdown["total"] == 40
        assert (kpis.consumers_reached, kpis.products_sold) == (40, 15)
        assert (kpis.cans_sold, kpis.packs_sold) == (12, 3)
        assert kpis.first_time_consumers == 25
        assert kpis.brand_aware_consumers == 10
        assert kpis.willing_to_purchase == 20

    @pytest.mark.asyncio
    async def test_recap_check_before_submit_reads_confirmed_fields(self):
        from recaps.recap_precheck import _Answers

        template_fields = [
            {"id": str(f.id), "name": label, "type": FIELDS[label][0]}
            for label, f in self.fields.items()
        ]
        field_values = [
            {"customFieldId": str(f.id), "value": FIELDS[label][1]}
            for label, f in self.fields.items()
        ]

        @sync_to_async
        def _read():
            pairs = _Answers(template_fields, field_values).pairs
            return (
                _consumers_sampled_from_fields(pairs),
                _sold_units_from_fields(pairs),
            )

        assert await _read() == (None, 9)
        assert (await self._save(self.admin, self._choices()))["ok"] is True
        assert await _read() == (40, 15)
