"""Staff "Edit recap" metric corrections: staff-only, audited, live totals.

A Brew Dr-style walk-up Retail recap is corrected through updateCustomRecap.
The edit must land in the conversion/Insights totals immediately, leave a
RecapMetricEdit row per changed value, keep approval as-is, send no email,
drop the stale Spark PDF and bump the recap dashboard cache.
"""

from datetime import datetime
from unittest import mock

import pytest
from asgiref.sync import sync_to_async
from django.core import mail
from django.core.cache import cache
from django.utils import timezone

from ambassadors.models import FileType
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models
from recaps.tenant_overview import sales_program_metrics


UPDATE = """
mutation UpdateCustomRecap($input: UpdateCustomRecapInput!) {
  updateCustomRecap(input: $input) {
    success
    message
    customRecap { uuid approved totalEngagements }
  }
}
"""

HISTORY = """
query CustomRecapHistory($uuid: ID!) {
  customRecap(uuid: $uuid) {
    metricEdits { fieldKey fieldLabel oldValue newValue reason editedByName }
  }
}
"""


class _BrewDrRecapBase(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Brew Dr. Kombucha")
        self.admin = self.create_user(
            username="ops-editor",
            email="ops-editor@test.com",
            role=self.roles["spark_admin"],
            first_name="Ops",
            last_name="Editor",
        )
        self.client_user = self.create_user(
            username="brand-viewer",
            email="brand-viewer@brewdr.test",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        today = timezone.localdate()
        self.today = today
        when = timezone.make_aware(datetime(today.year, today.month, today.day, 12))
        event_type = event_models.EventType.objects.create(
            name="Retail Sampling",
            slug="brewdr-edit-retail",
            tenant=self.tenant,
            created_by=self.sys,
        )
        self.event = event_models.Event.objects.create(
            name="New Seasons Arbor Lodge",
            tenant=self.tenant,
            address="1 Main",
            date=when,
            event_type=event_type,
            created_by=self.sys,
            updated_by=self.sys,
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Brew Dr Retail Sampling",
            event_type=event_type,
            tenant=self.tenant,
            created_by=self.sys,
        )
        number = recap_models.CustomRecapFieldType.objects.create(
            name="number", created_by=self.sys
        )
        section = recap_models.RecapSection.objects.create(
            name="Sampling", tenant=self.tenant, created_by=self.sys
        )
        self.sampled_field = recap_models.CustomField.objects.create(
            name="Total number of consumers sampled",
            custom_recap_template=self.template,
            custom_field_type=number,
            recap_section=section,
            created_by=self.sys,
        )
        self.cans_field = recap_models.CustomField.objects.create(
            name="How many single cans did consumers purchase?",
            custom_recap_template=self.template,
            custom_field_type=number,
            recap_section=section,
            created_by=self.sys,
        )
        self.recap = recap_models.CustomRecap.objects.create(
            name="New Seasons Arbor Lodge",
            approved=True,
            approved_at=timezone.now(),
            event=self.event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            total_engagements=50,
            created_by=self.sys,
        )
        self.sampled_value = recap_models.CustomFieldValue.objects.create(
            custom_recap=self.recap,
            custom_field=self.sampled_field,
            value="50",
            created_by=self.sys,
        )
        self.cans_value = recap_models.CustomFieldValue.objects.create(
            custom_recap=self.recap,
            custom_field=self.cans_field,
            value="10",
            created_by=self.sys,
        )

    def _input(self, *, sampled="50", cans="10", engagements=50, reason=None):
        payload = {
            "id": str(self.recap.id),
            "eventId": str(self.event.id),
            "customRecapTemplateId": str(self.template.id),
            "name": self.recap.name,
            "totalEngagements": engagements,
            "customFieldValues": [
                {
                    "customFieldId": str(self.sampled_field.id),
                    "customFieldValueId": str(self.sampled_value.id),
                    "value": sampled,
                },
                {
                    "customFieldId": str(self.cans_field.id),
                    "customFieldValueId": str(self.cans_value.id),
                    "value": cans,
                },
            ],
        }
        if reason is not None:
            payload["editReason"] = reason
        return {"input": payload}

    async def _update(self, user, **kwargs):
        result = await self._execute_mutation_authenticated(
            UPDATE, self._input(**kwargs), user, self.endpoint_path
        )
        assert result.errors is None, result.errors
        return result.data["updateCustomRecap"]

    def _metrics(self):
        return sales_program_metrics(self.tenant.id, self.today, self.today)

    def _edits(self):
        return list(
            recap_models.RecapMetricEdit.objects.filter(
                custom_recap=self.recap
            ).order_by("id")
        )


@pytest.mark.django_db(transaction=True)
class TestRecapMetricEdit(_BrewDrRecapBase):
    @pytest.mark.asyncio
    async def test_staff_edit_updates_totals_and_records_history(self):
        before = await sync_to_async(self._metrics)()
        assert before["consumers_sampled"] == 50
        assert before["units_sold"] == 10

        payload = await self._update(
            self.admin, sampled="80", cans="20", reason="BA typo per photos"
        )

        assert payload["success"] is True, payload
        assert payload["customRecap"]["approved"] is True
        after = await sync_to_async(self._metrics)()
        assert after["consumers_sampled"] == 80
        assert after["units_sold"] == 20

        edits = await sync_to_async(self._edits)()
        assert [(e.field_label, e.old_value, e.new_value) for e in edits] == [
            ("Total number of consumers sampled", "50", "80"),
            ("How many single cans did consumers purchase?", "10", "20"),
        ]
        assert {e.edited_by_id for e in edits} == {self.admin.id}
        assert {e.reason for e in edits} == {"BA typo per photos"}
        assert len({e.batch for e in edits}) == 1
        assert mail.outbox == []

    @pytest.mark.asyncio
    async def test_client_user_cannot_edit(self):
        payload = await self._update(self.client_user, sampled="999")

        assert payload["success"] is False
        assert "Ignite staff" in payload["message"]
        value = await sync_to_async(
            lambda: recap_models.CustomFieldValue.objects.get(id=self.sampled_value.id).value
        )()
        assert value == "50"
        assert await sync_to_async(self._edits)() == []

    @pytest.mark.asyncio
    async def test_negative_number_rejected(self):
        payload = await self._update(self.admin, cans="-4")

        assert payload["success"] is False
        assert "0 or more" in payload["message"]
        assert await sync_to_async(self._edits)() == []

    @pytest.mark.asyncio
    async def test_untouched_save_records_nothing(self):
        payload = await self._update(self.admin)

        assert payload["success"] is True, payload
        assert await sync_to_async(self._edits)() == []

    @pytest.mark.asyncio
    async def test_history_visible_to_staff_only(self):
        await self._update(self.admin, sampled="75", reason="recount")

        staff = await self._execute_query_authenticated(
            HISTORY, {"uuid": str(self.recap.uuid)}, self.admin, self.endpoint_path
        )
        assert staff.errors is None, staff.errors
        assert staff.data["customRecap"]["metricEdits"] == [
            {
                "fieldKey": f"field:{self.sampled_field.id}",
                "fieldLabel": "Total number of consumers sampled",
                "oldValue": "50",
                "newValue": "75",
                "reason": "recount",
                "editedByName": "Ops Editor",
            }
        ]

        client = await self._execute_query_authenticated(
            HISTORY,
            {"uuid": str(self.recap.uuid)},
            self.client_user,
            self.endpoint_path,
        )
        assert client.errors is None, client.errors
        assert client.data["customRecap"]["metricEdits"] == []

    @pytest.mark.asyncio
    async def test_edit_drops_stale_pdf_and_bumps_dashboard_cache(self):
        def _seed_pdf():
            pdf_type = FileType.objects.create(
                name="PDF", extension=".pdf", created_by=self.sys
            )
            return recap_models.CustomRecapFile.objects.create(
                name="Custom Recap PDF - New Seasons",
                url="recaps/pdfs/new-seasons.pdf",
                file_type=pdf_type,
                custom_recap=self.recap,
                approved=False,
                created_by=self.sys,
            )

        pdf = await sync_to_async(_seed_pdf)()
        version_key = f"dashboard:version:recap_dashboard:{self.tenant.id}"
        cache.set(version_key, 3, timeout=None)

        with mock.patch(
            "recaps.mutation_parts.pdf_helpers.delete_blob"
        ) as delete_blob:
            payload = await self._update(self.admin, cans="12")

        assert payload["success"] is True, payload
        delete_blob.assert_called_once_with("recaps/pdfs/new-seasons.pdf")
        assert not await sync_to_async(
            recap_models.CustomRecapFile.objects.filter(id=pdf.id).exists
        )()
        assert cache.get(version_key) == 4


UPDATE_STANDARD = """
mutation UpdateRecap($input: UpdateRecapInput!) {
  updateRecap(input: $input) { success message }
}
"""


@pytest.mark.django_db(transaction=True)
class TestStandardRecapMetricEdit(_BrewDrRecapBase):
    def _standard_recap(self):
        photo_type = FileType.objects.create(
            name="Image", extension=".jpg", created_by=self.sys
        )
        recap = recap_models.Recap.objects.create(
            name="Legacy recap",
            event=self.event,
            total_cans_sold=10,
            approved=True,
            created_by=self.sys,
        )
        recap_models.RecapFile.objects.create(
            name="photo",
            file="recaps/photo.jpg",
            file_type=photo_type,
            recap=recap,
            created_by=self.sys,
        )
        return recap

    async def _update_standard(self, user, recap, cans):
        result = await self._execute_mutation_authenticated(
            UPDATE_STANDARD,
            {
                "input": {
                    "id": str(recap.id),
                    "eventId": str(self.event.id),
                    "name": recap.name,
                    "files": [{"file": "recaps/photo.jpg"}],
                    "totalCansSold": cans,
                    "editReason": "recount",
                }
            },
            user,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        return result.data["updateRecap"]

    @pytest.mark.asyncio
    async def test_standard_recap_staff_edit_is_audited(self):
        recap = await sync_to_async(self._standard_recap)()

        payload = await self._update_standard(self.admin, recap, 14)

        assert payload["success"] is True, payload
        edits = await sync_to_async(
            lambda: list(recap_models.RecapMetricEdit.objects.filter(recap=recap))
        )()
        assert [(e.field_label, e.old_value, e.new_value, e.reason) for e in edits] == [
            ("Total cans sold", "10", "14", "recount")
        ]
        assert mail.outbox == []

    @pytest.mark.asyncio
    async def test_standard_recap_client_refused(self):
        recap = await sync_to_async(self._standard_recap)()

        payload = await self._update_standard(self.client_user, recap, 99)

        assert payload["success"] is False
        assert "Ignite staff" in payload["message"]
