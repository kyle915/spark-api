"""
Access-scoping sweep 4, events/mutations.py: the remaining
``is_spark_schema_request`` admin gates (plus the ``user.role.is_spark_admin``
reads OR-ed in next to them) now use the DB-backed
``is_admin_request`` (``_is_admin_access(*resolve_request_user_access(user))``).

Two edge cases, run through the real spark schema:
  * a removed admin (IGNITE_ADMIN_EXCLUDE) whose request user still carries
    the spark-admin role used to pass as an admin and skip the tenant gate;
  * an admin by the DB rule whose role slug isn't spark-admin (is_staff
    here) used to be treated as a client and refused outside their tenants.

Sites: prepare_create (createEvent), deleteProduct,
requestBatchTemplateDownloadUrl, importRequestsBatch, and the request
store-manager service (set_user_and_tenant + _ensure_tenant_access).
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone as dt_tz
from unittest.mock import patch

import pytest
from asgiref.sync import sync_to_async

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models

WHEN = datetime(2026, 12, 5, 17, 0, tzinfo=dt_tz.utc)

CREATE_EVENT = """
mutation C($input: CreateEventInput!) {
  createEvent(input: $input) { success message }
}
"""

DELETE_PRODUCT = """
mutation D($input: DeleteProductInput!) {
  deleteProduct(input: $input) { success message }
}
"""

TEMPLATE = """
mutation T($input: RequestBatchTemplateInput!) {
  requestBatchTemplateDownloadUrl(input: $input) { success message fileName }
}
"""

IMPORT = """
mutation I($input: ImportRequestsBatchInput!) {
  importRequestsBatch(input: $input) { success message }
}
"""

CREATE_MANAGER = """
mutation M($input: CreateRequestStoreManagerInput!) {
  createRequestStoreManager(input: $input) { success message }
}
"""


@contextmanager
def _excluded(email):
    from utils.graphql.permissions import IGNITE_ADMIN_EXCLUDE

    IGNITE_ADMIN_EXCLUDE.add(email)
    try:
        yield
    finally:
        IGNITE_ADMIN_EXCLUDE.discard(email)


@pytest.mark.django_db(transaction=True)
class TestAdminRequestEventsGates(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_spark import schema_spark

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()
        self.tenant_a = self.create_tenant(name="Ev Brand A")
        self.tenant_b = self.create_tenant(name="Ev Brand B")

        self.removed_email = "ev-removed@igniteproductions.co"
        self.removed = self.create_user(
            username="ev-removed", email=self.removed_email,
            role=self.roles["spark_admin"],
        )
        self.create_tenanted_user(self.removed, self.tenant_a)
        # Admin by the DB rule (is_staff), role slug "client", member of
        # nothing.
        self.staff = self.create_user(
            username="ev-staff", email="ev-staff@test.com",
            role=self.roles["client"], is_staff=True,
        )
        self.admin = self.create_user(
            username="ev-admin", email="ev-admin@test.com",
            role=self.roles["spark_admin"],
        )

        self.create_event_status("Approved", self.tenant_b, slug="approved")
        self.event_type_b = self.create_event_type("Demo", self.tenant_b)
        product_type = event_models.ProductType.objects.create(
            name="Can", tenant=self.tenant_b, created_by=self.sys,
        )
        self.product_b = event_models.Product.objects.create(
            name="B product", product_type=product_type, tenant=self.tenant_b,
            created_by=self.sys,
        )
        location = event_models.Location.objects.create(
            name="Austin", code="AUS", zip="78701", created_by=self.sys,
        )
        retailer_b = self.create_retailer(
            name="B store", address="1 Main", store_contact="x",
            location=location, tenant=self.tenant_b,
        )
        self.request_b = self.create_request(
            name="B request", date=WHEN, address="1 Main",
            client=self.create_client(
                name="B client", email="b@client.test", tenant=self.tenant_b
            ),
            distributor=self.create_distributor(
                name="B dist", email="b@dist.test", location=location,
                tenant=self.tenant_b,
            ),
            retailer=retailer_b,
            request_type=self.create_request_type(name="Demo", tenant=self.tenant_b),
            tenant=self.tenant_b,
            start_time=WHEN, end_time=WHEN + timedelta(hours=4),
        )

    async def _run(self, query, variables, user):
        result = await self._execute_mutation(
            query, variables, self.endpoint_path, user=user
        )
        assert result.errors is None, result.errors
        return next(iter(result.data.values()))

    # ── prepare_create (createEvent) ─────────────────────────────────

    def _event_input(self, **extra):
        data = {
            "name": "Sweep event",
            "eventTypeId": str(self.event_type_b.id),
            "date": WHEN.isoformat(),
            "address": "1 Main",
            "notes": "",
            "startTime": WHEN.isoformat(),
            "endTime": (WHEN + timedelta(hours=4)).isoformat(),
        }
        data.update(extra)
        return {"input": data}

    async def _event_count(self):
        return await sync_to_async(
            event_models.Event.objects.filter(
                tenant=self.tenant_b, name="Sweep event"
            ).count
        )()

    @pytest.mark.asyncio
    async def test_create_event_removed_admin_refused_in_other_tenant(self):
        with _excluded(self.removed_email):
            payload = await self._run(
                CREATE_EVENT,
                self._event_input(tenantId=str(self.tenant_b.id)),
                self.removed,
            )
        assert payload["success"] is False, payload
        assert await self._event_count() == 0

    @pytest.mark.asyncio
    async def test_create_event_on_request_removed_admin_refused(self):
        with _excluded(self.removed_email):
            payload = await self._run(
                CREATE_EVENT,
                self._event_input(requestId=str(self.request_b.id)),
                self.removed,
            )
        assert payload["success"] is False, payload
        assert await self._event_count() == 0

    @pytest.mark.asyncio
    async def test_create_event_staff_admin_allowed_in_any_tenant(self):
        payload = await self._run(
            CREATE_EVENT,
            self._event_input(tenantId=str(self.tenant_b.id)),
            self.staff,
        )
        assert payload["success"] is True, payload
        assert await self._event_count() == 1

    # ── deleteProduct ────────────────────────────────────────────────

    async def _product_exists(self):
        return await sync_to_async(
            event_models.Product.objects.filter(id=self.product_b.id).exists
        )()

    @pytest.mark.asyncio
    async def test_delete_product_removed_admin_refused(self):
        with _excluded(self.removed_email):
            payload = await self._run(
                DELETE_PRODUCT, {"input": {"id": str(self.product_b.id)}},
                self.removed,
            )
        assert payload["success"] is False, payload
        assert await self._product_exists()

    @pytest.mark.asyncio
    async def test_delete_product_staff_admin_allowed(self):
        payload = await self._run(
            DELETE_PRODUCT, {"input": {"id": str(self.product_b.id)}}, self.staff
        )
        assert payload["success"] is True, payload
        assert not await self._product_exists()

    # ── requestBatchTemplateDownloadUrl ──────────────────────────────

    @pytest.mark.asyncio
    async def test_batch_template_removed_admin_refused_for_other_tenant(self):
        with _excluded(self.removed_email):
            payload = await self._run(
                TEMPLATE, {"input": {"tenantId": str(self.tenant_b.id)}},
                self.removed,
            )
        assert payload["success"] is False, payload
        assert payload["fileName"] is None

    @pytest.mark.asyncio
    async def test_batch_template_staff_admin_allowed_for_any_tenant(self):
        payload = await self._run(
            TEMPLATE, {"input": {"tenantId": str(self.tenant_b.id)}}, self.staff
        )
        assert payload["success"] is True, payload

    # ── importRequestsBatch ──────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_import_batch_removed_admin_refused_before_reading_file(self):
        with (
            _excluded(self.removed_email),
            patch("events.mutations.download_blob_bytes") as download,
        ):
            payload = await self._run(
                IMPORT,
                {"input": {
                    "tenantId": str(self.tenant_b.id),
                    "file": "https://storage.googleapis.com/b/requests/x.xlsx",
                }},
                self.removed,
            )
        assert payload["success"] is False, payload
        download.assert_not_called()

    @pytest.mark.asyncio
    async def test_import_batch_staff_admin_reaches_file_step(self):
        with patch(
            "events.mutations.download_blob_bytes", return_value=b""
        ) as download:
            payload = await self._run(
                IMPORT,
                {"input": {
                    "tenantId": str(self.tenant_b.id),
                    "file": "https://storage.googleapis.com/b/requests/x.xlsx",
                }},
                self.staff,
            )
        # Past the tenant gate: it reads the (empty, stubbed) file.
        download.assert_called_once()
        assert payload["message"] == "Batch file not found.", payload

    # ── request store manager ────────────────────────────────────────

    async def _manager_count(self):
        return await sync_to_async(
            event_models.RequestStoreManager.objects.filter(
                request=self.request_b
            ).count
        )()

    @pytest.mark.asyncio
    async def test_store_manager_removed_admin_refused_on_other_tenant(self):
        with _excluded(self.removed_email):
            payload = await self._run(
                CREATE_MANAGER,
                {"input": {
                    "name": "Mgr", "phone": "555",
                    "requestId": str(self.request_b.id),
                }},
                self.removed,
            )
        assert payload["success"] is False, payload
        assert await self._manager_count() == 0

    @pytest.mark.asyncio
    async def test_store_manager_staff_admin_allowed_on_any_tenant(self):
        payload = await self._run(
            CREATE_MANAGER,
            {"input": {
                "name": "Mgr", "phone": "555",
                "requestId": str(self.request_b.id),
            }},
            self.staff,
        )
        assert payload["success"] is True, payload
        assert await self._manager_count() == 1

    @pytest.mark.asyncio
    async def test_store_manager_spark_admin_unchanged(self):
        payload = await self._run(
            CREATE_MANAGER,
            {"input": {
                "name": "Mgr", "phone": "555",
                "requestId": str(self.request_b.id),
            }},
            self.admin,
        )
        assert payload["success"] is True, payload
        assert await self._manager_count() == 1
