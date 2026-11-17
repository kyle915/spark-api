"""A user on several brands: lookups that don't name a tenant use the brand
the app says is selected (X-Spark-Tenant), but only one they belong to.

Before this, a client on two brands (e.g. FAMUS Energy + FAMUS Social) got
"not a member of this tenant" / "Event not found" on every single-record
page, because user.tenant can't choose between two memberships.
"""

from types import SimpleNamespace

import pytest
from graphql import GraphQLError

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from utils.graphql.mixins import SparkGraphQLMixin, selected_tenant_uuid


def _info(user, header=None):
    headers = {"X-Spark-Tenant": header} if header is not None else {}
    return SimpleNamespace(
        context=SimpleNamespace(request=SimpleNamespace(user=user, headers=headers))
    )


@pytest.mark.django_db(transaction=True)
class TestSelectedTenantHeader(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.energy = self.create_tenant(name="Energy Brand")
        self.social = self.create_tenant(name="Social Brand")
        self.stranger = self.create_tenant(name="Someone Else")
        self.two_brand = self.create_user(
            username="two-brand", email="two-brand@test.com", role=self.roles["client"]
        )
        self.create_tenanted_user(self.two_brand, self.energy)
        self.create_tenanted_user(self.two_brand, self.social)
        self.one_brand = self.create_user(
            username="one-brand", email="one-brand@test.com", role=self.roles["client"]
        )
        self.create_tenanted_user(self.one_brand, self.energy)
        self.mixin = SparkGraphQLMixin()

    @pytest.mark.asyncio
    async def test_two_brand_user_gets_selected_brand(self):
        for tenant in (self.energy, self.social):
            got = await self.mixin.get_user_tenant(
                _info(self.two_brand, str(tenant.uuid)), user=self.two_brand
            )
            assert got.id == tenant.id

    @pytest.mark.asyncio
    async def test_two_brand_user_without_header_still_ambiguous(self):
        with pytest.raises(GraphQLError):
            await self.mixin.get_user_tenant(_info(self.two_brand), user=self.two_brand)

    @pytest.mark.asyncio
    async def test_header_for_a_brand_they_do_not_belong_to_is_ignored(self):
        # Two-brand user: header names a stranger -> no tenant (never the stranger).
        with pytest.raises(GraphQLError):
            await self.mixin.get_user_tenant(
                _info(self.two_brand, str(self.stranger.uuid)), user=self.two_brand
            )
        # One-brand user: header ignored, they get their own brand.
        got = await self.mixin.get_user_tenant(
            _info(self.one_brand, str(self.stranger.uuid)), user=self.one_brand
        )
        assert got.id == self.energy.id

    @pytest.mark.asyncio
    async def test_explicit_tenant_wins_over_header(self):
        got = await self.mixin.get_user_tenant(
            _info(self.two_brand, str(self.social.uuid)),
            tenant_id=self.energy.id,
            user=self.two_brand,
        )
        assert got.id == self.energy.id

    def test_malformed_header_is_ignored(self):
        assert selected_tenant_uuid(_info(self.two_brand, "not-a-uuid")) is None
        assert selected_tenant_uuid(_info(self.two_brand, "")) is None
        assert selected_tenant_uuid(SimpleNamespace(context=None)) is None
