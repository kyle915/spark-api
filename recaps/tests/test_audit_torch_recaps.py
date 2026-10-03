"""audit_torch_recaps: read-only Torch audit report."""

from datetime import datetime, timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models


@pytest.mark.django_db(transaction=True)
class TestAuditTorchRecaps(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Torch Audit Co")
        self.tenant.slug = "audit-torch"
        self.tenant.save(update_fields=["slug"])
        self.retail_et = event_models.EventType.objects.create(
            name="Retail Sampling", slug="audit-torch-retail", tenant=self.tenant, created_by=self.sys
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Torch THC-Retail Sampling",
            event_type=self.retail_et,
            tenant=self.tenant,
            created_by=self.sys,
        )
        self.num = recap_models.CustomRecapFieldType.objects.create(name="Number", created_by=self.sys)
        self.long = recap_models.CustomRecapFieldType.objects.create(name="longtext", created_by=self.sys)
        self.section = recap_models.RecapSection.objects.create(
            name="Sampling", tenant=self.tenant, created_by=self.sys
        )
        self.fields = {}
        for name, ftype in (
            ("Total number of consumers sampled", self.num),
            ("How many single cans did consumers purchase?", self.num),
            ("General notes", self.long),
        ):
            self.fields[name] = recap_models.CustomField.objects.create(
                name=name,
                custom_recap_template=self.template,
                custom_field_type=ftype,
                recap_section=self.section,
                created_by=self.sys,
            )
        self.today = timezone.localdate()

    def _walkup_recap(self, values: dict[str, str], *, approved=True, name="Total Wine #1234"):
        when = timezone.make_aware(datetime(self.today.year, self.today.month, self.today.day, 12))
        event = event_models.Event.objects.create(
            name="walk-in",
            tenant=self.tenant,
            address="1 Main St, Tampa, FL",
            date=when,
            event_type=self.retail_et,
            created_by=self.sys,
            updated_by=self.sys,
        )
        recap = recap_models.CustomRecap.objects.create(
            name=name,
            approved=approved,
            submitted_at=when,
            event=event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.sys,
        )
        for field_name, value in values.items():
            recap_models.CustomFieldValue.objects.create(
                custom_recap=recap,
                custom_field=self.fields[field_name],
                value=value,
                created_by=self.sys,
            )
        return recap

    def _run(self, **kwargs) -> str:
        out = StringIO()
        call_command("audit_torch_recaps", tenant="audit-torch", stdout=out, **kwargs)
        return out.getvalue()

    def test_walkup_recap_pooled_and_flagged(self):
        recap = self._walkup_recap(
            {
                "Total number of consumers sampled": "80",
                "How many single cans did consumers purchase?": "0",
                "General notes": "Great day, sold 14 units to returning shoppers.",
            }
        )
        log = self._run()
        assert "sold 0 ÷ base 80 = 0.0%" in log
        assert "insights_excludes_from_conv" not in log
        assert "notes_sold_mismatch(notes=[14],field=0)" in log
        assert f"#{recap.id}" in log
        assert "===CSV-BEGIN===" in log
        assert "updated_by" in log

    def test_dry_demo_pooled_vs_people_engaged_and_unpaired_listed(self):
        select = recap_models.CustomRecapFieldType.objects.create(name="select", created_by=self.sys)
        for name, ftype in (("Dry demo? (no product tasted)", select), ("People engaged", self.num)):
            self.fields[name] = recap_models.CustomField.objects.create(
                name=name,
                custom_recap_template=self.template,
                custom_field_type=ftype,
                recap_section=self.section,
                created_by=self.sys,
            )
        self._walkup_recap(
            {
                "Total number of consumers sampled": "0",
                "How many single cans did consumers purchase?": "6",
                "Dry demo? (no product tasted)": "Yes",
                "People engaged": "30",
            }
        )
        unpaired = self._walkup_recap(
            {
                "Total number of consumers sampled": "0",
                "How many single cans did consumers purchase?": "4",
                "Dry demo? (no product tasted)": "Yes",
            }
        )
        self._walkup_recap(
            {
                "Total number of consumers sampled": "20",
                "How many single cans did consumers purchase?": "2",
                "General notes": "For this gig I didn't have samples, they bought on my word.",
            }
        )
        self._walkup_recap(
            {
                "Total number of consumers sampled": "50",
                "How many single cans did consumers purchase?": "5",
            }
        )
        log = self._run(no_csv=True)
        assert "sold 13 ÷ base 100 = 13.0% (n=3)" in log
        assert "live-sampled: 7/70 = 10.0% (n=2)" in log
        assert "dry demos vs people engaged: 6/30 = 20.0% (n=1)" in log
        assert "notes say dry but untagged: n=1" in log
        assert f"4 units on #{unpaired.id}" in log
        assert "dry_demo_tagged" in log
        assert "dry_demo_notes_untagged" in log

    def test_needs_review_reported_separately(self):
        self._walkup_recap(
            {
                "Total number of consumers sampled": "100",
                "How many single cans did consumers purchase?": "30",
            },
            approved=False,
        )
        log = self._run(no_csv=True)
        assert "[needs_review]" in log
        assert "needs_review" in log
        assert "sold 30 ÷ sampled 100 = 30.0%" in log  # incl. needs-review block only
        assert "===CSV-BEGIN===" not in log

    def test_under_threshold_and_coverage_against_insights_set(self):
        low = self._walkup_recap(
            {
                "Total number of consumers sampled": "100",
                "How many single cans did consumers purchase?": "5",
                "General notes": "Store was really slow today, low foot traffic.",
            }
        )
        ok = self._walkup_recap(
            {"Total number of consumers sampled": "40", "How many single cans did consumers purchase?": "12"}
        )
        zero = self._walkup_recap({"Total number of consumers sampled": "30"})
        unrated = self._walkup_recap({"How many single cans did consumers purchase?": "3"})
        pending = self._walkup_recap(
            {"Total number of consumers sampled": "50", "How many single cans did consumers purchase?": "2"},
            approved=False,
        )
        event_et = event_models.EventType.objects.create(
            name="Event Activation", slug="audit-torch-ea", tenant=self.tenant, created_by=self.sys
        )
        mistyped = self._walkup_recap(
            {"Total number of consumers sampled": "20", "How many single cans did consumers purchase?": "1"}
        )
        mistyped.event.event_type = event_et
        mistyped.event.save(update_fields=["event_type"])
        archived = self._walkup_recap({"Total number of consumers sampled": "10"})
        archived.archived_at = timezone.now()
        archived.save(update_fields=["archived_at"])

        log = self._run()
        cov = log.split("## Coverage")[1].split("## Recaps under")[0]
        assert "retail/on-prem recaps, all statuses: 7" in cov
        assert "counted in Insights rate: 3" in cov
        assert f"needs review (not approved): 1  ids=#{pending.id}" in cov
        assert f"no_base: 1  ids=#{unrated.id}" in cov
        assert f"archived: 1  ids=#{archived.id}" in cov
        assert "** SHOULD COUNT ** activation typed 'Event Activation'" in cov
        assert f"should-count-but-doesn't: 1  ids=#{mistyped.id}" in cov
        under = log.split("## Recaps under 20%")[1].split("## Unrated")[0]
        assert f"#{low.id} " in under and "tags=[low traffic]" in under
        assert f"#{zero.id} " in under and "conv=0.0%" in under and "(blank)" in under
        assert f"#{ok.id} " not in under
        assert f"#{pending.id} " in under.split("[needs_review]")[1]
        assert f"#{mistyped.id} " in under  # rated 5% even though Insights drops it
        assert f"#{unrated.id} " in log.split("## Unrated")[1].split("## Recap detail")[0]
        csv_block = log.split("===U20-CSV-BEGIN===")[1].split("===U20-CSV-END===")[0]
        assert csv_block.strip().splitlines()[0].startswith("section,id,date")
        assert f"under_approved,{low.id}," in csv_block
        assert "===COVERAGE-CSV-BEGIN===" in log

    def test_third_party_out_of_conversion_and_listed_separately(self):
        self._walkup_recap(
            {"Total number of consumers sampled": "100", "How many single cans did consumers purchase?": "25"}
        )
        agency = self._walkup_recap(
            {"Total number of consumers sampled": "50", "How many single cans did consumers purchase?": "2"}
        )
        recap_models.CustomRecap.objects.filter(id=agency.id).update(
            is_third_party=True, exclude_from_aggregates=True
        )
        log = self._run()
        assert "sold 25 ÷ base 100 = 25.0% (n=1)" in log
        assert "BEFORE (incl. 3rd-party): 27/150 = 18.0% (n=2)" in log
        assert "consumers sampled (retail/on-prem approved): 100 excl. vs 150 incl. 3rd-party" in log
        cov = log.split("## Coverage")[1].split("## Recaps under")[0]
        assert "retail/on-prem recaps, all statuses: 1" in cov
        assert "3rd-party recaps (intentionally excluded): 1" in cov and "still in Insights set: 0" in cov
        under = log.split("## Recaps under 20%")[1].split("## 3rd-party recaps under")[0]
        assert f"#{agency.id} " not in under
        tp = log.split("## 3rd-party recaps under 20%")[1].split("## Unrated")[0]
        assert f"#{agency.id} " in tp
        assert f"third_party_under_approved,{agency.id}," in log

    def test_since_until_window(self):
        self._walkup_recap({"Total number of consumers sampled": "50"})
        future = (self.today + timedelta(days=5)).isoformat()
        log = self._run(since=future, until=future, no_csv=True)
        assert "TOTAL" in log
        assert "Recap detail" in log
        assert "#" not in log.split("## Recap detail")[1]

    def test_unknown_tenant(self):
        with pytest.raises(CommandError):
            call_command("audit_torch_recaps", tenant="nope-nope", stdout=StringIO())

    def test_never_writes(self):
        recap = self._walkup_recap({"Total number of consumers sampled": "10"})
        before = recap_models.CustomRecap.objects.get(id=recap.id).updated_at
        self._run(no_csv=True)
        assert recap_models.CustomRecap.objects.get(id=recap.id).updated_at == before
