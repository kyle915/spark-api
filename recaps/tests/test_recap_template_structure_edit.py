"""
Tests for the custom-recap-template STRUCTURE-EDIT mutations
(`moveCustomFieldToSection` + `deleteRecapSection`), run end to end
against the real `schema_clients` GraphQL surface.

These let an admin rearrange an existing template — e.g. move an
"Account Spend Amount" field out of an "Additional Insights" section
into an "Engagement + Spend" section, then delete the now-empty
"Additional Insights" section — WITHOUT destroying captured recap data.

Coverage:
  * moveCustomFieldToSection reassigns CustomField.recap_section AND
    preserves the field row + every CustomFieldValue already captured
    for it (the move is a pointer change, never delete+recreate).
  * moveCustomFieldToSection rejects a target section from a DIFFERENT
    template (and therefore a different tenant) — cross-template moves.
  * deleteRecapSection deletes an EMPTY section; refuses a NON-EMPTY
    one with the clear "Move or remove this section's fields before
    deleting it." error (and leaves the section + its fields intact).
  * Tenant isolation: a caller from another tenant can neither move a
    field nor delete a section (mirrors removeCustomField gating —
    cross-tenant denial reads as non-existence, never leaks the record).
  * A spark-admin can do both in any tenant.

Mirrors the fixture style of test_recap_mutation_tenant_isolation.
"""

import pytest

from asgiref.sync import sync_to_async

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models


# ── Mutations under test ──────────────────────────────────────────────

MOVE_FIELD_MUTATION = """
mutation MoveCustomFieldToSection($fieldId: ID!, $sectionId: ID!) {
  moveCustomFieldToSection(input: { fieldId: $fieldId, sectionId: $sectionId }) {
    success
    message
    customField {
      uuid
      recapSectionId
    }
  }
}
"""

DELETE_SECTION_MUTATION = """
mutation DeleteRecapSection($sectionId: ID!) {
  deleteRecapSection(input: { sectionId: $sectionId }) {
    success
    message
    deletedRecapSectionUuid
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestRecapTemplateStructureEdit(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()

        # Tenant A (the caller's tenant) and tenant B (the foreign one).
        self.tenant = self.create_tenant(name="Girl Beer")
        self.other_tenant = self.create_tenant(name="Liquid Death")

        self.spark_admin = self.create_user(
            username="admin-struct",
            email="admin-struct@test.com",
            role=self.roles["spark_admin"],
        )
        # Client belongs to tenant A only.
        self.client_user = self.create_user(
            username="client-struct",
            email="client-struct@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        # Client belonging to tenant B only (the cross-tenant attacker).
        self.other_client_user = self.create_user(
            username="other-client-struct",
            email="other-client-struct@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.other_client_user, self.other_tenant)

        # Shared field type (no tenant FK on CustomRecapFieldType).
        self.field_type = recap_models.CustomRecapFieldType.objects.create(
            name="Text",
            created_by=self.system_user,
        )

        # ── Tenant A template with TWO sections ──────────────────────
        self.event_type = self.create_event_type(
            name="Sampling", tenant=self.tenant
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="GB Template",
            event_type=self.event_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )
        # "Additional Insights" — source section (holds the field we move).
        self.section_insights = recap_models.RecapSection.objects.create(
            name="Additional Insights",
            tenant=self.tenant,
            created_by=self.system_user,
        )
        # "Engagement + Spend" — destination section.
        self.section_engagement = recap_models.RecapSection.objects.create(
            name="Engagement + Spend",
            tenant=self.tenant,
            created_by=self.system_user,
        )
        # The field being moved: "Account Spend Amount", starts in
        # "Additional Insights".
        self.field = recap_models.CustomField.objects.create(
            name="Account Spend Amount",
            custom_recap_template=self.template,
            custom_field_type=self.field_type,
            recap_section=self.section_insights,
            created_by=self.system_user,
        )
        # A second field already living in the destination section, so the
        # destination is a valid "same-template" target.
        self.dest_anchor_field = recap_models.CustomField.objects.create(
            name="Total Engagements (anchor)",
            custom_recap_template=self.template,
            custom_field_type=self.field_type,
            recap_section=self.section_engagement,
            created_by=self.system_user,
        )

        # A submitted recap with a captured VALUE for the field we move,
        # to prove the move preserves answers.
        self.event = self.create_event(name="Whole Foods Burbank", tenant=self.tenant)
        self.custom_recap = recap_models.CustomRecap.objects.create(
            name="Custom recap",
            event=self.event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.system_user,
        )
        self.field_value = recap_models.CustomFieldValue.objects.create(
            value="$4,200",
            custom_recap=self.custom_recap,
            custom_field=self.field,
            created_by=self.system_user,
        )

        # ── Tenant B template with its OWN section (cross-template) ───
        self.other_event_type = self.create_event_type(
            name="Sampling B", tenant=self.other_tenant
        )
        self.other_template = recap_models.CustomRecapTemplate.objects.create(
            name="LD Template",
            event_type=self.other_event_type,
            tenant=self.other_tenant,
            created_by=self.system_user,
        )
        self.other_section = recap_models.RecapSection.objects.create(
            name="LD Section",
            tenant=self.other_tenant,
            created_by=self.system_user,
        )
        # Anchor a field in the other template's section so it's a "real"
        # in-use section (and clearly a different template).
        self.other_field = recap_models.CustomField.objects.create(
            name="LD Field",
            custom_recap_template=self.other_template,
            custom_field_type=self.field_type,
            recap_section=self.other_section,
            created_by=self.system_user,
        )

    # ── helpers ─────────────────────────────────────────────────────

    async def _refresh_field(self, field):
        return await sync_to_async(recap_models.CustomField.objects.get)(
            id=field.id
        )

    async def _section_exists(self, section_id):
        return await sync_to_async(
            recap_models.RecapSection.objects.filter(id=section_id).exists
        )()

    # ════════════════════════════════════════════════════════════════
    # moveCustomFieldToSection
    # ════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_move_field_reassigns_section_and_preserves_values(self):
        result = await self._execute_mutation_authenticated(
            MOVE_FIELD_MUTATION,
            {
                "fieldId": str(self.field.id),
                "sectionId": str(self.section_engagement.id),
            },
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["moveCustomFieldToSection"]
        assert payload["success"] is True, payload
        assert payload["customField"]["uuid"] == str(self.field.uuid)

        # The field row is the SAME row, now pointing at the new section.
        refreshed = await self._refresh_field(self.field)
        assert refreshed.id == self.field.id
        assert refreshed.recap_section_id == self.section_engagement.id

        # Its captured value survived untouched (same row, same field FK,
        # same value) — the move did NOT delete+recreate the field.
        value = await sync_to_async(recap_models.CustomFieldValue.objects.get)(
            id=self.field_value.id
        )
        assert value.custom_field_id == self.field.id
        assert value.value == "$4,200"
        # And it still resolves under the field that now lives in the new
        # section.
        value_count = await sync_to_async(
            recap_models.CustomFieldValue.objects.filter(
                custom_field=refreshed
            ).count
        )()
        assert value_count == 1

    @pytest.mark.asyncio
    async def test_spark_admin_can_move_field(self):
        result = await self._execute_mutation_authenticated(
            MOVE_FIELD_MUTATION,
            {
                "fieldId": str(self.field.id),
                "sectionId": str(self.section_engagement.id),
            },
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["moveCustomFieldToSection"]
        assert payload["success"] is True, payload
        refreshed = await self._refresh_field(self.field)
        assert refreshed.recap_section_id == self.section_engagement.id

    @pytest.mark.asyncio
    async def test_cannot_move_field_into_other_template_section(self):
        # Target a section that belongs to a DIFFERENT template (tenant B).
        result = await self._execute_mutation_authenticated(
            MOVE_FIELD_MUTATION,
            {
                "fieldId": str(self.field.id),
                "sectionId": str(self.other_section.id),
            },
            self.spark_admin,  # admin, so it's the template-match that blocks
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["moveCustomFieldToSection"]
        assert payload["success"] is False, payload
        assert "same template" in payload["message"].lower(), payload
        # NOT moved — still in the original section.
        refreshed = await self._refresh_field(self.field)
        assert refreshed.recap_section_id == self.section_insights.id

    @pytest.mark.asyncio
    async def test_other_tenant_client_cannot_move_field(self):
        # A client of tenant B tries to restructure tenant A's template.
        result = await self._execute_mutation_authenticated(
            MOVE_FIELD_MUTATION,
            {
                "fieldId": str(self.field.id),
                "sectionId": str(self.section_engagement.id),
            },
            self.other_client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["moveCustomFieldToSection"]
        assert payload["success"] is False, payload
        # Cross-tenant denial reads as non-existence (no record-existence
        # leak), mirroring removeCustomField / the recap-mutation gate.
        assert payload["message"] == "Custom field not found.", payload
        assert "authorized" not in payload["message"].lower(), payload
        # NOT moved.
        refreshed = await self._refresh_field(self.field)
        assert refreshed.recap_section_id == self.section_insights.id

    # ════════════════════════════════════════════════════════════════
    # deleteRecapSection
    # ════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_delete_empty_section_succeeds(self):
        # Empty the source section first by moving its only field out.
        await sync_to_async(
            recap_models.CustomField.objects.filter(id=self.field.id).update
        )(recap_section=self.section_engagement)

        result = await self._execute_mutation_authenticated(
            DELETE_SECTION_MUTATION,
            {"sectionId": str(self.section_insights.id)},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["deleteRecapSection"]
        assert payload["success"] is True, payload
        assert payload["deletedRecapSectionUuid"] == str(
            self.section_insights.uuid
        )
        # Section is gone.
        assert await self._section_exists(self.section_insights.id) is False

    @pytest.mark.asyncio
    async def test_delete_non_empty_section_is_refused(self):
        # section_insights still has self.field — must refuse.
        result = await self._execute_mutation_authenticated(
            DELETE_SECTION_MUTATION,
            {"sectionId": str(self.section_insights.id)},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["deleteRecapSection"]
        assert payload["success"] is False, payload
        assert payload["message"] == (
            "Move or remove this section's fields before deleting it."
        ), payload
        # Section AND its field are intact.
        assert await self._section_exists(self.section_insights.id) is True
        refreshed = await self._refresh_field(self.field)
        assert refreshed.recap_section_id == self.section_insights.id

    @pytest.mark.asyncio
    async def test_spark_admin_can_delete_empty_section(self):
        # section_engagement has the anchor field; empty it first.
        await sync_to_async(
            recap_models.CustomField.objects.filter(
                id=self.dest_anchor_field.id
            ).update
        )(recap_section=self.section_insights)

        result = await self._execute_mutation_authenticated(
            DELETE_SECTION_MUTATION,
            {"sectionId": str(self.section_engagement.id)},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["deleteRecapSection"]
        assert payload["success"] is True, payload
        assert await self._section_exists(self.section_engagement.id) is False

    @pytest.mark.asyncio
    async def test_other_tenant_client_cannot_delete_section(self):
        # A client of tenant B tries to delete tenant A's (empty) section.
        # Empty it first so the ONLY thing that can block is the tenant gate.
        await sync_to_async(
            recap_models.CustomField.objects.filter(id=self.field.id).update
        )(recap_section=self.section_engagement)

        result = await self._execute_mutation_authenticated(
            DELETE_SECTION_MUTATION,
            {"sectionId": str(self.section_insights.id)},
            self.other_client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["deleteRecapSection"]
        assert payload["success"] is False, payload
        # Cross-tenant denial reads as non-existence.
        assert payload["message"] == "Recap section not found.", payload
        assert "authorized" not in payload["message"].lower(), payload
        # Still there.
        assert await self._section_exists(self.section_insights.id) is True
