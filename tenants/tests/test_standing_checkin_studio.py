"""Standing Check-In Studio — write path + GraphQL mutation."""

from __future__ import annotations

import pytest
from asgiref.sync import sync_to_async

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from config.schema_client import schema_clients
from recaps.models import FileRecapCategory
from tenants.models import Tenant, normalize_checkin_resources
from tenants.standing_checkin import (
    ensure_bucket_categories,
    parse_photo_buckets,
    update_standing_checkin_config,
)

MUTATION = """
mutation U($input: UpdateStandingCheckinConfigInput!) {
  updateStandingCheckinConfig(input: $input) {
    success
    message
    checkinCode
    locationMode
    photoBuckets
    changedFields
    resources { label kind url note }
    programs { id name isDefault }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestStandingCheckinHelpers(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"

    def test_parse_flat_and_program_map(self):
        flat = parse_photo_buckets(
            [{"name": "Table Set Up", "helper": "wide", "min": 2}]
        )
        assert flat == [
            {"name": "Table Set Up", "helper": "wide", "min": 2}
        ]
        mapped = parse_photo_buckets(
            {
                "Retail Sampling": [{"name": "Table Set Up"}],
                "Event Activation": [{"name": "Activation Photos", "min": 3}],
            }
        )
        assert mapped["Retail Sampling"][0]["name"] == "Table Set Up"
        assert mapped["Event Activation"][0]["min"] == 3

    def test_parse_rejects_bad_url_shape_via_resources(self):
        tenant = self.create_tenant(name="Studio Brand", slug="studio-brand-helpers")
        with pytest.raises(ValueError, match="rejected"):
            update_standing_checkin_config(
                tenant,
                resources=[{"label": "Guide", "kind": "pdf", "url": "javascript:alert(1)"}],
            )

    def test_reuses_exact_sentinel_categories_without_duplicating(self):
        tenant = self.create_tenant(name="Sentinel Brand", slug="sentinel-brand")
        sampling = FileRecapCategory.objects.create(
            name="Sampling photos", tenant=tenant
        )
        receipts = FileRecapCategory.objects.create(name="Receipts", tenant=tenant)

        ensure_bucket_categories(
            tenant,
            [
                {"name": "Sampling photos"},
                {"name": "Receipts"},
                {"name": "Table Set Up"},
            ],
        )

        names = list(
            FileRecapCategory.objects.filter(tenant_id=tenant.id)
            .order_by("id")
            .values_list("id", "name")
        )
        assert (sampling.id, "Sampling photos") in names
        assert (receipts.id, "Receipts") in names
        # One new row for Table Set Up — not a second Sampling photos / Receipts.
        assert len(names) == 3
        assert {n for _, n in names} == {
            "Sampling photos",
            "Receipts",
            "Table Set Up",
        }

    def test_refuses_to_rename_sentinel_into_another_bucket(self):
        tenant = self.create_tenant(name="Sentinel Rename", slug="sentinel-rename")
        sampling = FileRecapCategory.objects.create(
            name="Sampling photos", tenant=tenant
        )

        ensure_bucket_categories(tenant, [{"name": "Consumer Sampling Pictures"}])

        sampling.refresh_from_db()
        assert sampling.name == "Sampling photos"
        assert FileRecapCategory.objects.filter(tenant_id=tenant.id).count() == 2
        assert FileRecapCategory.objects.filter(
            tenant_id=tenant.id, name="Consumer Sampling Pictures"
        ).exists()


@pytest.mark.django_db(transaction=True)
class TestUpdateStandingCheckinConfig(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.tenant = self.create_tenant(name="Studio Brand", slug="studio-brand")
        self.et_retail = self.create_event_type("Retail Sampling", self.tenant)
        self.et_event = self.create_event_type("Event Activation", self.tenant)
        self.tenant.checkin_code = "ST-KEEPME"
        self.tenant.checkin_location_mode = Tenant.CHECKIN_LOCATION_ADDRESS
        self.tenant.checkin_event_type = self.et_retail
        self.tenant.save(
            update_fields=[
                "checkin_code",
                "checkin_location_mode",
                "checkin_event_type",
            ]
        )
        self.tenant.checkin_event_types.set([self.et_retail])

    def _users(self):
        staff = self.create_user(
            username="studio-staff@igniteproductions.co",
            email="studio-staff@igniteproductions.co",
            role=self.roles["spark_admin"],
            password="password123",
        )
        client = self.create_user(
            username="studio-client@example.com",
            email="studio-client@example.com",
            role=self.roles["client"],
            password="password123",
        )
        self.create_tenanted_user(user=client, tenant=self.tenant)
        return staff, client

    @pytest.mark.asyncio
    async def test_staff_can_edit_buckets_resources_mode_programs_keeps_code(self):
        staff, client = await sync_to_async(self._users)()
        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "locationMode": "market",
                "photoBuckets": [
                    {"name": "Table Set Up"},
                    {
                        "name": "Consumer Sampling Pictures",
                        "helper": "please try to upload 8+",
                        "min": 8,
                    },
                ],
                "resources": [
                    {
                        "label": "BA Guide",
                        "kind": "pdf",
                        "url": "/training/studio/guide.pdf",
                    }
                ],
                "eventTypeIds": [
                    str(self.et_event.id),
                    str(self.et_retail.id),
                ],
            }
        }

        denied = await self._execute_mutation(
            MUTATION, variables, self.endpoint_path, user=client
        )
        assert denied.errors is None, denied.errors
        assert denied.data["updateStandingCheckinConfig"]["success"] is False

        ok = await self._execute_mutation(
            MUTATION, variables, self.endpoint_path, user=staff
        )
        assert ok.errors is None, ok.errors
        out = ok.data["updateStandingCheckinConfig"]
        assert out["success"] is True
        assert out["checkinCode"] == "ST-KEEPME"
        assert out["locationMode"] == "market"
        assert out["resources"][0]["label"] == "BA Guide"
        assert out["programs"][0]["name"] == "Event Activation"
        assert out["programs"][0]["isDefault"] is True

        def _assert_db():
            t = Tenant.objects.get(id=self.tenant.id)
            assert t.checkin_code == "ST-KEEPME"
            assert t.checkin_location_mode == Tenant.CHECKIN_LOCATION_MARKET
            assert t.checkin_event_type_id == self.et_event.id
            assert set(
                t.checkin_event_types.values_list("id", flat=True)
            ) == {self.et_event.id, self.et_retail.id}
            buckets = t.checkin_photo_buckets
            assert isinstance(buckets, list)
            assert buckets[0]["name"] == "Table Set Up"
            assert buckets[1]["min"] == 8
            resources = normalize_checkin_resources(t.checkin_resources)
            assert resources[0]["url"] == "/training/studio/guide.pdf"
            assert t.checkin_training_url == "/training/studio/guide.pdf"
            names = set(
                FileRecapCategory.objects.filter(tenant_id=t.id).values_list(
                    "name", flat=True
                )
            )
            assert "Table Set Up" in names
            assert "Consumer Sampling Pictures" in names

        await sync_to_async(_assert_db)()

    @pytest.mark.asyncio
    async def test_mutation_never_accepts_code_change(self):
        """Even if a client somehow smuggles a code, the field is not on input —
        and the write path never touches checkin_code."""
        staff, _ = await sync_to_async(self._users)()

        def _write_direct():
            t = Tenant.objects.get(id=self.tenant.id)
            result = update_standing_checkin_config(
                t, location_mode="address", photo_buckets_provided=False
            )
            assert result["checkin_code"] == "ST-KEEPME"
            t.refresh_from_db()
            assert t.checkin_code == "ST-KEEPME"

        await sync_to_async(_write_direct)()

        again = await self._execute_mutation(
            MUTATION,
            {
                "input": {
                    "tenantId": str(self.tenant.id),
                    "locationMode": "address",
                }
            },
            self.endpoint_path,
            user=staff,
        )
        assert again.data["updateStandingCheckinConfig"]["checkinCode"] == "ST-KEEPME"
