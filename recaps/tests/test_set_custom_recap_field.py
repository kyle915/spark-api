"""
Coverage for the `set_custom_recap_field` management command — the targeted,
reversible single-field correction used to fix data-entry errors on prod
(e.g. a "Consumers Sampled" typed as 1960 instead of 30).

Asserts the safety contract:
  * dry-run (no --apply) changes nothing;
  * --apply writes the new value;
  * --expect-current refuses to write when the current value differs;
  * an ambiguous --field-contains (matches >1 field) refuses to write;
  * no match raises;
  * setting the value it already holds is a no-op.
"""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models


@pytest.mark.django_db(transaction=True)
class TestSetCustomRecapField(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Stone House Bread", slug="shb")
        self.event_type = self.create_event_type(
            name="Retail Sampling", tenant=self.tenant
        )
        recap_models.CustomRecapFieldType.objects.get_or_create(
            name="number", defaults={"created_by": self.system_user}
        )
        self.number_ft = recap_models.CustomRecapFieldType.objects.get(name="number")

        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="SHB Recap",
            event_type=self.event_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )
        self.section = recap_models.RecapSection.objects.create(
            tenant=self.tenant, name="Numbers", created_by=self.system_user
        )
        self.sampled_field = recap_models.CustomField.objects.create(
            custom_recap_template=self.template,
            recap_section=self.section,
            name="Consumers Sampled",
            custom_field_type=self.number_ft,
            created_by=self.system_user,
        )
        self.event = event_models.Event.objects.create(
            name="Kroger #671", tenant=self.tenant, address="1 Main St",
            created_by=self.system_user,
        )
        self.recap = recap_models.CustomRecap.objects.create(
            name="Kroger recap #671", event=self.event, tenant=self.tenant,
            custom_recap_template=self.template, created_by=self.system_user,
        )
        self.value = recap_models.CustomFieldValue.objects.create(
            custom_recap=self.recap, custom_field=self.sampled_field,
            value="1960", created_by=self.system_user,
        )

    def _run(self, **kwargs):
        out = StringIO()
        opts = {"stdout": out, "stderr": out}
        opts.update(kwargs)
        call_command("set_custom_recap_field", **opts)
        return out.getvalue()

    def test_dry_run_changes_nothing(self):
        out = self._run(
            recap=str(self.recap.id), field_contains="consumers sampled", value="30"
        )
        assert "DRY-RUN" in out
        self.value.refresh_from_db()
        assert self.value.value == "1960"

    def test_apply_writes_new_value(self):
        out = self._run(
            recap=str(self.recap.id), field_contains="consumers sampled",
            value="30", apply=True,
        )
        assert "APPLIED" in out
        self.value.refresh_from_db()
        assert self.value.value == "30"

    def test_resolve_by_uuid(self):
        self._run(
            recap=str(self.recap.uuid), field_contains="consumers sampled",
            value="30", apply=True,
        )
        self.value.refresh_from_db()
        assert self.value.value == "30"

    def test_expect_current_guard_blocks_on_mismatch(self):
        with pytest.raises(CommandError, match="expect-current"):
            self._run(
                recap=str(self.recap.id), field_contains="consumers sampled",
                value="30", expect_current="999", apply=True,
            )
        self.value.refresh_from_db()
        assert self.value.value == "1960"  # untouched

    def test_expect_current_allows_when_match(self):
        self._run(
            recap=str(self.recap.id), field_contains="consumers sampled",
            value="30", expect_current="1960", apply=True,
        )
        self.value.refresh_from_db()
        assert self.value.value == "30"

    def test_ambiguous_field_refuses(self):
        # A second field that also matches the substring "sampled".
        other = recap_models.CustomField.objects.create(
            custom_recap_template=self.template,
            recap_section=self.section,
            name="Products Sampled",
            custom_field_type=self.number_ft,
            created_by=self.system_user,
        )
        recap_models.CustomFieldValue.objects.create(
            custom_recap=self.recap, custom_field=other,
            value="5", created_by=self.system_user,
        )
        with pytest.raises(CommandError, match="refusing to guess"):
            self._run(
                recap=str(self.recap.id), field_contains="sampled",
                value="30", apply=True,
            )
        self.value.refresh_from_db()
        assert self.value.value == "1960"  # untouched

    def test_no_match_raises(self):
        with pytest.raises(CommandError, match="No field"):
            self._run(
                recap=str(self.recap.id), field_contains="nonexistent",
                value="30", apply=True,
            )

    def test_idempotent_noop_when_already_set(self):
        out = self._run(
            recap=str(self.recap.id), field_contains="consumers sampled",
            value="1960", apply=True,
        )
        assert "no-op" in out.lower()
        self.value.refresh_from_db()
        assert self.value.value == "1960"

    def test_unknown_recap_raises(self):
        with pytest.raises(CommandError, match="No custom recap"):
            self._run(
                recap="99999999", field_contains="consumers sampled", value="30",
            )
