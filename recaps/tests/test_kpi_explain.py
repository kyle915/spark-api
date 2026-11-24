"""KPI tiles that explain themselves.

What these prove, through the real clients schema with the field sets the
web client sends:

  * the explanation's total IS the tile's total, for every tile metric, for
    an activity tenant and for a sales program;
  * it names the fields that feed a number, says whether each was confirmed
    by staff or picked up from its wording, and counts the approved recaps
    behind it;
  * recaps waiting for approval and recaps kept out of totals are listed with
    what they would add — and adding them really adds that much;
  * a number no form has a field for reads "not tracked";
  * a brand user reads their own tenant's explanation without the staff
    "fix mapping" link; staff get the link.

The American Ice case: "How many shoppers took the 4-for-$6 upgrade?" was
not counted as sold until staff confirmed it.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from asgiref.sync import sync_to_async
from django.core.cache import cache
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import kpi_explain
from recaps import metric_mapping as mm
from recaps import models as recap_models
from recaps.tenant_overview import tenant_kpi_totals

# The web client's TenantKpisQuery, field for field.
TENANT_KPIS = """
query TenantKpisQuery($tenantId: ID!, $year: Int) {
  tenantKpis(tenantId: $tenantId, year: $year) {
    events recaps consumersReached samplesDistributed productsSold cansSold
    packsSold totalEngagements firstTimeConsumers brandAwareConsumers
    willingToPurchase
    monthlyTrend { month recaps engagements samples consumersSampled unitsSold }
    trendSeries { key label }
    trendNote
    program {
      recaps demos consumersSampled unitsSold cansSold packsSold conversionPct
      dryDemos dryPeopleEngaged firstTimeConsumers brandAwareConsumers
      willingToPurchase
    }
  }
}
"""

# The web client's TenantKpiExplanationQuery, field for field.
EXPLAIN = """
query TenantKpiExplanationQuery($tenantId: ID!, $metric: String!, $year: Int) {
  tenantKpiExplanation(tenantId: $tenantId, metric: $metric, year: $year) {
    metric label total program summary recapsCounted recapsInScope
    recapsWithoutValue
    sources { templateId templateUuid templateName recaps value fields { label how value } }
    excluded { key label recaps wouldAdd }
    notTracked
    notTrackedTemplates { templateId templateUuid templateName }
    note
    canFixMapping
  }
}
"""

STATUS = """
query TenantKpiStatusQuery($tenantId: ID!, $year: Int) {
  tenantKpiStatus(tenantId: $tenantId, year: $year) {
    untrackedMetrics
    pending { metric recaps wouldAdd }
  }
}
"""

TILE_FIELDS = {
    "products_sold": "productsSold",
    "consumers_reached": "consumersReached",
    "samples_distributed": "samplesDistributed",
    "total_engagements": "totalEngagements",
    "first_time_consumers": "firstTimeConsumers",
    "brand_aware_consumers": "brandAwareConsumers",
    "willing_to_purchase": "willingToPurchase",
}
PROGRAM_FIELDS = {
    "products_sold": "unitsSold",
    "consumers_reached": "consumersSampled",
    "first_time_consumers": "firstTimeConsumers",
    "brand_aware_consumers": "brandAwareConsumers",
    "willing_to_purchase": "willingToPurchase",
}

TINS = "How many $1 tins were sold?"
UPGRADE = "How many shoppers took the 4-for-$6 upgrade?"
SAMPLED = "How many consumers did you sample?"
KNEW = "How many knew about the brand?"
NOTES = "Notes"


@pytest.mark.django_db(transaction=True)
class TestKpiExplain(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        cache.clear()
        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Polar Ice")
        self.admin = self.create_user(
            username="kpi-admin", email="kpi-admin@test.com", role=self.roles["spark_admin"]
        )
        self.client_user = self.create_user(
            username="kpi-client", email="kpi-client@test.com", role=self.roles["client"]
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        self.types = {
            kind: recap_models.CustomRecapFieldType.objects.create(
                name=kind, created_by=self.system_user
            )
            for kind in ("number", "longtext")
        }
        self.template, self.fields = self._template(
            "Ice demo recap",
            {TINS: "number", UPGRADE: "number", SAMPLED: "number", KNEW: "number", NOTES: "longtext"},
        )
        # Two approved, three waiting for approval, one kept out of totals.
        self._recap({TINS: "10", UPGRADE: "4", SAMPLED: "50", KNEW: "12", NOTES: "Busy"}, approved=True, engagements=60)
        self._recap({TINS: "6", UPGRADE: "2", SAMPLED: "30", NOTES: "Slow start"}, approved=True, engagements=35)
        self._recap({TINS: "3", UPGRADE: "1", SAMPLED: "20", KNEW: "5"}, approved=False)
        self._recap({TINS: "2", SAMPLED: "15"}, approved=False)
        self._recap({TINS: "5", UPGRADE: "5", SAMPLED: "25"}, approved=False)
        self._recap({TINS: "100", SAMPLED: "400"}, approved=True, aggregate=False)
        # An approved recap with no sold answer at all.
        self._recap({NOTES: "Store closed early"}, approved=True)

    # ── fixtures ──────────────────────────────────────────────────────

    def _template(self, name, spec):
        event_type = self.create_event_type(name="Sampling", tenant=self.tenant)
        template = recap_models.CustomRecapTemplate.objects.create(
            name=name, event_type=event_type, tenant=self.tenant, created_by=self.system_user
        )
        section = recap_models.RecapSection.objects.create(
            name="Results", tenant=self.tenant, created_by=self.system_user
        )
        fields = {
            label: recap_models.CustomField.objects.create(
                name=label,
                order=i,
                custom_recap_template=template,
                custom_field_type=self.types[kind],
                recap_section=section,
                created_by=self.system_user,
            )
            for i, (label, kind) in enumerate(spec.items())
        }
        return template, fields

    def _recap(self, answers, *, approved, aggregate=True, engagements=None, template=None, fields=None):
        template = template or self.template
        fields = fields or self.fields
        now = timezone.now()
        event = self.create_event(
            name="Store demo",
            tenant=self.tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )
        recap = recap_models.CustomRecap.objects.create(
            name="Store demo recap",
            approved=approved,
            exclude_from_aggregates=not aggregate,
            total_engagements=engagements,
            event=event,
            tenant=self.tenant,
            custom_recap_template=template,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        for label, value in answers.items():
            recap_models.CustomFieldValue.objects.create(
                value=value,
                custom_recap=recap,
                custom_field=fields[label],
                created_by=self.system_user,
            )
        return recap

    # ── calls ─────────────────────────────────────────────────────────

    async def _run(self, query, variables, user):
        result = await self._execute_query_authenticated(
            query, variables, user, self.endpoint_path
        )
        assert result.errors is None, result.errors
        return result.data

    async def _tiles(self, user=None, year=None):
        data = await self._run(
            TENANT_KPIS, {"tenantId": str(self.tenant.id), "year": year}, user or self.admin
        )
        return data["tenantKpis"]

    async def _explain(self, metric, user=None, year=None):
        data = await self._run(
            EXPLAIN,
            {"tenantId": str(self.tenant.id), "metric": metric, "year": year},
            user or self.admin,
        )
        return data["tenantKpiExplanation"]

    async def _status(self, user=None):
        data = await self._run(STATUS, {"tenantId": str(self.tenant.id)}, user or self.admin)
        return data["tenantKpiStatus"]

    @sync_to_async
    def _confirm(self, label, metric):
        mm.save_mapping(
            self.template.id,
            [{"field_id": self.fields[label].id, "metric": metric}],
            self.admin,
        )

    # ── the total is the tile ─────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_explanation_total_equals_every_tile(self):
        for year in (None, timezone.now().year):
            tiles = await self._tiles(year=year)
            for metric, tile_field in TILE_FIELDS.items():
                explained = await self._explain(metric, year=year)
                assert explained["total"] == tiles[tile_field], (metric, year)
                # The per-form split adds back up to the total.
                assert sum(s["value"] or 0 for s in explained["sources"]) == explained["total"], metric

    @pytest.mark.asyncio
    async def test_explanation_total_equals_the_tile_after_a_confirmation(self):
        await self._confirm(UPGRADE, mm.UNITS_SOLD_SINGLE)
        tiles = await self._tiles()
        explained = await self._explain("products_sold")
        assert explained["total"] == tiles["productsSold"] == 10 + 4 + 6 + 2

    @pytest.mark.asyncio
    async def test_sales_program_explanation_matches_the_program_tile(self):
        await sync_to_async(
            lambda: type(self.tenant).objects.filter(id=self.tenant.id).update(
                insights_trend_series="sales"
            )
        )()
        tiles = await self._tiles()
        assert tiles["program"] is not None
        for metric, field_name in PROGRAM_FIELDS.items():
            explained = await self._explain(metric)
            assert explained["program"] is True
            assert explained["total"] == tiles["program"][field_name], metric
            for bucket in explained["excluded"]:
                assert bucket["wouldAdd"] is None

    # ── what it says ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_names_fields_and_recaps_behind_units_sold(self):
        explained = await self._explain("products_sold")
        # Only the tins line matches by wording; the upgrade line does not.
        assert explained["total"] == 16
        assert explained["recapsCounted"] == 2
        assert explained["recapsInScope"] == 3
        assert explained["recapsWithoutValue"] == 1
        [source] = explained["sources"]
        assert source["templateName"] == "Ice demo recap"
        assert source["fields"] == [{"label": TINS, "how": "wording", "value": 16}]
        assert f"“{TINS}”" in explained["summary"]
        assert "from 2 approved recaps" in explained["summary"]
        assert "regex" not in explained["summary"].lower()

    @pytest.mark.asyncio
    async def test_confirmed_upgrade_field_is_named_as_confirmed(self):
        await self._confirm(UPGRADE, mm.UNITS_SOLD_SINGLE)
        explained = await self._explain("products_sold")
        fields = {f["label"]: f for f in explained["sources"][0]["fields"]}
        assert fields[UPGRADE] == {"label": UPGRADE, "how": "confirmed", "value": 6}
        assert fields[TINS] == {"label": TINS, "how": "wording", "value": 16}
        assert f"“{TINS}” + “{UPGRADE}”" in explained["summary"] or (
            f"“{UPGRADE}” + “{TINS}”" in explained["summary"]
        )

    @pytest.mark.asyncio
    async def test_left_out_recaps_say_what_they_would_add(self):
        explained = await self._explain("products_sold")
        buckets = {b["key"]: b for b in explained["excluded"]}
        assert buckets["pending_approval"] == {
            "key": "pending_approval",
            "label": "Waiting for approval",
            "recaps": 3,
            "wouldAdd": 3 + 2 + 5,
        }
        assert buckets["excluded_from_totals"]["recaps"] == 1
        assert buckets["excluded_from_totals"]["wouldAdd"] == 100
        assert "3 recaps waiting for approval would add 10." in explained["summary"]

        # Approving them really adds that much.
        before = (await self._tiles())["productsSold"]
        await sync_to_async(
            lambda: recap_models.CustomRecap.objects.filter(
                tenant=self.tenant, approved=False
            ).update(approved=True)
        )()
        after = (await self._tiles())["productsSold"]
        assert after - before == buckets["pending_approval"]["wouldAdd"]

    @pytest.mark.asyncio
    async def test_audience_and_samples_explanations(self):
        knew = await self._explain("brand_aware_consumers")
        assert knew["total"] == 12
        assert knew["sources"][0]["fields"] == [{"label": KNEW, "how": "wording", "value": 12}]
        assert {b["key"]: b["wouldAdd"] for b in knew["excluded"]}["pending_approval"] == 5

        sampled = await self._explain("consumers_reached")
        assert sampled["total"] == 80
        assert sampled["sources"][0]["fields"] == [{"label": SAMPLED, "how": "wording", "value": 80}]

        samples = await self._explain("samples_distributed")
        assert samples["total"] == 80
        assert samples["note"]

        engaged = await self._explain("total_engagements")
        assert engaged["total"] == 95
        assert engaged["recapsCounted"] == 2

    # ── not tracked ───────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_a_number_with_no_field_reads_not_tracked(self):
        status = await self._status()
        # The form asks no first-time or willing-to-buy question.
        assert "first_time_consumers" in status["untrackedMetrics"]
        assert "willing_to_purchase" in status["untrackedMetrics"]
        assert "products_sold" not in status["untrackedMetrics"]
        explained = await self._explain("willing_to_purchase")
        assert explained["notTracked"] is True
        assert explained["notTrackedTemplates"] == [
            {
                "templateId": str(self.template.id),
                "templateUuid": str(self.template.uuid),
                "templateName": "Ice demo recap",
            }
        ]

        await self._confirm(UPGRADE, mm.WILLING_TO_BUY)
        status = await self._status()
        assert "willing_to_purchase" not in status["untrackedMetrics"]

    @pytest.mark.asyncio
    async def test_status_lists_pending_per_tile(self):
        status = await self._status()
        pending = {p["metric"]: p for p in status["pending"]}
        assert pending["products_sold"] == {"metric": "products_sold", "recaps": 3, "wouldAdd": 10}
        assert pending["brand_aware_consumers"]["recaps"] == 1

    # ── who sees what ─────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_brand_user_reads_without_the_fix_link_staff_with_it(self):
        as_client = await self._explain("products_sold", user=self.client_user)
        assert as_client["canFixMapping"] is False
        assert as_client["total"] == 16
        as_staff = await self._explain("products_sold")
        assert as_staff["canFixMapping"] is True

    @pytest.mark.asyncio
    async def test_unknown_metric_reads_null(self):
        assert await self._explain("not_a_metric") is None

    # ── the tile itself is unchanged ─────────────────────────────────

    @pytest.mark.asyncio
    async def test_tiles_count_only_approved_aggregate_recaps(self):
        tiles = await self._tiles()
        assert tiles["recaps"] == 3
        assert tiles["productsSold"] == 16
        assert tiles["consumersReached"] == 80

    def test_counted_scope_matches_tenant_kpi_totals(self):
        totals = tenant_kpi_totals(self.tenant.id)
        for metric in kpi_explain.METRICS:
            explained = kpi_explain.explain_tenant_kpi(self.tenant.id, metric)
            assert explained["total"] == getattr(totals, metric), metric

    # ── the template editor's check before saving ────────────────────

    PREVIEW = """
    query Preview($templateId: ID, $productSamples: Boolean!, $fields: [RecapFieldPreviewInput!]!) {
      recapTemplateMetricPreview(
        templateId: $templateId, productSamples: $productSamples, fields: $fields
      ) {
        missingMetrics { key label }
        uncountedFields
      }
    }
    """

    async def _preview(self, fields, user=None, template_id="self"):
        tid = str(self.template.id) if template_id == "self" else template_id
        data = await self._run(
            self.PREVIEW,
            {"templateId": tid, "productSamples": False, "fields": fields},
            user or self.admin,
        )
        return data["recapTemplateMetricPreview"]

    def _draft(self, *labels, new=()):
        out = [
            {"fieldId": str(self.fields[label].id), "name": label, "fieldType": "number"}
            for label in labels
        ]
        out += [{"fieldId": None, "name": name, "fieldType": "number"} for name in new]
        return out

    @pytest.mark.asyncio
    async def test_preview_flags_the_uncounted_upgrade_line(self):
        preview = await self._preview(self._draft(TINS, UPGRADE, SAMPLED, KNEW))
        assert preview["uncountedFields"] == [UPGRADE]
        missing = {m["key"] for m in preview["missingMetrics"]}
        assert {"first_time", "willing_to_buy"} <= missing
        assert "units_sold" not in missing

    @pytest.mark.asyncio
    async def test_preview_respects_confirmations_and_new_fields(self):
        await self._confirm(UPGRADE, mm.NONE)
        preview = await self._preview(
            self._draft(TINS, UPGRADE, SAMPLED, KNEW, new=["First time trying it?"])
        )
        assert preview["uncountedFields"] == []
        assert "first_time" not in {m["key"] for m in preview["missingMetrics"]}

    @pytest.mark.asyncio
    async def test_preview_says_when_a_rename_drops_a_number(self):
        draft = self._draft(UPGRADE, SAMPLED, KNEW)
        draft.append(
            {"fieldId": str(self.fields[TINS].id), "name": "Tins moved today", "fieldType": "number"}
        )
        preview = await self._preview(draft)
        assert "units_sold" in {m["key"] for m in preview["missingMetrics"]}

    @pytest.mark.asyncio
    async def test_preview_is_staff_only(self):
        data = await self._run(
            self.PREVIEW,
            {"templateId": str(self.template.id), "productSamples": False, "fields": []},
            self.client_user,
        )
        assert data["recapTemplateMetricPreview"] is None
