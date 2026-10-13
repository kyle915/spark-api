"""Multi-brand activations: one BA form, one recap per brand.

Builds the FAMUS / Alcohol Armor / Wook Water group with the real setup
command, then files a Wook Water + FAMUS co-activation through the walk-up
write path and checks each brand only ever holds its own numbers.
"""

from __future__ import annotations

import pytest
from django.core.management import call_command
from django.utils import timezone
from graphql import GraphQLError

from ambassadors import checkin_web
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.co_brands import co_brand_lines, replace_request_co_brands, resolve_co_brands
from events.models import Event, EventType, Product, RequestCoBrand, RequestType
from recaps import co_activation
from recaps.models import CustomField, CustomRecap, CustomRecapTemplate, FileType
from recaps.queries import CustomRecapQueriesService
from tenants.brand_groups import build_brand_group_compare, user_can_compare
from tenants.models import BrandGroup, BrandGroupAccess, Tenant
from tenants.types import _brand_group_for


def _field(template, name_part: str) -> CustomField:
    return CustomField.objects.get(
        custom_recap_template=template, name__icontains=name_part
    )


class _CoActivationBase(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.admin = self.create_user(
            username="admin-co@igniteproductions.co",
            email="admin-co@igniteproductions.co",
            role=self.roles["spark_admin"],
            is_superuser=True,
        )
        call_command("setup_brand_group", apply=True, skip_images=True, stdout=_Sink())
        self.ww = Tenant.objects.get(slug="wook-water")
        self.famus = Tenant.objects.get(slug="famus-energy")
        self.aa = Tenant.objects.get(slug="alcohol-armor")
        ba_user = self.create_user(
            username="ba-co@test.com", email="ba-co@test.com", role=self.roles["ambassador"]
        )
        self.ba = self.create_ambassador(ba_user)
        FileType.objects.get_or_create(name="image", defaults={"created_by": self.admin})

        retail_ww = EventType.objects.get(tenant=self.ww, name="Retail Sampling")
        self.ww_tpl = CustomRecapTemplate.objects.get(tenant=self.ww, event_type=retail_ww)
        self.fm_tpl = CustomRecapTemplate.objects.get(
            tenant=self.famus, event_type__name="Retail Sampling"
        )
        self.event = self.create_event(
            name="Publix #1201",
            tenant=self.ww,
            address="100 Main St, Charlotte, NC 28202",
            event_type=retail_ww,
            date=timezone.now(),
        )

    def _blob(self, name: str) -> str:
        return f"recap_files/checkin/{self.event.uuid}/{name}.jpg"

    def _submit(self, co_brands):
        values = [
            {"customFieldId": str(_field(self.ww_tpl, "foot traffic").id), "value": "400"},
            {"customFieldId": str(_field(self.ww_tpl, "Sample format").id), "value": "Full can / bottle"},
            {"customFieldId": str(_field(self.ww_tpl, "consumers sampled").id), "value": "60"},
            {"customFieldId": str(_field(self.ww_tpl, "single cans").id), "value": "6"},
            {"customFieldId": str(_field(self.ww_tpl, "packs did").id), "value": "2"},
        ]
        return checkin_web.submit_checkin_recap(
            event=self.event,
            ambassador=self.ba,
            template=self.ww_tpl,
            field_values=values,
            files=[{"blobName": self._blob("host")}],
            total_engagements=None,
            co_brands=co_brands,
        )

    def _famus_entry(self):
        product = Product.objects.filter(tenant=self.famus).order_by("id").first()
        return {
            "tenantId": str(self.famus.id),
            "fieldValues": [
                {"customFieldId": str(_field(self.fm_tpl, "consumers sampled").id), "value": "25"},
                {"customFieldId": str(_field(self.fm_tpl, "single cans").id), "value": "3"},
                # A shared-section id is ignored on a brand entry.
                {"customFieldId": str(_field(self.fm_tpl, "foot traffic").id), "value": "9999"},
            ],
            "productSamples": [{"productId": str(product.id), "quantity": 25}],
            "files": [{"blobName": self._blob("famus")}],
        }

    def _values(self, recap) -> dict[str, str]:
        return {
            v.custom_field.name: v.value
            for v in recap.custom_field_value.select_related("custom_field")
        }



@pytest.mark.django_db(transaction=True)
class TestCoActivation(_CoActivationBase):
    def test_setup_builds_group_programs_and_codes(self):
        group = BrandGroup.objects.get(slug="famus-alcohol-armor-wook-water")
        assert set(group.member_tenants().values_list("slug", flat=True)) == {
            "famus-energy",
            "alcohol-armor",
            "wook-water",
        }
        codes = {t.slug: t.checkin_code for t in (self.ww, self.famus, self.aa)}
        assert codes["wook-water"].startswith("WW-")
        assert codes["famus-energy"].startswith("FM-")
        assert codes["alcohol-armor"].startswith("AA-")
        for tenant in (self.ww, self.famus, self.aa):
            assert sorted(EventType.objects.filter(tenant=tenant).values_list("name", flat=True)) == [
                "Event Activation",
                "Retail Sampling",
            ]
            assert tenant.insights_trend_series == Tenant.TREND_SERIES_SALES

        call_command("setup_brand_group", apply=True, skip_images=True, stdout=_Sink())
        for tenant in (self.ww, self.famus, self.aa):
            tenant.refresh_from_db()
            assert tenant.checkin_code == codes[tenant.slug]

    def test_co_activation_writes_one_recap_per_brand(self):
        host = self._submit([self._famus_entry()])
        host.refresh_from_db()
        slice_recap = CustomRecap.objects.get(co_activation_parent=host)

        assert host.co_activation_key and slice_recap.co_activation_key == host.co_activation_key
        assert slice_recap.tenant_id == self.famus.id
        assert slice_recap.event.tenant_id == self.famus.id
        assert slice_recap.event.co_activation_host_id == self.event.id
        assert slice_recap.approved is False

        host_values = self._values(host)
        slice_values = self._values(slice_recap)
        assert host_values["Total number of consumers sampled"] == "60"
        assert slice_values["Total number of consumers sampled"] == "25"
        assert slice_values["Estimated store foot traffic"] == "400"
        assert host.total_engagements == 60
        assert slice_recap.total_engagements == 25
        assert slice_recap.custom_recap_product_sample.get().quantity == 25
        blobs = set(slice_recap.custom_recap_files.values_list("url", flat=True))
        assert self._blob("famus") in blobs and self._blob("host") in blobs
        assert self._blob("famus") not in set(host.custom_recap_files.values_list("url", flat=True))

        assert co_activation.co_activation_brand_names(host) == ["FAMUS Energy"]
        assert co_activation.co_activation_brand_names(slice_recap) == ["Wook Water"]

    def test_dropping_co_brand_removes_slice_and_copy_event(self):
        host = self._submit([self._famus_entry()])
        copy_event_id = CustomRecap.objects.get(co_activation_parent=host).event_id
        checkin_web.submit_checkin_recap(
            event=self.event,
            ambassador=self.ba,
            template=self.ww_tpl,
            field_values=[
                {"customFieldId": str(_field(self.ww_tpl, "consumers sampled").id), "value": "60"},
            ],
            files=[{"blobName": self._blob("host")}],
            total_engagements=None,
            co_brands=[],
        )
        host.refresh_from_db()
        assert not CustomRecap.objects.filter(co_activation_parent=host).exists()
        assert not Event.objects.filter(id=copy_event_id).exists()
        assert host.co_activation_key is None

    def test_needs_review_lists_host_only(self):
        host = self._submit([self._famus_entry()])
        service = CustomRecapQueriesService()
        famus_review = service.get_filtered_queryset(tenant_id=self.famus.id, approved=False)
        assert not famus_review.exists()
        ww_review = service.get_filtered_queryset(tenant_id=self.ww.id, approved=False)
        assert list(ww_review.values_list("id", flat=True)) == [host.id]
        assert [r.id for r in co_activation.activation_recaps(host)][0] == host.id

    def test_compare_keeps_each_brand_on_its_own_numbers(self):
        host = self._submit([self._famus_entry()])
        CustomRecap.objects.filter(co_activation_key=host.co_activation_key).update(approved=True)
        data = build_brand_group_compare(self.ww.brand_group, None, None)
        rows = {row["name"]: row for row in data["brands"]}
        assert rows["Wook Water"]["consumers_sampled"] == 60
        assert rows["Wook Water"]["units_sold"] == 8
        assert rows["FAMUS Energy"]["consumers_sampled"] == 25
        assert rows["FAMUS Energy"]["units_sold"] == 3
        assert rows["Alcohol Armor"]["recaps"] == 0
        assert data["co_activations"] == 1
        assert data["activations"] == 1

        co_only = build_brand_group_compare(self.ww.brand_group, None, None, True)
        assert {r["name"]: r["recaps"] for r in co_only["brands"]}["Wook Water"] == 1

    def test_compare_access(self):
        client = self.create_user(
            username="client-co@test.com", email="client-co@test.com", role=self.roles["client"]
        )
        group_id = self.ww.brand_group_id
        assert user_can_compare(self.admin.id, group_id, is_admin=True)
        assert not user_can_compare(client.id, group_id, is_admin=False)
        BrandGroupAccess.objects.create(brand_group_id=group_id, user=client)
        assert user_can_compare(client.id, group_id, is_admin=False)

    def test_brand_group_type_and_walkup_options(self):
        group = _brand_group_for(self.ww)
        assert [b.name for b in group.brands] == ["Alcohol Armor", "FAMUS Energy", "Wook Water"]
        assert [b.is_self for b in group.brands] == [False, False, True]

        options = checkin_web.serialize_co_brand_options(self.event)
        assert [b["name"] for b in options["brands"]] == ["Alcohol Armor", "FAMUS Energy"]
        famus = options["brands"][1]
        assert {s["name"] for s in famus["sections"]} == {"Brand Results", "Brand Feedback"}
        assert len(famus["products"]) == Product.objects.filter(tenant=self.famus).count()

    def test_request_co_brand_validation(self):
        request = self.create_request(
            name="Co-activation request",
            date=timezone.now().date(),
            address="100 Main St, Charlotte, NC 28202",
            client=None,
            distributor=None,
            retailer=None,
            request_type=RequestType.objects.get(tenant=self.ww, name="Retail Sampling"),
            tenant=self.ww,
        )
        famus_sku = Product.objects.filter(tenant=self.famus).first()
        aa_sku = Product.objects.filter(tenant=self.aa).first()

        with pytest.raises(GraphQLError):
            resolve_co_brands(self.ww.id, [{"tenantId": self.ww.id, "productIds": []}])
        with pytest.raises(GraphQLError):
            resolve_co_brands(self.ww.id, [{"tenantId": self.famus.id, "productIds": []}])
        with pytest.raises(GraphQLError):
            resolve_co_brands(self.ww.id, [{"tenantId": self.famus.id, "productIds": [aa_sku.id]}])
        outsider = self.create_tenant(name="Outsider")
        with pytest.raises(GraphQLError):
            resolve_co_brands(self.ww.id, [{"tenantId": outsider.id, "productIds": [famus_sku.id]}])

        replace_request_co_brands(
            request,
            [
                {"tenantId": self.famus.id, "productIds": [famus_sku.id]},
                {"tenantId": self.aa.id, "productIds": [aa_sku.id]},
            ],
        )
        assert RequestCoBrand.objects.filter(request=request).count() == 2
        assert co_brand_lines(request) == [
            f"Alcohol Armor: {aa_sku.name}",
            f"FAMUS Energy: {famus_sku.name}",
        ]
        replace_request_co_brands(request, None)
        assert RequestCoBrand.objects.filter(request=request).count() == 2
        replace_request_co_brands(request, [])
        assert not RequestCoBrand.objects.filter(request=request).exists()


APPROVE_CUSTOM_RECAP = """
mutation Approve($id: ID!, $approved: Boolean!) {
  approveCustomRecap(input: { id: $id, approved: $approved }) {
    success
    customRecap { approved coActivation { isPrimary brandNames } }
  }
}
"""

COMPARE = """
query Compare($tenantId: ID!) {
  canCompareBrands(tenantId: $tenantId)
  brandGroupCompare(tenantId: $tenantId) {
    groupName
    activations
    coActivations
    brands { name consumersSampled unitsSold conversionPct }
    notes
  }
}
"""

TENANT_PUBLIC = """
query Public($slug: String!) {
  tenantPublic(requestUrlName: $slug) {
    name
    brandGroup { name brands { name requestUrlName isSelf } }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestCoActivationGraphQL(_CoActivationBase):
    endpoint_path = "/api/v1/graphql/clients"

    @pytest.fixture(autouse=True)
    def schema_setup(self, setup):
        from config.schema_client import schema_clients

        self.schema = schema_clients

    @pytest.mark.asyncio
    async def test_approving_host_approves_and_mails_every_brand(self):
        from asgiref.sync import sync_to_async
        from unittest import mock

        # The off-thread finalize re-enters sync_to_async and would block the
        # async test's executor for its 90s timeout.
        with mock.patch("ambassadors.checkin_web._finalize_recap_offthread"):
            host = await sync_to_async(lambda: self._submit([self._famus_entry()]))()
        with mock.patch("recaps.mutations._kick_recap_approved_notify") as kick:
            kick.return_value = None

            async def _noop(*_a, **_k):
                return None

            kick.side_effect = _noop
            result = await self._execute_mutation_authenticated(
                APPROVE_CUSTOM_RECAP,
                {"id": str(host.id), "approved": True},
                self.admin,
                self.endpoint_path,
            )
        assert result.errors is None, result.errors
        payload = result.data["approveCustomRecap"]
        assert payload["success"] is True
        assert payload["customRecap"]["coActivation"] == {
            "isPrimary": True,
            "brandNames": ["FAMUS Energy"],
        }
        slice_recap = await sync_to_async(CustomRecap.objects.get)(co_activation_parent=host)
        assert slice_recap.approved is True
        mailed = [call.args[0].tenant_id for call in kick.call_args_list]
        assert sorted(mailed) == sorted([self.ww.id, self.famus.id])

    @pytest.mark.asyncio
    async def test_compare_query_is_staff_or_granted_only(self):
        from asgiref.sync import sync_to_async

        client = await sync_to_async(self.create_user)(
            username="client-gql@test.com", email="client-gql@test.com", role=self.roles["client"]
        )
        await sync_to_async(self.create_tenanted_user)(client, self.ww)
        denied = await self._execute_mutation_authenticated(
            COMPARE, {"tenantId": str(self.ww.id)}, client, self.endpoint_path
        )
        assert denied.errors is None, denied.errors
        assert denied.data == {"canCompareBrands": False, "brandGroupCompare": None}

        staff = await self._execute_mutation_authenticated(
            COMPARE, {"tenantId": str(self.ww.id)}, self.admin, self.endpoint_path
        )
        assert staff.errors is None, staff.errors
        assert staff.data["canCompareBrands"] is True
        names = [b["name"] for b in staff.data["brandGroupCompare"]["brands"]]
        assert names == ["Alcohol Armor", "FAMUS Energy", "Wook Water"]

    @pytest.mark.asyncio
    async def test_public_tenant_exposes_brand_group(self):
        result = await self._execute_mutation(
            TENANT_PUBLIC, {"slug": "wook-water"}, self.endpoint_path
        )
        assert result.errors is None, result.errors
        group = result.data["tenantPublic"]["brandGroup"]
        assert [b["requestUrlName"] for b in group["brands"]] == [
            "alcohol-armor",
            "famus-energy",
            "wook-water",
        ]
        assert [b["isSelf"] for b in group["brands"]] == [False, False, True]


class _Sink:
    def write(self, *_args, **_kwargs):
        return None

    def flush(self):
        return None
