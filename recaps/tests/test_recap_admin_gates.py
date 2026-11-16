"""
Write gates added to recap mutations that were only StrictIsAuthenticated
(and exposed on the spark, clients AND mobile schemas):

* nudgeAmbassadorForRecap — staff only (it pushes caller-written text).
* removeCustomField / updateRecapSection (+ the other template/section/
  field writes) — `_assert_caller_authorized_for_recap_tenant(...,
  block_ambassadors=True)`: admins anywhere, clients only in their own
  tenant, ambassadors never.
* create/updateCustomRecapFieldType — global catalog, staff only.
* importConnecteamRecapPdf — same tenant gate; pdfGcsPath must be an
  upload under connecteam-imports/.

Run through the real spark schema. Push + job queue are patched.
"""

from datetime import datetime, timedelta, timezone as dt_tz
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import sync_to_async

from ambassadors.models import AmbassadorEvent
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models

WHEN = datetime(2026, 11, 1, 17, 0, tzinfo=dt_tz.utc)

NUDGE = """
mutation Nudge($input: NudgeAmbassadorForRecapInput!) {
  nudgeAmbassadorForRecap(input: $input) { success message }
}
"""
REMOVE_FIELD = """
mutation Remove($input: RemoveCustomFieldInput!) {
  removeCustomField(input: $input) { success message }
}
"""
UPDATE_SECTION = """
mutation UpdSection($input: UpdateRecapSectionInput!) {
  updateRecapSection(input: $input) { success message }
}
"""
UPDATE_FIELD_TYPE = """
mutation UpdType($input: UpdateCustomRecapFieldTypeInput!) {
  updateCustomRecapFieldType(input: $input) { success message }
}
"""
IMPORT_PDF = """
mutation Import($input: ImportConnecteamRecapPdfInput!) {
  importConnecteamRecapPdf(input: $input) { success message }
}
"""


def _ok(result, name):
    return result.errors is None and result.data[name]["success"] is True


def _refused(result, name):
    return bool(result.errors) or result.data[name]["success"] is False


@pytest.mark.django_db(transaction=True)
class TestRecapAdminGates(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_spark import schema_spark

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.tenant_a = self.create_tenant(name="Brand A")
        self.tenant_b = self.create_tenant(name="Brand B")

        self.admin = self.create_user(
            username="rgate-admin", email="rgate-admin@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_a = self.create_user(
            username="rgate-client", email="rgate-client@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_a, self.tenant_a)
        self.ba_user = self.create_user(
            username="rgate-ba", email="rgate-ba@test.com",
            role=self.roles["ambassador"],
        )
        self.create_tenanted_user(self.ba_user, self.tenant_b)
        self.ambassador = self.create_ambassador(self.ba_user)

        self.field_type = recap_models.CustomRecapFieldType.objects.create(
            name="Text", created_by=self.system_user,
        )
        self.event_type_b = self.create_event_type(name="Demo", tenant=self.tenant_b)
        self.template_b = recap_models.CustomRecapTemplate.objects.create(
            name="B template", event_type=self.event_type_b,
            tenant=self.tenant_b, created_by=self.system_user,
        )
        self.section_b = recap_models.RecapSection.objects.create(
            name="B section", tenant=self.tenant_b, created_by=self.system_user,
        )
        self.section_a = recap_models.RecapSection.objects.create(
            name="A section", tenant=self.tenant_a, created_by=self.system_user,
        )
        self.field_b = recap_models.CustomField.objects.create(
            name="B field", custom_recap_template=self.template_b,
            custom_field_type=self.field_type, recap_section=self.section_b,
            created_by=self.system_user,
        )
        self.event_b = event_models.Event.objects.create(
            name="B event", tenant=self.tenant_b, address="",
            date=WHEN, start_time=WHEN, end_time=WHEN + timedelta(hours=4),
            created_by=self.system_user,
        )
        self.ae = AmbassadorEvent.objects.create(
            ambassador=self.ambassador, event=self.event_b,
            tenant=self.tenant_b, is_approved=True, created_by=self.admin,
        )

    def _non_admins(self):
        return {"anon": None, "ba": self.ba_user, "client": self.client_a}

    # ── nudgeAmbassadorForRecap ──────────────────────────────────────

    @pytest.mark.asyncio
    async def test_nudge_staff_only(self):
        variables = {
            "input": {
                "ambassadorEventUuid": str(self.ae.uuid),
                "title": "Free text",
                "body": "Anything at all",
            }
        }
        with patch(
            "ambassadors.push.send_push_to_user", new=AsyncMock(return_value=1)
        ) as push:
            for who, user in self._non_admins().items():
                result = await self._execute_mutation(
                    NUDGE, variables, self.endpoint_path, user=user,
                )
                assert _refused(result, "nudgeAmbassadorForRecap"), who
            push.assert_not_awaited()

            result = await self._execute_mutation(
                NUDGE, variables, self.endpoint_path, user=self.admin,
            )
            assert _ok(result, "nudgeAmbassadorForRecap"), result
            push.assert_awaited_once()

    # ── removeCustomField ────────────────────────────────────────────

    async def _field_exists(self):
        return await sync_to_async(
            recap_models.CustomField.objects.filter(id=self.field_b.id).exists
        )()

    @pytest.mark.asyncio
    async def test_remove_custom_field_gated(self):
        variables = {"input": {"id": str(self.field_b.id)}}
        for who, user in self._non_admins().items():
            result = await self._execute_mutation(
                REMOVE_FIELD, variables, self.endpoint_path, user=user,
            )
            assert _refused(result, "removeCustomField"), who
            assert await self._field_exists(), who

        result = await self._execute_mutation(
            REMOVE_FIELD, variables, self.endpoint_path, user=self.admin,
        )
        assert _ok(result, "removeCustomField"), result
        assert not await self._field_exists()

    # ── updateRecapSection ───────────────────────────────────────────

    async def _section(self, section):
        return await sync_to_async(
            lambda: recap_models.RecapSection.objects.values("name", "tenant_id").get(
                id=section.id
            )
        )()

    @pytest.mark.asyncio
    async def test_update_recap_section_gated(self):
        # Another brand's section, kept in its own tenant.
        steal = {
            "input": {
                "id": str(self.section_b.id),
                "name": "Stolen",
                "tenantId": str(self.tenant_a.id),
            }
        }
        for who, user in self._non_admins().items():
            result = await self._execute_mutation(
                UPDATE_SECTION, steal, self.endpoint_path, user=user,
            )
            assert _refused(result, "updateRecapSection"), who
        assert await self._section(self.section_b) == {
            "name": "B section", "tenant_id": self.tenant_b.id,
        }

        # A client can't push their own section into another tenant either.
        push_out = {
            "input": {
                "id": str(self.section_a.id),
                "name": "A section",
                "tenantId": str(self.tenant_b.id),
            }
        }
        result = await self._execute_mutation(
            UPDATE_SECTION, push_out, self.endpoint_path, user=self.client_a,
        )
        assert _refused(result, "updateRecapSection")
        assert (await self._section(self.section_a))["tenant_id"] == self.tenant_a.id

        # ...but can rename it in place.
        rename = {
            "input": {
                "id": str(self.section_a.id),
                "name": "A renamed",
                "tenantId": str(self.tenant_a.id),
            }
        }
        result = await self._execute_mutation(
            UPDATE_SECTION, rename, self.endpoint_path, user=self.client_a,
        )
        assert _ok(result, "updateRecapSection"), result

        # Admin: unchanged behaviour.
        admin_rename = {
            "input": {
                "id": str(self.section_b.id),
                "name": "B renamed",
                "tenantId": str(self.tenant_b.id),
            }
        }
        result = await self._execute_mutation(
            UPDATE_SECTION, admin_rename, self.endpoint_path, user=self.admin,
        )
        assert _ok(result, "updateRecapSection"), result
        assert await self._section(self.section_b) == {
            "name": "B renamed", "tenant_id": self.tenant_b.id,
        }

    # ── updateCustomRecapFieldType (global catalog) ──────────────────

    @pytest.mark.asyncio
    async def test_field_type_staff_only(self):
        variables = {"input": {"id": str(self.field_type.id), "name": "Hacked"}}
        for who, user in self._non_admins().items():
            result = await self._execute_mutation(
                UPDATE_FIELD_TYPE, variables, self.endpoint_path, user=user,
            )
            assert _refused(result, "updateCustomRecapFieldType"), who
        await sync_to_async(self.field_type.refresh_from_db)()
        assert self.field_type.name == "Text"

        result = await self._execute_mutation(
            UPDATE_FIELD_TYPE, variables, self.endpoint_path, user=self.admin,
        )
        assert _ok(result, "updateCustomRecapFieldType"), result

    # ── importConnecteamRecapPdf ─────────────────────────────────────

    @pytest.mark.asyncio
    async def test_import_connecteam_gated(self):
        def _vars(path):
            return {
                "input": {
                    "eventId": str(self.event_b.id),
                    "customRecapTemplateId": str(self.template_b.id),
                    "pdfGcsPath": path,
                }
            }

        good = _vars("connecteam-imports/0123456789abcdef0123456789abcdef.pdf")
        with patch(
            "recaps.mutation_parts.connecteam.enqueue_connecteam_import"
        ) as enqueue, patch(
            "recaps.mutation_parts.connecteam.write_connecteam_job"
        ):
            # Ambassador in the event's own tenant, client of another brand,
            # anonymous: all refused, nothing queued.
            for who, user in self._non_admins().items():
                result = await self._execute_mutation(
                    IMPORT_PDF, good, self.endpoint_path, user=user,
                )
                assert _refused(result, "importConnecteamRecapPdf"), who
            enqueue.assert_not_called()

            # Admin can't point the importer at an arbitrary object.
            result = await self._execute_mutation(
                IMPORT_PDF, _vars("recaps/someone-else/photo.pdf"),
                self.endpoint_path, user=self.admin,
            )
            assert _refused(result, "importConnecteamRecapPdf")
            assert (
                result.data["importConnecteamRecapPdf"]["message"]
                == "Invalid PDF upload path."
            )
            enqueue.assert_not_called()

            result = await self._execute_mutation(
                IMPORT_PDF, good, self.endpoint_path, user=self.admin,
            )
            assert _ok(result, "importConnecteamRecapPdf"), result
            enqueue.assert_called_once()
