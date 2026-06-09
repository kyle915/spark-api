"""
Regression coverage for two recap backend bugs.

B2 — uploads to the "Upload Receipt" field were mis-filed into "Table setup".
    The upload widgets (web + mobile) send a *positional sentinel* for the
    file's role: "1" = sampling photos, "2" = receipts. The old resolver
    treated that sentinel as a raw DB PK; because the default categories are
    seeded per-tenant in the order ["Sampling photos", "Table setup",
    "Receipts"], PK 2 is "Table setup", so a receipt ("2") landed there
    instead of under the tenant's "Receipts". The fix
    (``recaps.mutations._resolve_file_recap_category``) resolves the
    sentinel by the tenant's seeded category NAME, per-tenant.

B6 — per-SKU product samples (and sales performance) were missing from the
    recap PDF for custom recaps. The "Product Samples" / "Sales Performance"
    <section>s lived only inside the legacy branch's summary f-string; the
    custom branch dropped them even though the per-SKU lists are computed
    unconditionally. The fix hoists a shared block that also renders for
    custom recaps.
"""

import pytest

from asgiref.sync import sync_to_async

from events.models import Product, ProductType
from recaps import models as recap_models
from recaps.mutations import (
    _PHOTOS_CATEGORY_NAME,
    _RECEIPTS_CATEGORY_NAME,
    _resolve_file_recap_category,
)
from recaps.pdf import build_recap_pdf_html
from tenants.mutations import DEFAULT_FILE_RECAP_CATEGORIES
from ambassadors.tests.base import AmbassadorsGraphQLTestCase


# The positional sentinels the upload widgets send.
PHOTOS_SENTINEL = "1"
RECEIPTS_SENTINEL = "2"


class _Base(AmbassadorsGraphQLTestCase):
    def _seed_categories(self, tenant):
        """Seed a tenant's default file-recap categories in the SAME order as
        tenants.mutations (so an earlier-seeded tenant gets the lower category
        PKs — the layout that exposed the mis-filing), returning
        {name: FileRecapCategory}."""
        system_user = self.get_system_user()
        by_name = {}
        for name in DEFAULT_FILE_RECAP_CATEGORIES:
            cat = recap_models.FileRecapCategory.objects.create(
                name=name, tenant=tenant, created_by=system_user
            )
            by_name[name] = cat
        return by_name


@pytest.mark.django_db
class TestResolveFileRecapCategoryB2(_Base):
    """B2: positional sentinels resolve by tenant category NAME, not raw PK."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.system_user = self.get_system_user()
        # Seed tenant A FIRST so its category PKs are the low ones
        # (Sampling photos=1, Table setup=2, Receipts=3) — exactly the layout
        # that made the old PK-based resolver mis-file receipts.
        self.tenant_a = self.create_tenant(name="Liquid Death")
        self.cats_a = self._seed_categories(self.tenant_a)
        # Tenant B is seeded second, so its PKs are higher (4/5/6) and never
        # line up with the sentinels "1"/"2".
        self.tenant_b = self.create_tenant(name="Girl Beer")
        self.cats_b = self._seed_categories(self.tenant_b)

    def test_receipts_sentinel_resolves_to_receipts_not_table_setup(self):
        # The headline bug: "2" must be Receipts, NOT "Table setup".
        resolved = _resolve_file_recap_category(
            RECEIPTS_SENTINEL, tenant_id=self.tenant_a.id
        )
        assert resolved is not None
        assert resolved.name == _RECEIPTS_CATEGORY_NAME == "Receipts"
        assert resolved.id == self.cats_a["Receipts"].id
        assert resolved.name != "Table setup"
        assert resolved.tenant_id == self.tenant_a.id

    def test_photos_sentinel_resolves_to_sampling_photos(self):
        resolved = _resolve_file_recap_category(
            PHOTOS_SENTINEL, tenant_id=self.tenant_a.id
        )
        assert resolved is not None
        assert resolved.name == _PHOTOS_CATEGORY_NAME == "Sampling photos"
        assert resolved.id == self.cats_a["Sampling photos"].id

    def test_sentinels_are_per_tenant(self):
        # Each tenant must get its OWN category, even though tenant B's PKs do
        # not include 1/2 at all.
        resolved_a = _resolve_file_recap_category(
            RECEIPTS_SENTINEL, tenant_id=self.tenant_a.id
        )
        resolved_b = _resolve_file_recap_category(
            RECEIPTS_SENTINEL, tenant_id=self.tenant_b.id
        )
        assert resolved_a.id == self.cats_a["Receipts"].id
        assert resolved_b.id == self.cats_b["Receipts"].id
        assert resolved_a.id != resolved_b.id
        assert resolved_b.tenant_id == self.tenant_b.id
        assert resolved_b.name == "Receipts"

    def test_receipts_sentinel_never_lands_on_another_tenants_table_setup(self):
        # Regression guard for the exact failure mode. Tenant A was seeded
        # first, so within tenant A the "Table setup" PK is the lower of A's
        # ids — and a naive PK-based resolver matching the global PK 2 could
        # land a tenant-B receipt onto tenant A's "Table setup". The resolver
        # must instead return tenant B's OWN "Receipts" by name.
        table_setup_a = self.cats_a["Table setup"]
        resolved_b = _resolve_file_recap_category(
            RECEIPTS_SENTINEL, tenant_id=self.tenant_b.id
        )
        assert resolved_b.id != table_setup_a.id
        assert resolved_b.tenant_id == self.tenant_b.id
        assert resolved_b.name == "Receipts"

    def test_explicit_category_pk_still_resolves(self):
        # A real explicit category id (e.g. one the user picked in the
        # category-management UI) must still resolve to that exact row.
        table_setup_a = self.cats_a["Table setup"]
        resolved = _resolve_file_recap_category(
            str(table_setup_a.id), tenant_id=self.tenant_a.id
        )
        assert resolved is not None
        assert resolved.id == table_setup_a.id
        assert resolved.name == "Table setup"

    def test_explicit_high_pk_category_resolves(self):
        # Tenant B's Sampling photos is a high PK (not a sentinel value); an
        # explicit id for it must round-trip.
        photos_b = self.cats_b["Sampling photos"]
        resolved = _resolve_file_recap_category(
            str(photos_b.id), tenant_id=self.tenant_b.id
        )
        assert resolved is not None
        assert resolved.id == photos_b.id

    def test_none_and_garbage_are_graceful(self):
        assert _resolve_file_recap_category(None, tenant_id=self.tenant_a.id) is None
        assert _resolve_file_recap_category("", tenant_id=self.tenant_a.id) is None
        # A non-numeric, non-sentinel value never raises — the recap must not
        # be lost to a stray id.
        assert (
            _resolve_file_recap_category("not-an-id", tenant_id=self.tenant_a.id)
            is None
        )

    def test_sentinel_falls_back_when_tenant_has_no_named_category(self):
        # If a tenant was never seeded the role category, the sentinel degrades
        # to the legacy PK behavior instead of dropping the file. Here tenant C
        # has only a single custom-named category whose PK equals the sentinel.
        tenant_c = self.create_tenant(name="No Seeds Co")
        only_cat = recap_models.FileRecapCategory.objects.create(
            name="Custom Only", tenant=tenant_c, created_by=self.system_user
        )
        resolved = _resolve_file_recap_category(
            str(only_cat.id), tenant_id=tenant_c.id
        )
        assert resolved is not None
        assert resolved.id == only_cat.id


@pytest.mark.django_db
class TestReceiptSentinelKeywordFallback(_Base):
    """A custom-template tenant (e.g. Girl Beer) whose receipt bucket isn't
    named exactly "Receipts" must STILL catch the receipt sentinel "2" by
    keyword — never mis-file into "Table setup" via the PK fallback."""

    def _seed_custom(self, tenant, receipt_name):
        """Seed ["Sampling photos", "Table setup", <receipt_name>] in order so
        "Table setup" gets a low PK — the layout that made the old resolver
        mis-file sentinel "2" into "Table setup"."""
        system_user = self.get_system_user()
        by_name = {}
        for name in ["Sampling photos", "Table setup", receipt_name]:
            by_name[name] = recap_models.FileRecapCategory.objects.create(
                name=name, tenant=tenant, created_by=system_user
            )
        return by_name

    def test_upload_receipt_variant_matches_not_table_setup(self):
        # The Girl Beer report: receipt bucket named "Upload Receipt".
        tenant = self.create_tenant(name="Girl Beer Custom")
        cats = self._seed_custom(tenant, "Upload Receipt")
        resolved = _resolve_file_recap_category(
            RECEIPTS_SENTINEL, tenant_id=tenant.id
        )
        assert resolved is not None
        assert resolved.id == cats["Upload Receipt"].id
        assert resolved.name == "Upload Receipt"
        assert resolved.id != cats["Table setup"].id
        assert resolved.name != "Table setup"

    def test_singular_receipt_variant_matches(self):
        tenant = self.create_tenant(name="Singular Co")
        cats = self._seed_custom(tenant, "Receipt")
        resolved = _resolve_file_recap_category(
            RECEIPTS_SENTINEL, tenant_id=tenant.id
        )
        assert resolved.id == cats["Receipt"].id
        assert resolved.name != "Table setup"

    def test_descriptive_receipt_variant_matches(self):
        tenant = self.create_tenant(name="Descriptive Co")
        cats = self._seed_custom(tenant, "Product Purchase Receipt")
        resolved = _resolve_file_recap_category(
            RECEIPTS_SENTINEL, tenant_id=tenant.id
        )
        assert resolved.id == cats["Product Purchase Receipt"].id
        assert resolved.name != "Table setup"

    def test_exact_receipts_still_wins_over_keyword(self):
        # When BOTH an exact "Receipts" and a keyword-ish "Receipt photos"
        # exist, the exact seeded name takes precedence (fast path).
        tenant = self.create_tenant(name="Both Names Co")
        system_user = self.get_system_user()
        recap_models.FileRecapCategory.objects.create(
            name="Receipt photos", tenant=tenant, created_by=system_user
        )
        exact = recap_models.FileRecapCategory.objects.create(
            name="Receipts", tenant=tenant, created_by=system_user
        )
        resolved = _resolve_file_recap_category(
            RECEIPTS_SENTINEL, tenant_id=tenant.id
        )
        assert resolved.id == exact.id
        assert resolved.name == "Receipts"

    def test_photos_variant_matches_by_keyword(self):
        tenant = self.create_tenant(name="Custom Photos Co")
        system_user = self.get_system_user()
        recap_models.FileRecapCategory.objects.create(
            name="Table setup", tenant=tenant, created_by=system_user
        )
        photos = recap_models.FileRecapCategory.objects.create(
            name="Event Photos", tenant=tenant, created_by=system_user
        )
        resolved = _resolve_file_recap_category(
            PHOTOS_SENTINEL, tenant_id=tenant.id
        )
        assert resolved is not None
        assert resolved.id == photos.id

    def test_no_receipt_category_self_heals_with_own_receipts(self):
        # A tenant with NO receipt-ish category at all: sentinel "2" has no
        # name or keyword match — the resolver now SELF-HEALS by creating the
        # tenant's OWN "Receipts" instead of falling through to the PK path
        # (which could only land the file in another tenant's category).
        tenant = self.create_tenant(name="No Receipt Co")
        system_user = self.get_system_user()
        for name in ["Sampling photos", "Table setup", "Misc"]:
            recap_models.FileRecapCategory.objects.create(
                name=name, tenant=tenant, created_by=system_user
            )
        resolved = _resolve_file_recap_category(
            RECEIPTS_SENTINEL, tenant_id=tenant.id
        )
        assert resolved is not None
        assert resolved.tenant_id == tenant.id
        assert resolved.name == _RECEIPTS_CATEGORY_NAME


@pytest.mark.django_db
class TestSentinelSelfHealAndTenantIsolation(_Base):
    """The Girl Beer production incident, pinned at the resolver level.

    Girl Beer was onboarded outside ``createTenant`` and so had ZERO
    FileRecapCategory rows; receipt sentinel "2" fell through the old PK
    fallback onto the GLOBAL PK-2 "Table setup" — another tenant's category.
    The resolver must now (a) self-heal sentinels by creating the tenant's own
    role category, and (b) never return another tenant's row for an explicit
    category id either.
    """

    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.system_user = self.get_system_user()
        # Seeded-first tenant owns the low PKs — the global rows the old
        # fallback used to leak onto.
        self.tenant_a = self.create_tenant(name="First Seeded Co")
        self.cats_a = self._seed_categories(self.tenant_a)
        # The Girl Beer shape: a tenant with NO categories at all.
        self.tenant_unseeded = self.create_tenant(name="Unseeded Co")

    def test_receipt_sentinel_self_heals_unseeded_tenant(self):
        assert not recap_models.FileRecapCategory.objects.filter(
            tenant_id=self.tenant_unseeded.id
        ).exists()
        resolved = _resolve_file_recap_category(
            RECEIPTS_SENTINEL, tenant_id=self.tenant_unseeded.id
        )
        assert resolved is not None
        assert resolved.tenant_id == self.tenant_unseeded.id
        assert resolved.name == _RECEIPTS_CATEGORY_NAME
        # And NOT the foreign low-PK "Table setup" the old fallback leaked to.
        assert resolved.id != self.cats_a["Table setup"].id

    def test_photos_sentinel_self_heals_unseeded_tenant(self):
        resolved = _resolve_file_recap_category(
            PHOTOS_SENTINEL, tenant_id=self.tenant_unseeded.id
        )
        assert resolved is not None
        assert resolved.tenant_id == self.tenant_unseeded.id
        assert resolved.name == _PHOTOS_CATEGORY_NAME

    def test_self_heal_is_idempotent(self):
        first = _resolve_file_recap_category(
            RECEIPTS_SENTINEL, tenant_id=self.tenant_unseeded.id
        )
        second = _resolve_file_recap_category(
            RECEIPTS_SENTINEL, tenant_id=self.tenant_unseeded.id
        )
        assert first.id == second.id
        assert (
            recap_models.FileRecapCategory.objects.filter(
                tenant_id=self.tenant_unseeded.id,
                name=_RECEIPTS_CATEGORY_NAME,
            ).count()
            == 1
        )

    def test_explicit_foreign_pk_resolves_to_none_not_foreign_row(self):
        # An explicit id pointing at ANOTHER tenant's category, with no
        # same-name row of our own: resolve to None (uncategorized) — never
        # cross-tenant.
        foreign = self.cats_a["Table setup"]
        resolved = _resolve_file_recap_category(
            str(foreign.id), tenant_id=self.tenant_unseeded.id
        )
        assert resolved is None

    def test_explicit_foreign_pk_maps_onto_own_same_name_row(self):
        # But when we DO have a category of the same name, the foreign id maps
        # onto our own row (long-standing behavior, kept).
        own_table_setup = recap_models.FileRecapCategory.objects.create(
            name="Table setup",
            tenant=self.tenant_unseeded,
            created_by=self.system_user,
        )
        foreign = self.cats_a["Table setup"]
        resolved = _resolve_file_recap_category(
            str(foreign.id), tenant_id=self.tenant_unseeded.id
        )
        assert resolved is not None
        assert resolved.id == own_table_setup.id

    def test_tenantless_explicit_pk_keeps_legacy_global_lookup(self):
        # No tenant context (legacy callers): an explicit id still resolves to
        # the raw row — scoping only applies when we know the tenant.
        foreign = self.cats_a["Receipts"]
        resolved = _resolve_file_recap_category(str(foreign.id), tenant_id=None)
        assert resolved is not None
        assert resolved.id == foreign.id


@pytest.mark.django_db(transaction=True)
class TestCustomRecapPdfSamplesB6(_Base):
    """B6: custom-recap PDF HTML includes the per-SKU samples + sales rows."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Liquid Death")
        self.event_type = self.create_event_type(
            name="Sampling", tenant=self.tenant
        )
        self.event = self.create_event(name="Whole Foods", tenant=self.tenant)
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="LD Template",
            event_type=self.event_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )
        self.product_type = ProductType.objects.create(
            name="Beverage", tenant=self.tenant, created_by=self.system_user
        )
        self.product = Product.objects.create(
            name="Mountain Water 16oz",
            product_type=self.product_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )
        self.type_of_good = recap_models.TypeOfGood.objects.create(
            name="Can", tenant=self.tenant, created_by=self.system_user
        )

    def _build_custom_recap(self, *, with_sample=True, with_sale=True):
        custom_recap = recap_models.CustomRecap.objects.create(
            name="LD Custom Recap",
            approved=True,
            event=self.event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        if with_sample:
            recap_models.CustomRecapProductSample.objects.create(
                custom_recap=custom_recap,
                product=self.product,
                quantity=42,
                created_by=self.system_user,
            )
        if with_sale:
            recap_models.CustomRecapSalePerformance.objects.create(
                custom_recap=custom_recap,
                product=self.product,
                type_of_good=self.type_of_good,
                price="3.50",
                created_by=self.system_user,
            )
        return custom_recap

    @pytest.mark.asyncio
    async def test_custom_recap_pdf_includes_per_sku_sample_rows(self):
        custom_recap = await sync_to_async(self._build_custom_recap)()

        html = await sync_to_async(build_recap_pdf_html)(custom_recap, [])

        # The "Product Samples" section must be present for a custom recap...
        assert "Product Samples" in html
        # ...with the actual per-SKU row (product name + quantity).
        assert "Mountain Water 16oz - Qty: 42" in html
        # It must NOT be the empty "N/A" placeholder.
        assert (
            "Mountain Water 16oz" in html.split("Product Samples", 1)[1]
        ), "sample row not rendered under the Product Samples section"

    @pytest.mark.asyncio
    async def test_custom_recap_pdf_includes_sales_performance_rows(self):
        custom_recap = await sync_to_async(self._build_custom_recap)()

        html = await sync_to_async(build_recap_pdf_html)(custom_recap, [])

        assert "Sales Performance" in html
        assert "Mountain Water 16oz (Can) - $3.50" in html

    @pytest.mark.asyncio
    async def test_custom_recap_pdf_samples_section_present_when_empty(self):
        # Even with no samples, the section header should render (with N/A) so
        # the custom layout matches the legacy layout's structure.
        custom_recap = await sync_to_async(self._build_custom_recap)(
            with_sample=False, with_sale=False
        )

        html = await sync_to_async(build_recap_pdf_html)(custom_recap, [])

        assert "Product Samples" in html
        assert "Sales Performance" in html
