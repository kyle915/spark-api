"""Coverage for the select / multiselect choice field's `options`.

The dropdown / multi-select field types store their admin-defined allowed
choices in CustomField.options (a JSON list of strings). This pins:
  * the column round-trips a list of strings (migration 0026 + the model),
  * the GraphQL CustomField.options resolver returns that list (and [] when
    a field has no options, e.g. a plain text field).

The submitted answer itself rides in CustomFieldValue.value (a single option
string for 'select', a JSON array for 'multiselect') — that path needs no new
backend logic (it's just text), so it isn't re-tested here.
"""
from __future__ import annotations

import pytest

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models
from recaps.types import CustomField as CustomFieldType


@pytest.mark.django_db(transaction=True)
class TestCustomFieldOptions(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="SHB Options")
        self.event_type = self.create_event_type(name="Sampling", tenant=self.tenant)
        self.multiselect_type = recap_models.CustomRecapFieldType.objects.create(
            name="multiselect", created_by=self.system_user
        )
        self.text_type = recap_models.CustomRecapFieldType.objects.create(
            name="text", created_by=self.system_user
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="T", event_type=self.event_type, tenant=self.tenant,
            created_by=self.system_user,
        )
        self.section = recap_models.RecapSection.objects.create(
            name="Numbers", tenant=self.tenant, created_by=self.system_user
        )

    def _field(self, field_type, options):
        return recap_models.CustomField.objects.create(
            name="What market is this?",
            custom_recap_template=self.template,
            custom_field_type=field_type,
            recap_section=self.section,
            created_by=self.system_user,
            options=options,
        )

    def test_options_round_trip_on_model(self):
        f = self._field(self.multiselect_type, ["Detroit", "Grand Rapids", "Lansing"])
        f.refresh_from_db()
        assert f.options == ["Detroit", "Grand Rapids", "Lansing"]

    def test_options_default_empty_list(self):
        # A field created without options (any non-choice field) defaults to [].
        f = recap_models.CustomField.objects.create(
            name="Notes",
            custom_recap_template=self.template,
            custom_field_type=self.text_type,
            recap_section=self.section,
            created_by=self.system_user,
        )
        f.refresh_from_db()
        assert f.options == []

    def test_graphql_resolver_returns_options(self):
        f = self._field(self.multiselect_type, ["A", "B"])
        f.refresh_from_db()
        # Invoke the resolver the way Strawberry does — the wrapped function
        # reads self.__dict__["options"].
        assert CustomFieldType.options(f) == ["A", "B"]

    def test_graphql_resolver_empty_when_no_options(self):
        f = recap_models.CustomField.objects.create(
            name="Notes2",
            custom_recap_template=self.template,
            custom_field_type=self.text_type,
            recap_section=self.section,
            created_by=self.system_user,
        )
        f.refresh_from_db()
        assert CustomFieldType.options(f) == []
