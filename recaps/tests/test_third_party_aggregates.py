"""3rd-party recaps (exclude_from_aggregates) stay in Recaps, out of totals."""

from datetime import datetime
from io import StringIO
from types import SimpleNamespace

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client as DjangoClient, override_settings
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models
from recaps.aggregates import exclude_non_aggregate_recaps
from recaps.ld_summary_export import compute_ld_summary
from recaps.recap_field_export import build_recap_field_export
from recaps.report_service import build_campaign_report
from recaps.tenant_overview import (
    tenant_conversion_kpis,
    tenant_event_recap_counts,
    tenant_kpi_totals,
    tenant_monthly_trend,
)
from recaps.tenant_sentiment import _custom_feedback_rows


@pytest.mark.django_db(transaction=True)
class TestThirdPartyExcludedFromAggregates(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Torch 3P Co")
        self.tenant.slug = "torch-3p"
        self.tenant.checkin_code = "TP-CLOCK1"
        self.tenant.checkin_recap_code = "TP-AGENCY1"
        self.tenant.save(update_fields=["slug", "checkin_code", "checkin_recap_code"])
        self.et = event_models.EventType.objects.create(
            name="Retail Sampling", slug="torch-3p-retail", tenant=self.tenant, created_by=self.sys
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Torch THC-Retail Sampling", event_type=self.et, tenant=self.tenant, created_by=self.sys
        )
        num = recap_models.CustomRecapFieldType.objects.create(name="Number", created_by=self.sys)
        longtext = recap_models.CustomRecapFieldType.objects.create(name="longtext", created_by=self.sys)
        section = recap_models.RecapSection.objects.create(name="S", tenant=self.tenant, created_by=self.sys)
        self.fields = {
            name: recap_models.CustomField.objects.create(
                name=name, custom_recap_template=self.template, custom_field_type=ft,
                recap_section=section, created_by=self.sys,
            )
            for name, ft in (
                ("Total number of consumers sampled", num),
                ("How many single cans did consumers purchase?", num),
                ("Consumer Feedback", longtext),
            )
        }
        self.today = timezone.localdate()
        self.when = timezone.make_aware(datetime(self.today.year, self.today.month, self.today.day, 12))

    def _event(self, name="Total Wine #1"):
        return event_models.Event.objects.create(
            name=name, tenant=self.tenant, address="1 Main St", date=self.when,
            event_type=self.et, created_by=self.sys, updated_by=self.sys,
        )

    def _recap(self, sampled, cans, *, excluded=False, event=None, feedback="", engagements=None):
        recap = recap_models.CustomRecap.objects.create(
            name="r", approved=True, submitted_at=self.when, event=event or self._event(),
            tenant=self.tenant, custom_recap_template=self.template, created_by=self.sys,
            is_third_party=excluded, exclude_from_aggregates=excluded,
            total_engagements=engagements,
        )
        for name, value in (
            ("Total number of consumers sampled", str(sampled)),
            ("How many single cans did consumers purchase?", str(cans)),
            ("Consumer Feedback", feedback),
        ):
            if value:
                recap_models.CustomFieldValue.objects.create(
                    custom_recap=recap, custom_field=self.fields[name], value=value, created_by=self.sys
                )
        return recap

    def test_helper_drops_flagged_recaps_children_and_agency_only_events(self):
        counted = self._recap(100, 20)
        agency = self._recap(50, 25, excluded=True)
        shared_event = self._event("shared")
        self._recap(10, 1, event=shared_event)
        self._recap(10, 9, excluded=True, event=shared_event)

        recaps = exclude_non_aggregate_recaps(recap_models.CustomRecap.objects.all(), "event__")
        assert agency.id not in set(recaps.values_list("id", flat=True))
        assert counted.id in set(recaps.values_list("id", flat=True))
        values = exclude_non_aggregate_recaps(
            recap_models.CustomFieldValue.objects.all(), "custom_recap__event__"
        )
        assert not values.filter(custom_recap=agency).exists()
        events = set(exclude_non_aggregate_recaps(event_models.Event.objects.all(), "").values_list("id", flat=True))
        assert agency.event_id not in events
        assert shared_event.id in events
        assert counted.event_id in events

    def test_conversion_excludes_flagged(self):
        self._recap(100, 20)
        self._recap(50, 25, excluded=True)
        k = tenant_conversion_kpis(self.tenant.id, start=self.today, end=self.today)
        assert (k["sold"], k["engagements"], k["pct"]) == (20, 100, 20.0)

    def test_kpi_totals_counts_and_trend_exclude_flagged(self):
        self._recap(100, 20, engagements=100)
        self._recap(50, 25, excluded=True, engagements=50)
        totals = tenant_kpi_totals(self.tenant.id)
        assert totals.total_engagements == 100
        assert totals.products_sold == 20
        assert tenant_event_recap_counts(self.tenant.id) == (1, 1)
        trend = tenant_monthly_trend(self.tenant.id)
        assert sum(m.recaps for m in trend) == 1
        assert sum(m.engagements for m in trend) == 100

    def test_campaign_report_keeps_flagged_out_of_kpis(self):
        counted = self._recap(100, 20, engagements=100)
        agency = self._recap(50, 25, excluded=True, engagements=50)
        event = SimpleNamespace(
            id=1, date=self.when, name="e", address="", recaps=[], custom_recap=[counted, agency],
            event_type=None, state=None,
        )
        request = SimpleNamespace(id=1, event_set=[event], tenant=self.tenant, name="Req")
        data = build_campaign_report(request, generated_at="2026-10-02T00:00:00Z")
        assert data.kpis.recaps == 1
        assert data.kpis.total_engagements == 100

    def test_ld_summary_skips_flagged(self):
        self._recap(100, 20)
        self._recap(50, 25, excluded=True)
        summary = compute_ld_summary(self.tenant)
        assert summary.total_demos == 1
        assert summary.consumers == 100

    def test_field_export_lists_flagged_row_but_not_in_totals(self):
        self._recap(100, 20)
        self._recap(50, 25, excluded=True)
        payload = build_recap_field_export(self.tenant, resolve_heic=False)
        assert payload["meta"]["row_count"] == 2
        assert payload["diagnostics"]["rows_not_in_totals"] == 1

    def test_sentiment_feedback_skips_flagged(self):
        self._recap(10, 1, feedback="Loved the peach")
        self._recap(10, 1, excluded=True, feedback="Agency quote")
        assert list(_custom_feedback_rows(self.tenant.id, None)) == ["Loved the peach"]

    def test_backfill_command_dry_run_then_apply(self):
        agency = self._recap(50, 25)
        recap_models.CustomRecap.objects.filter(id=agency.id).update(is_third_party=True)
        normal = self._recap(100, 20)
        out = StringIO()
        call_command("exclude_third_party_from_aggregates", tenant="torch-3p", stdout=out)
        log = out.getvalue()
        assert "DRY RUN" in log and f"#{agency.id}" in log and "-> True" in log
        agency.refresh_from_db()
        self.tenant.refresh_from_db()
        assert agency.exclude_from_aggregates is False
        assert self.tenant.checkin_recap_excludes_aggregates is False

        before = recap_models.CustomRecap.objects.get(id=agency.id).updated_at
        call_command("exclude_third_party_from_aggregates", tenant="torch-3p", apply=True, stdout=StringIO())
        agency.refresh_from_db()
        normal.refresh_from_db()
        self.tenant.refresh_from_db()
        assert agency.exclude_from_aggregates is True
        assert agency.approved is True and agency.updated_at == before
        assert normal.exclude_from_aggregates is False
        assert self.tenant.checkin_recap_excludes_aggregates is True
        assert self.tenant.checkin_recap_code == "TP-AGENCY1"
        assert self.tenant.checkin_code == "TP-CLOCK1"
        k = tenant_conversion_kpis(self.tenant.id, start=self.today, end=self.today)
        assert (k["sold"], k["engagements"]) == (20, 100)

    def test_backfill_requires_agency_link(self):
        self.tenant.checkin_recap_code = None
        self.tenant.save(update_fields=["checkin_recap_code"])
        with pytest.raises(CommandError):
            call_command("exclude_third_party_from_aggregates", tenant="torch-3p", stdout=StringIO())

    @override_settings(INTERNAL_CRON_SECRET="s3cret")
    def test_cron_view_is_secret_gated_and_dry_run(self):
        http = DjangoClient()
        url = "/internal/cron/exclude-third-party-from-aggregates"
        assert http.post(url, {"tenant": "torch-3p"}).status_code in (401, 403)
        res = http.post(url, {"tenant": "torch-3p"}, HTTP_X_CRON_SECRET="s3cret")
        assert res.status_code == 200, res.content
        assert "DRY RUN" in res.json()["log"]
