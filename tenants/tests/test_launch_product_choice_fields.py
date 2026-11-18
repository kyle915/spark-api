"""The launch wizard writes the NEW catalog into every product-choice field.

Reproduces CoHo Imports (tenant 38): launched from Mark Anthony Brands'
Long Drink template (layout filtered Products Sampled to "The Finnish Long
Drink") with a 9-SKU Jinro catalog, the cloned "Products Sampled" kept the
donor's nine "The Finnish Long Drink — …" options. Every test drives the real
``launchTenant`` / ``previewTenantLaunch`` mutations.
"""

import pytest
from asgiref.sync import sync_to_async

from events.models import EventType, Product, ProductType
from recaps.models import (
    CustomField,
    CustomRecapFieldType,
    CustomRecapTemplate,
    RecapSection,
)
from recaps.products_sampled import PRODUCTS_SAMPLED_TYPES_KEY, resolve_field_options
from tenants.models import Tenant
from tenants.tests.base import BaseGraphQLTestCase

DONOR_LINE = "The Finnish Long Drink"
DONOR_SKUS = [
    "Original",
    "Zero Sugar",
    "Cranberry",
    "Strong",
    "Blood Orange",
    "Pineapple",
    "Peach",
    "Grapefruit",
    "Lemon Zero",
]
JINRO_SKUS = [
    "Jinro Chamisul Fresh",
    "Jinro Chamisul Original",
    "Jinro Is Back",
    "Jinro Grapefruit",
    "Jinro Green Grape",
    "Jinro Plum",
    "Jinro Strawberry",
    "Jinro Peach",
    "Jinro Apple Mango",
]
JINRO_CSV = "name,sku\n" + "\n".join(
    f"{name},JNR-{i}" for i, name in enumerate(JINRO_SKUS, start=1)
)
JINRO_LABELS = sorted(f"Products — {n}" for n in JINRO_SKUS)

LAUNCH = """
mutation Launch($input: TenantLaunchInput!) {
  launchTenant(input: $input) {
    success message warnings templateId catalogTotal tenant { id name }
  }
}
"""
PREVIEW = """
mutation Preview($input: TenantLaunchInput!) {
  previewTenantLaunch(input: $input) {
    success message warnings
    preview {
      sections { name fields { name type options fromCatalog } }
      productFields { name options donorOptions }
      swapChanges { kind field before after }
      warnings
    }
  }
}
"""


def _field_type(name, creator):
    row = CustomRecapFieldType.objects.filter(name=name).first()
    return row or CustomRecapFieldType.objects.create(name=name, created_by=creator)


def _mab_donor(creator):
    """Mark Anthony Brands with its Long Drink catalog + Total Wine form."""
    donor = Tenant.objects.create(
        name="Mark Anthony Brands",
        slug="mark-anthony-brands",
        request_url_name="mark-anthony-brands",
        created_by=creator,
        checkin_code="MAB-DONOR1",
    )
    line = ProductType.objects.create(name=DONOR_LINE, tenant=donor, created_by=creator)
    for name in DONOR_SKUS:
        Product.objects.create(
            name=name, product_type=line, tenant=donor, created_by=creator
        )
    event_type = EventType.objects.create(
        name="Retail Sampling",
        slug="retail-sampling",
        tenant=donor,
        created_by=creator,
        is_default=True,
    )
    template = CustomRecapTemplate.objects.create(
        name="Total Wine Long Drink Recap",
        tenant=donor,
        event_type=event_type,
        created_by=creator,
        product_samples=True,
        layout={
            PRODUCTS_SAMPLED_TYPES_KEY: [DONOR_LINE],
            "fieldConditions": {
                "4oz pours served": {
                    "field": "Sample format",
                    "includes": "4oz pour",
                    "required": True,
                },
                "Long Drink display photo": {
                    "field": "Long Drink display?",
                    "includes": "Long Drink endcap",
                    "required": False,
                },
            },
        },
    )
    multi = _field_type("multiselect", creator)
    select = _field_type("select", creator)
    number = _field_type("number", creator)
    section = RecapSection.objects.create(
        tenant=donor, name="Sampling", order=0, created_by=creator
    )
    specs = [
        ("Products Sampled", multi, [f"{DONOR_LINE} — {n}" for n in DONOR_SKUS]),
        ("Sample format", select, ["Full can", "4oz pour"]),
        ("4oz pours served", number, []),
        # Not named Products Sampled, but its options ARE donor products.
        ("Favorite flavor", select, ["Original", "Cranberry", "Other"]),
        ("Long Drink display?", select, ["Long Drink endcap", "Cooler"]),
    ]
    for order, (name, ftype, options) in enumerate(specs):
        CustomField.objects.create(
            custom_recap_template=template,
            recap_section=section,
            custom_field_type=ftype,
            name=name,
            required=False,
            order=order,
            options=options,
            created_by=creator,
        )
    return donor, template


def _cloned(tenant_slug: str) -> dict:
    tenant = Tenant.objects.get(slug=tenant_slug)
    template = CustomRecapTemplate.objects.get(tenant=tenant)
    fields = {
        f.name: f
        for f in CustomField.objects.filter(custom_recap_template=template)
        .select_related("custom_recap_template", "custom_field_type")
    }
    return {"tenant": tenant, "template": template, "fields": fields}


@pytest.mark.django_db(transaction=True)
class TestLaunchProductChoiceFields(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"

    async def _admin(self):
        return await sync_to_async(self.create_user)(
            username="cohoadmin",
            email="cohoadmin@test.com",
            role=self.roles["spark_admin"],
            password="password123",
            is_staff=True,
        )

    def _input(self, template, slug="coho-imports", catalog=JINRO_CSV):
        payload = {
            "name": "CoHo Imports",
            "slug": slug,
            "donorTemplateId": str(template.id),
            "labelSwaps": ["Long Drink=Jinro"],
            "codePrefix": "CHI",
        }
        if catalog:
            payload["catalogCsv"] = catalog
        return {"input": payload}

    @pytest.mark.asyncio
    async def test_coho_clone_gets_only_the_new_catalog(self):
        admin = await self._admin()
        donor, template = await sync_to_async(_mab_donor)(admin)
        donor_before = await sync_to_async(
            lambda: {
                f.name: list(f.options or [])
                for f in CustomField.objects.filter(custom_recap_template=template)
            }
        )()

        result = await self._execute_mutation(
            LAUNCH, self._input(template), self.endpoint_path, user=admin
        )
        assert result.errors is None, result.errors
        payload = result.data["launchTenant"]
        assert payload["success"] is True, payload
        assert payload["catalogTotal"] == 9
        # The donor's product-line filter could not apply to Jinro.
        assert any("filter was removed" in w for w in payload["warnings"])

        def _check():
            got = _cloned("coho-imports")
            fields = got["fields"]
            sampled = fields["Products Sampled"]
            # Stored options (what the web check-in serves) = new catalog only.
            assert sorted(sampled.options) == JINRO_LABELS
            assert not any("Finnish" in o for o in sampled.options)
            # Read-time resolver (GraphQL pills) agrees.
            assert sorted(resolve_field_options(sampled)) == JINRO_LABELS
            # A choice field detected by its donor-product options.
            assert sorted(fields["Favorite flavor"].options) == sorted(
                JINRO_LABELS + ["Other"]
            )
            # Non-brand options untouched; brand options swapped.
            assert fields["Sample format"].options == ["Full can", "4oz pour"]
            assert fields["Jinro display?"].options == ["Jinro endcap", "Cooler"]
            layout = got["template"].layout
            assert PRODUCTS_SAMPLED_TYPES_KEY not in layout
            conditions = layout["fieldConditions"]
            assert conditions["4oz pours served"]["includes"] == "4oz pour"
            assert conditions["Jinro display photo"] == {
                "field": "Jinro display?",
                "includes": "Jinro endcap",
                "required": False,
            }

            from ambassadors.checkin_web import _template_sections

            sections, _ = _template_sections(got["template"])
            served = {
                f["name"]: f["options"] for s in sections for f in s["fields"]
            }
            assert sorted(served["Products Sampled"]) == JINRO_LABELS

            # Donor untouched.
            after = {
                f.name: list(f.options or [])
                for f in CustomField.objects.filter(custom_recap_template=template)
            }
            assert after == donor_before
            donor_tpl = CustomRecapTemplate.objects.get(id=template.id)
            assert donor_tpl.layout[PRODUCTS_SAMPLED_TYPES_KEY] == [DONOR_LINE]

        await sync_to_async(_check)()

    @pytest.mark.asyncio
    async def test_empty_catalog_clears_and_warns(self):
        admin = await self._admin()
        _donor, template = await sync_to_async(_mab_donor)(admin)

        result = await self._execute_mutation(
            LAUNCH,
            self._input(template, slug="empty-launch", catalog=None),
            self.endpoint_path,
            user=admin,
        )
        assert result.errors is None, result.errors
        payload = result.data["launchTenant"]
        assert payload["success"] is True, payload
        assert payload["catalogTotal"] == 0
        assert any(
            "Products Sampled" in w and "no products to offer" in w
            for w in payload["warnings"]
        ), payload["warnings"]

        def _check():
            fields = _cloned("empty-launch")["fields"]
            assert fields["Products Sampled"].options == []
            # Donor products dropped; the non-product extra survives.
            assert fields["Favorite flavor"].options == ["Other"]
            assert resolve_field_options(fields["Products Sampled"]) == []

        await sync_to_async(_check)()

    @pytest.mark.asyncio
    async def test_preview_shows_products_and_swaps_without_writing(self):
        admin = await self._admin()
        _donor, template = await sync_to_async(_mab_donor)(admin)
        tenants_before = await sync_to_async(Tenant.objects.count)()

        result = await self._execute_mutation(
            PREVIEW, self._input(template), self.endpoint_path, user=admin
        )
        assert result.errors is None, result.errors
        payload = result.data["previewTenantLaunch"]
        assert payload["success"] is True, payload
        preview = payload["preview"]

        by_name = {pf["name"]: pf for pf in preview["productFields"]}
        assert set(by_name) == {"Products Sampled", "Favorite flavor"}
        assert sorted(by_name["Products Sampled"]["options"]) == JINRO_LABELS
        assert len(by_name["Products Sampled"]["donorOptions"]) == 9

        fields = {f["name"]: f for s in preview["sections"] for f in s["fields"]}
        assert fields["Products Sampled"]["fromCatalog"] is True
        assert fields["Sample format"]["fromCatalog"] is False
        assert fields["Sample format"]["options"] == ["Full can", "4oz pour"]
        assert fields["Jinro display?"]["options"] == ["Jinro endcap", "Cooler"]

        swaps = {(c["kind"], c["before"], c["after"]) for c in preview["swapChanges"]}
        assert ("option", "Long Drink endcap", "Jinro endcap") in swaps
        assert ("field", "Long Drink display?", "Jinro display?") in swaps
        assert ("template", "Total Wine Long Drink Recap", "Total Wine Jinro Recap") in swaps
        assert not any(c["before"] == "Full can" for c in preview["swapChanges"])
        assert any("filter was removed" in w for w in preview["warnings"])

        assert await sync_to_async(Tenant.objects.count)() == tenants_before

    @pytest.mark.asyncio
    async def test_relaunch_repairs_a_clone_made_before_the_fix(self):
        """A pre-fix clone (donor products + donor line filter) is repaired by
        re-running launch with the same slug."""
        admin = await self._admin()
        _donor, template = await sync_to_async(_mab_donor)(admin)

        def _prefix_clone():
            from tenants.launch import create_tenant_with_defaults, import_products

            tenant = create_tenant_with_defaults(
                name="CoHo Imports", slug="coho-imports", creator=admin
            )
            event_type, _ = EventType.objects.get_or_create(
                tenant=tenant,
                name="Retail Sampling",
                defaults={"slug": "retail-sampling", "created_by": admin},
            )
            clone = CustomRecapTemplate.objects.create(
                name="Total Wine Jinro Recap",
                tenant=tenant,
                event_type=event_type,
                created_by=admin,
                layout={PRODUCTS_SAMPLED_TYPES_KEY: [DONOR_LINE]},
            )
            section = RecapSection.objects.create(
                tenant=tenant, name="Sampling", order=0, created_by=admin
            )
            CustomField.objects.create(
                custom_recap_template=clone,
                recap_section=section,
                custom_field_type=_field_type("multiselect", admin),
                name="Products Sampled",
                order=0,
                options=[f"{DONOR_LINE} — {n}" for n in DONOR_SKUS],
                created_by=admin,
            )
            from tenants.launch import parse_catalog_csv

            rows, _ = parse_catalog_csv(JINRO_CSV)
            import_products(tenant=tenant, creator=admin, rows=rows)

        await sync_to_async(_prefix_clone)()

        result = await self._execute_mutation(
            LAUNCH, self._input(template), self.endpoint_path, user=admin
        )
        assert result.data["launchTenant"]["success"] is True, result.data

        def _check():
            got = _cloned("coho-imports")
            assert sorted(got["fields"]["Products Sampled"].options) == JINRO_LABELS
            assert PRODUCTS_SAMPLED_TYPES_KEY not in got["template"].layout
            assert Product.objects.filter(tenant=got["tenant"]).count() == 9

        await sync_to_async(_check)()
