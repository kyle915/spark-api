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
        assert "sold 0 ÷ sampled 80 = 0.0%" in log
        assert "insights_excludes_from_conv" in log
        assert "notes_sold_mismatch(notes=[14],field=0)" in log
        assert f"#{recap.id}" in log
        assert "===CSV-BEGIN===" in log

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
