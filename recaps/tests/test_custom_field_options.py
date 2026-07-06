"""Coverage for the select / multiselect choice field's `options`.

The dropdown / multi-select field types store their admin-defined allowed
choices in CustomField.options (a JSON list of strings). This pins:
  * the column round-trips a list of strings (migration 0026 + the model),
  * the GraphQL CustomField.options resolver returns that list THROUGH THE
    MOBILE SCHEMA (and [] for a non-choice field).

The schema-level checks matter: the resolver reads the JSON column via
__dict__, and the strawberry-django query optimizer will DEFER that column
(making options come back []) unless the resolver hints `only=["options"]`.
Calling the resolver as a bare function can't catch that — only running the
real `event(uuid:){ customRecapTemplate { customField { options } } }` query
does (this was the Feel Free "No options configured" bug).

The submitted answer itself rides in CustomFieldValue.value (a single option
string for 'select', a JSON array for 'multiselect') — just text, not re-tested.
"""
from __future__ import annotations

import pytest
from asgiref.sync import sync_to_async

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models


EVENT_Q = """
query Ev($uuid: ID!) {
  event(uuid: $uuid) {
    customRecapTemplate {
      customField { name options }
    }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestCustomFieldOptions(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_mobile import schema_mobile

        self.roles = self.setup_default_roles()
        self.schema = schema_mobile
        self.endpoint_path = "/api/v1/graphql/mobile"
        self.system_user = self.get_system_user()
        self.admin = self.create_user(
            username="opts-admin",
            email="opts@igniteproductions.co",
            role=self.roles["spark_admin"],
            is_staff=True,
        )
        self.tenant = self.create_tenant(name="SHB Options")
        self.event_type = self.create_event_type(name="Sampling", tenant=self.tenant)
        self.multiselect_type = recap_models.CustomRecapFieldType.objects.create(
            name="multiselect", created_by=self.system_user
        )
        self.text_type = recap_models.CustomRecapFieldType.objects.create(
            name="text", created_by=self.system_user
        )
        # Template targets the event_type so an event carrying it resolves to
        # this template (the tenant + event_type match path).
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

    async def _options_via_schema(self, field_type, options):
        """Create the field + an event that resolves to the template, run the
        real mobile event query, and return the field's `options` as the app
        would receive them."""
        def _seed():
            self._field(field_type, options)
            return self.create_event(
                name="Opts event", tenant=self.tenant, event_type=self.event_type
            )

        event = await sync_to_async(_seed)()
        res = await self._execute_mutation(
            EVENT_Q, {"uuid": str(event.uuid)}, user=self.admin
        )
        assert res.errors is None, res.errors
        tpl = res.data["event"]["customRecapTemplate"]
        assert tpl is not None
        fld = next(
            f for f in tpl["customField"] if f["name"] == "What market is this?"
        )
        return fld["options"]

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

    @pytest.mark.asyncio
    async def test_choice_options_survive_the_schema(self):
        # The regression: a multiselect field's options must reach the app via
        # the mobile event query (optimizer must not defer the column).
        assert await self._options_via_schema(self.multiselect_type, ["A", "B"]) == [
            "A",
            "B",
        ]

    @pytest.mark.asyncio
    async def test_non_choice_field_options_empty_via_schema(self):
        assert await self._options_via_schema(self.text_type, []) == []
