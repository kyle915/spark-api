"""
Event.customRecapTemplate resolution — the fix for Feel Free's recap fields
not showing in the mobile app.

The mobile RecapSubmitScreen gates the data-entry form on
`event.customRecapTemplate`. Most events have no direct
`custom_recap_template_id` FK, so the resolver falls back. This pins the
fallback order (first hit wins):

  1. direct FK (not exercised here — the common case has none);
  2. the template of a recap ALREADY filed for this event (Feel Free: the
     approved dashboard recap points straight at its template — authoritative
     even when the tenant has several templates and the event has no matching
     event_type);
  3. the tenant + event_type match (the desktop recap form's match);
  4. the tenant's SOLE template, if it has exactly one.

Runs the real `event(uuid:)` query through the mobile schema so it exercises
the actual async resolver (not just a helper), including the nested
`customField` shape the app reads to render inputs.
"""
import pytest
from asgiref.sync import sync_to_async

from events.tests.base import EventsGraphQLTestCase
from recaps import models as rm


EVENT_Q = """
query Ev($uuid: ID!) {
  event(uuid: $uuid) {
    id
    uuid
    customRecapTemplate {
      id
      uuid
      name
      customField {
        id
        name
        required
        customFieldType { id name }
        recapSection { id name }
        options
      }
    }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestEventCustomRecapTemplateResolve(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_mobile import schema_mobile

        self.roles = self.setup_default_roles()
        self.schema = schema_mobile
        self.endpoint_path = "/api/v1/graphql/mobile"
        self.sys = self.get_system_user()
        # Ignite-domain spark admin can read events across tenants.
        self.admin = self.create_user(
            username="ff-admin",
            email="admin@igniteproductions.co",
            role=self.roles["spark_admin"],
            is_staff=True,
        )
        self.text_type = rm.CustomRecapFieldType.objects.create(
            name="text", created_by=self.sys
        )

    # ---- sync helpers (call only inside sync_to_async) ------------------
    def _template(self, tenant, name, event_type=None):
        return rm.CustomRecapTemplate.objects.create(
            name=name, event_type=event_type, tenant=tenant, created_by=self.sys
        )

    def _field(self, template, tenant, name):
        section = rm.RecapSection.objects.create(
            name=f"{name} Section", tenant=tenant, created_by=self.sys
        )
        return rm.CustomField.objects.create(
            name=name,
            custom_recap_template=template,
            custom_field_type=self.text_type,
            recap_section=section,
            created_by=self.sys,
        )

    async def _resolve_template(self, event):
        res = await self._execute_mutation(
            EVENT_Q, {"uuid": str(event.uuid)}, user=self.admin
        )
        assert res.errors is None, res.errors
        return res.data["event"]["customRecapTemplate"]

    # ---- scenarios -----------------------------------------------------
    @pytest.mark.asyncio
    async def test_existing_recap_template_wins_even_when_ambiguous(self):
        """The Feel Free case: event has NO event_type and the tenant has TWO
        templates (ambiguous by count), but a recap already filed for the event
        binds a specific template — so THAT template (and its fields) resolves."""
        def _seed():
            tenant = self.create_tenant(name="Feel Free")
            et_a = self.create_event_type(name="Type A", tenant=tenant)
            et_b = self.create_event_type(name="Type B", tenant=tenant)
            tpl_a = self._template(tenant, "Template A", event_type=et_a)
            tpl_b = self._template(tenant, "Template B", event_type=et_b)
            self._field(tpl_a, tenant, "Field A")
            self._field(tpl_b, tenant, "Cans Sampled")
            # Event carries NO event_type and NO direct FK — the exact shape
            # that used to fall through to the photo-only form.
            event = self.create_event(name="Austin 6th St", tenant=tenant)
            rm.CustomRecap.objects.create(
                name="filed", event=event, tenant=tenant,
                custom_recap_template=tpl_b, created_by=self.sys, updated_by=self.sys,
            )
            return event, tpl_b

        event, tpl_b = await sync_to_async(_seed)()
        tpl = await self._resolve_template(event)
        assert tpl is not None, "expected the event's own recap template, got null"
        assert tpl["uuid"] == str(tpl_b.uuid)
        assert tpl["name"] == "Template B"
        names = {f["name"] for f in tpl["customField"]}
        assert names == {"Cans Sampled"}  # tpl_b's field, not tpl_a's

    @pytest.mark.asyncio
    async def test_sole_template_fallback_when_no_event_type_no_recap(self):
        """No event_type, no recap yet, but the tenant has exactly ONE
        template — unambiguous, so use it (a brand-new BA filing the first
        recap for a Feel Free event still gets the form)."""
        def _seed():
            tenant = self.create_tenant(name="Sole Tpl Co")
            # Templates always carry an event_type (NOT NULL); the Feel Free
            # reality is the EVENT lacks a matching type, so give the template
            # a type but leave the event's type unset.
            et = self.create_event_type(name="Sole Type", tenant=tenant)
            tpl = self._template(tenant, "Only Template", event_type=et)
            self._field(tpl, tenant, "Consumers Reached")
            event = self.create_event(name="No type event", tenant=tenant)
            return event, tpl

        event, tpl = await sync_to_async(_seed)()
        resolved = await self._resolve_template(event)
        assert resolved is not None
        assert resolved["uuid"] == str(tpl.uuid)
        assert {f["name"] for f in resolved["customField"]} == {"Consumers Reached"}

    @pytest.mark.asyncio
    async def test_ambiguous_no_event_type_no_recap_is_null(self):
        """No event_type, no recap, and 2+ templates → we can't guess which,
        so stay null (the app safely shows the legacy photo form rather than a
        wrong template)."""
        def _seed():
            tenant = self.create_tenant(name="Ambiguous Co")
            self._template(tenant, "T1", event_type=self.create_event_type("E1", tenant))
            self._template(tenant, "T2", event_type=self.create_event_type("E2", tenant))
            return self.create_event(name="Ambiguous event", tenant=tenant)

        event = await sync_to_async(_seed)()
        assert await self._resolve_template(event) is None

    @pytest.mark.asyncio
    async def test_event_type_match_used_over_sole_or_ambiguous(self):
        """When the event DOES carry an event_type that a template targets, the
        tenant+event_type match wins (desktop's match) — even with 2 templates
        and no filed recap."""
        def _seed():
            tenant = self.create_tenant(name="Typed Co")
            et_x = self.create_event_type(name="X", tenant=tenant)
            et_y = self.create_event_type(name="Y", tenant=tenant)
            self._template(tenant, "TX", event_type=et_x)
            tpl_y = self._template(tenant, "TY", event_type=et_y)
            event = self.create_event(name="Typed event", tenant=tenant, event_type=et_y)
            return event, tpl_y

        event, tpl_y = await sync_to_async(_seed)()
        resolved = await self._resolve_template(event)
        assert resolved is not None
        assert resolved["uuid"] == str(tpl_y.uuid)

    @pytest.mark.asyncio
    async def test_tenant_without_templates_stays_null(self):
        """A legacy photo-only tenant (no templates at all) is unaffected —
        customRecapTemplate stays null so the app keeps the legacy form."""
        def _seed():
            tenant = self.create_tenant(name="Legacy Photo Co")
            return self.create_event(name="Legacy event", tenant=tenant)

        event = await sync_to_async(_seed)()
        assert await self._resolve_template(event) is None
