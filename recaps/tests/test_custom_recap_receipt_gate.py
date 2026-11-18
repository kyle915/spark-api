"""createCustomRecap / updateCustomRecap: a BA's spend needs a receipt photo.

Through the real ``schema_clients`` surface (the app files custom recaps via
createCustomRecap, photos uploaded first and sent inline as ``files``):

* a BA filing spend > 0 with no receipt-category file is refused with the
  BA-facing sentence and leaves no recap behind;
* the receipt slot sentinel "2" (what the app sends) clears it;
* staff filing / editing on a BA's behalf is never blocked;
* a BA edit is judged on the recap as it ends up.
"""

from datetime import datetime, timedelta, timezone as _tz

import pytest
from asgiref.sync import sync_to_async
from strawberry.relay import to_base64

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models

MESSAGE = "Add a photo of your receipt — you entered $65.00 in product spend."

CREATE = """
mutation Create($input: CreateCustomRecapInput!) {
  createCustomRecap(input: $input) {
    success
    message
    customRecap { id }
  }
}
"""

UPDATE = """
mutation Update($input: UpdateCustomRecapInput!) {
  updateCustomRecap(input: $input) {
    success
    message
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestCustomRecapReceiptGate(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients
        from recaps.models import (
            CustomField,
            CustomRecapFieldType,
            FileRecapCategory,
            FileType,
            RecapSection,
        )

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        system = self.get_system_user()
        self.tenant = self.create_tenant(name="Sipli GraphQL")
        self.admin = self.create_user(
            username="admin-rg@test.com",
            email="admin-rg@test.com",
            role=self.roles["spark_admin"],
        )
        self.ba_user = self.create_user(
            username="ba-rg@test.com",
            email="ba-rg@test.com",
            role=self.roles["ambassador"],
        )
        self.ba = self.create_ambassador(self.ba_user)
        from tenants.models import TenantedUser

        TenantedUser.objects.get_or_create(
            user=self.ba_user, tenant=self.tenant, defaults={"created_by": system}
        )
        now = datetime.now(_tz.utc)
        etype = self.create_event_type(name="Sampling", tenant=self.tenant)
        self.event = self.create_event(
            name="Sprouts Main",
            tenant=self.tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
            event_type=etype,
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Sipli", event_type=etype, tenant=self.tenant, created_by=system
        )
        text, _ = CustomRecapFieldType.objects.get_or_create(
            name="text", defaults={"created_by": system}
        )
        section = RecapSection.objects.create(
            tenant=self.tenant, name="Spend", order=0, created_by=system
        )
        self.spend_field = CustomField.objects.create(
            name="Product spend",
            custom_recap_template=self.template,
            custom_field_type=text,
            recap_section=section,
            order=0,
            created_by=system,
        )
        FileType.objects.get_or_create(name="image", defaults={"created_by": system})
        FileRecapCategory.objects.create(
            name="Sampling photos", tenant=self.tenant, created_by=system
        )
        FileRecapCategory.objects.create(
            name="Receipts", tenant=self.tenant, created_by=system
        )

    def _input(self, spend="65.00", files=None, **extra):
        out = {
            "eventId": str(self.event.id),
            "customRecapTemplateId": str(self.template.id),
            "name": "Sprouts recap",
            "customFieldValues": [
                {"customFieldId": str(self.spend_field.id), "value": spend}
            ],
            "files": files or [],
        }
        out.update(extra)
        return out

    @staticmethod
    def _file(name, slot):
        return {"file": f"recap_files/test/{name}.jpg", "fileRecapCategoryId": slot}

    async def _create(self, user, **kw):
        result = await self._execute_mutation_authenticated(
            CREATE, {"input": self._input(**kw)}, user, self.endpoint_path
        )
        assert result.errors is None, result.errors
        return result.data["createCustomRecap"]

    async def _count(self):
        return await sync_to_async(
            recap_models.CustomRecap.objects.filter(event=self.event).count
        )()

    @pytest.mark.asyncio
    async def test_ba_spend_without_receipt_is_refused(self):
        payload = await self._create(self.ba_user, files=[self._file("a", "1")])
        assert payload["success"] is False, payload
        assert payload["message"] == MESSAGE
        assert await self._count() == 0

    @pytest.mark.asyncio
    async def test_ba_spend_with_receipt_slot_files(self):
        payload = await self._create(
            self.ba_user, files=[self._file("a", "1"), self._file("r", "2")]
        )
        assert payload["success"] is True, payload
        assert await self._count() == 1

    @pytest.mark.asyncio
    async def test_ba_zero_spend_needs_no_receipt(self):
        payload = await self._create(
            self.ba_user, spend="0.00", files=[self._file("a", "1")]
        )
        assert payload["success"] is True, payload

    @pytest.mark.asyncio
    async def test_staff_filing_spend_without_receipt_is_not_blocked(self):
        payload = await self._create(
            self.admin,
            files=[],
            ambassadorId=to_base64("Ambassador", self.ba.id),
            fillingForAmbassador=True,
        )
        assert payload["success"] is True, payload

    def _make_recap(self, with_receipt: bool):
        recap = recap_models.CustomRecap.objects.create(
            name="Existing",
            event=self.event,
            ambassador=self.ba,
            tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.ba_user,
        )
        if with_receipt:
            recap_models.CustomRecapFile.objects.create(
                name="r",
                url="recap_files/test/r0.jpg",
                file_type=recap_models.FileType.objects.first(),
                file_recap_category=recap_models.FileRecapCategory.objects.get(
                    tenant=self.tenant, name="Receipts"
                ),
                custom_recap=recap,
                created_by=self.ba_user,
            )
        return recap

    async def _update(self, user, recap):
        variables = {
            "input": {
                "id": str(recap.id),
                "eventId": str(self.event.id),
                "customRecapTemplateId": str(self.template.id),
                "name": "Existing",
                "customFieldValues": [
                    {"customFieldId": str(self.spend_field.id), "value": "65.00"}
                ],
            }
        }
        result = await self._execute_mutation_authenticated(
            UPDATE, variables, user, self.endpoint_path
        )
        assert result.errors is None, result.errors
        return result.data["updateCustomRecap"]

    @pytest.mark.asyncio
    async def test_ba_edit_adding_spend_without_receipt_is_refused(self):
        recap = await sync_to_async(self._make_recap)(with_receipt=False)
        payload = await self._update(self.ba_user, recap)
        assert payload["success"] is False, payload
        assert payload["message"] == MESSAGE
        values = await sync_to_async(
            lambda: list(
                recap_models.CustomFieldValue.objects.filter(custom_recap=recap)
            )
        )()
        assert values == []  # rolled back

    @pytest.mark.asyncio
    async def test_ba_edit_of_recap_with_receipt_goes_through(self):
        recap = await sync_to_async(self._make_recap)(with_receipt=True)
        payload = await self._update(self.ba_user, recap)
        assert payload["success"] is True, payload

    @pytest.mark.asyncio
    async def test_staff_edit_entering_spend_after_the_fact_is_not_blocked(self):
        recap = await sync_to_async(self._make_recap)(with_receipt=False)
        payload = await self._update(self.admin, recap)
        assert payload["success"] is True, payload
