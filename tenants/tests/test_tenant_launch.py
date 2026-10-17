import pytest
from django.contrib.auth import get_user_model

from events.models import EventType, Product
from recaps.models import (
    CustomField,
    CustomRecapFieldType,
    CustomRecapTemplate,
    FileRecapCategory,
    RecapSection,
)
from tenants.launch import (
    LaunchError,
    build_preview,
    import_products,
    launch_tenant,
    parse_catalog_csv,
    parse_shopify_collection_json,
    validate_slug,
)
from tenants.models import Tenant
from tenants.tests.base import BaseGraphQLTestCase

User = get_user_model()


def _creator(db):
    from tenants.tests.base import ensure_role

    role = ensure_role("Spark Admin", slug="spark-admin")
    user, _ = User.objects.get_or_create(
        username="launch-admin",
        defaults={"email": "launch-admin@test.com", "role": role, "is_staff": True},
    )
    return user


def _donor(creator):
    donor = Tenant.objects.create(
        name="Donor Drinks",
        slug="donor-drinks",
        request_url_name="donor-drinks",
        created_by=creator,
        checkin_location_mode=Tenant.CHECKIN_LOCATION_MARKET,
        checkin_photo_buckets={
            "Event Activation": [
                {"name": "Activation Set Up"},
                {"name": "Consumer Sampling Pictures", "helper": "8+", "min": 8},
            ]
        },
        checkin_code="DON-ABC123",
    )
    event_type = EventType.objects.create(
        name="Event Activation",
        slug="event-activation",
        tenant=donor,
        created_by=creator,
        is_default=True,
    )
    template = CustomRecapTemplate.objects.create(
        name="Donor Event Recap",
        tenant=donor,
        event_type=event_type,
        created_by=creator,
        product_samples=True,
        layout={"v": 1},
    )
    number = CustomRecapFieldType.objects.create(name="number", created_by=creator)
    text = CustomRecapFieldType.objects.create(name="longtext", created_by=creator)
    multi = CustomRecapFieldType.objects.create(name="multiselect", created_by=creator)
    section = RecapSection.objects.create(
        tenant=donor, name="Engagement", order=0, created_by=creator
    )
    CustomField.objects.create(
        custom_recap_template=template,
        recap_section=section,
        custom_field_type=number,
        name="How many Donor Drinks fans showed up?",
        required=True,
        order=0,
        created_by=creator,
    )
    CustomField.objects.create(
        custom_recap_template=template,
        recap_section=section,
        custom_field_type=multi,
        name="Products Sampled",
        required=True,
        order=1,
        options=["Donor Cola"],
        created_by=creator,
    )
    CustomField.objects.create(
        custom_recap_template=template,
        recap_section=section,
        custom_field_type=text,
        name="Notes",
        required=False,
        order=2,
        created_by=creator,
    )
    return donor, template


@pytest.mark.django_db
class TestSlugValidation:
    def test_valid_slug(self):
        assert validate_slug(" Drink-Lyft ") == "drink-lyft"

    @pytest.mark.parametrize(
        "bad", ["", "drink lyft", "drink--lyft", "-drink", "drink_lyft", "drink.lyft"]
    )
    def test_invalid_slugs(self, bad):
        with pytest.raises(LaunchError):
            validate_slug(bad)

    def test_exact_slug_resolution_never_substring(self, db):
        creator = _creator(db)
        Tenant.objects.create(
            name="Drink Lyft", slug="drink-lyft", created_by=creator
        )
        from tenants.launch import resolve_tenant_by_exact_slug

        assert resolve_tenant_by_exact_slug("drink") is None
        assert resolve_tenant_by_exact_slug("drink-lyft") is not None
        assert resolve_tenant_by_exact_slug("DRINK-LYFT") is not None


@pytest.mark.django_db
class TestCatalogImport:
    def test_csv_parse_and_idempotent_import(self, db):
        creator = _creator(db)
        tenant = Tenant.objects.create(name="Acme", slug="acme", created_by=creator)
        rows, errors = parse_catalog_csv(
            "name,sku,image_url\nAcme Cola,ACME-1,https://x/1.jpg\nAcme Lime,ACME-2,\n"
        )
        assert errors == []
        first = import_products(tenant=tenant, creator=creator, rows=rows)
        assert first["created"] == 2
        second = import_products(tenant=tenant, creator=creator, rows=rows)
        assert second["created"] == 0
        assert second["reused"] == 2
        assert Product.objects.filter(tenant=tenant).count() == 2
        # Same SKU under a renamed product still reuses the row.
        renamed, _ = parse_catalog_csv("name,sku\nAcme Cola Zero,ACME-1\n")
        third = import_products(tenant=tenant, creator=creator, rows=renamed)
        assert third["created"] == 0
        assert Product.objects.filter(tenant=tenant).count() == 2

    def test_shopify_json_parse(self):
        rows, errors = parse_shopify_collection_json(
            '{"products": [{"title": "Cola 12-Pack", "product_type": "Cola",'
            ' "variants": [{"sku": "COLA-12"}], "image": {"src": "https://cdn/x.jpg"}}]}'
        )
        assert errors == []
        assert rows == [
            {
                "name": "Cola 12-Pack",
                "sku": "COLA-12",
                "image_url": "https://cdn/x.jpg",
                "product_type": "Cola",
            }
        ]

    def test_long_names_skipped_not_truncated(self, db):
        creator = _creator(db)
        tenant = Tenant.objects.create(name="Acme", slug="acme2", created_by=creator)
        result = import_products(
            tenant=tenant,
            creator=creator,
            rows=[{"name": "X" * 60, "sku": "LONG-1", "image_url": ""}],
        )
        assert result["created"] == 0
        assert result["warnings"]


@pytest.mark.django_db
class TestLaunchTenant:
    def test_clone_does_not_mutate_donor_and_code_minted_once(self, db):
        creator = _creator(db)
        donor, template = _donor(creator)
        donor_field_count = CustomField.objects.filter(
            custom_recap_template=template
        ).count()
        donor_options = list(
            CustomField.objects.filter(
                custom_recap_template=template, name="Products Sampled"
            ).values_list("options", flat=True)
        )

        result = launch_tenant(
            creator=creator,
            name="Acme Drinks",
            slug="acme-drinks",
            donor_template_id=template.id,
            label_swaps=[("Donor Drinks", "Acme Drinks")],
            catalog_rows=[
                {"name": "Acme Cola", "sku": "ACME-1", "image_url": ""},
            ],
            code_prefix="ACME",
        )
        tenant = result["tenant"]
        assert result["tenant_created"] is True
        assert tenant.slug == "acme-drinks"
        assert tenant.request_url_name == "acme-drinks"
        assert tenant.auto_approve_requests is False
        assert result["checkin_code"].startswith("ACME-")
        assert result["checkin_url"] == (
            f"https://client.igniteproductions.co/checkin/{result['checkin_code']}"
        )
        assert result["spark_form_url"] == (
            "https://client.igniteproductions.co/spark-form/acme-drinks"
        )
        cloned = result["template"]
        assert cloned is not None and cloned.tenant_id == tenant.id
        names = list(
            CustomField.objects.filter(custom_recap_template=cloned)
            .order_by("order")
            .values_list("name", flat=True)
        )
        assert "How many Acme Drinks fans showed up?" in names
        # Donor untouched.
        assert CustomField.objects.filter(
            custom_recap_template=template
        ).count() == donor_field_count
        assert list(
            CustomField.objects.filter(
                custom_recap_template=template, name="Products Sampled"
            ).values_list("options", flat=True)
        ) == donor_options
        assert Tenant.objects.get(id=donor.id).checkin_code == "DON-ABC123"
        # Photo buckets + location mode cloned.
        tenant.refresh_from_db()
        assert tenant.checkin_location_mode == Tenant.CHECKIN_LOCATION_MARKET
        assert tenant.checkin_photo_buckets == {
            "Event Activation": [
                {"name": "Activation Set Up"},
                {"name": "Consumer Sampling Pictures", "helper": "8+", "min": 8},
            ]
        }
        assert FileRecapCategory.objects.filter(
            tenant=tenant, name="Activation Set Up"
        ).exists()
        assert tenant.checkin_event_type_id == cloned.event_type_id

        # Re-launch with the same slug resumes and never remints.
        again = launch_tenant(
            creator=creator,
            name="Acme Drinks",
            slug="acme-drinks",
            donor_template_id=template.id,
            catalog_rows=[
                {"name": "Acme Cola", "sku": "ACME-1", "image_url": ""},
            ],
            code_prefix="ACME",
        )
        assert again["tenant"].id == tenant.id
        assert again["tenant_created"] is False
        assert again["checkin_code"] == result["checkin_code"]
        assert again["code_minted"] is False
        assert Product.objects.filter(tenant=tenant).count() == 1
        assert CustomRecapTemplate.objects.filter(tenant=tenant).count() == 1

    def test_preview_writes_nothing(self, db):
        creator = _creator(db)
        donor, template = _donor(creator)
        before = Tenant.objects.count()
        preview = build_preview(
            name="Preview Drinks",
            slug="preview-drinks",
            donor_template=template,
            catalog_rows=[{"name": "P Cola", "sku": "P-1", "image_url": ""}],
        )
        assert Tenant.objects.count() == before
        assert preview["spark_form_url"].endswith("/spark-form/preview-drinks")
        assert preview["sections"][0]["fields"][0]["name"].startswith("How many")
        assert preview["photo_buckets"][0]["name"] == "Activation Set Up"


@pytest.mark.django_db(transaction=True)
class TestLaunchGraphQL(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"

    async def _admin(self):
        from asgiref.sync import sync_to_async

        return await sync_to_async(self.create_user)(
            username="launchgqladmin",
            email="launchgqladmin@test.com",
            role=self.roles["spark_admin"],
            password="password123",
            is_staff=True,
        )

    @pytest.mark.asyncio
    async def test_preview_then_launch_and_non_admin_blocked(self):
        from asgiref.sync import sync_to_async

        admin = await self._admin()
        donor, template = await sync_to_async(_donor)(admin)
        mutation = """
        mutation Launch($input: TenantLaunchInput!) {
          launchTenant(input: $input) {
            success message checkinCode sparkFormUrl tenant { id name slug }
          }
        }
        """
        variables = {
            "input": {
                "name": "GQL Drinks",
                "slug": "gql-drinks",
                "donorTemplateId": str(template.id),
                "catalogCsv": "name,sku\nGQL Cola,GQL-1\n",
                "codePrefix": "GQL",
            }
        }
        result = await self._execute_mutation(
            mutation, variables, self.endpoint_path, user=admin
        )
        assert result.errors is None, result.errors
        payload = result.data["launchTenant"]
        assert payload["success"] is True, payload
        assert payload["checkinCode"].startswith("GQL-")
        assert payload["sparkFormUrl"].endswith("/spark-form/gql-drinks")

        preview_mutation = """
        mutation Preview($input: TenantLaunchInput!) {
          previewTenantLaunch(input: $input) {
            success preview { slug existingTenant { name } sections { name } }
          }
        }
        """
        preview = await self._execute_mutation(
            preview_mutation, variables, self.endpoint_path, user=admin
        )
        assert preview.data["previewTenantLaunch"]["success"] is True
        assert preview.data["previewTenantLaunch"]["preview"]["existingTenant"]["name"] == "GQL Drinks"

        ba = await sync_to_async(self.create_user)(
            username="launchba",
            email="launchba@test.com",
            role=self.roles["ambassador"],
            password="password123",
        )
        blocked = await self._execute_mutation(
            mutation,
            {"input": {"name": "Nope", "slug": "nope-drinks"}},
            self.endpoint_path,
            user=ba,
        )
        assert blocked.data["launchTenant"]["success"] is False
