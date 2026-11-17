"""A person on several brands can open a detail link for any brand they
belong to, even while the app has a different brand selected; nobody else
can."""

from types import SimpleNamespace

import pytest
from graphql import GraphQLError

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.queries import EventQueriesService


class _Service(EventQueriesService):
    """Scoped lookup always misses (current brand != record's brand); the
    unscoped lookup finds a record in `record_tenant`."""

    def __init__(self, user, record_tenant):
        self._user, self._tenant = user, record_tenant

    async def get_user(self, info):
        return self._user

    async def resolve_tenant_id(self, info, **kw):
        return -1

    async def get_record(self, id=None, uuid=None, tenant_id=None):
        if tenant_id is not None and tenant_id != self._tenant.id:
            raise GraphQLError("Record not found.")
        return SimpleNamespace(tenant_id=self._tenant.id, uuid=uuid)


@pytest.mark.django_db(transaction=True)
class TestCrossBrandDetailLinks(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.energy = self.create_tenant(name="Energy Brand")
        self.social = self.create_tenant(name="Social Brand")
        self.stranger = self.create_tenant(name="Someone Else")
        self.client_user = self.create_user(
            username="two-brand-links", email="tbl@test.com", role=self.roles["client"]
        )
        self.create_tenanted_user(self.client_user, self.energy)
        self.create_tenanted_user(self.client_user, self.social)

    @pytest.mark.asyncio
    async def test_record_in_another_brand_they_belong_to_opens(self):
        got = await _Service(self.client_user, self.social).get_detail_record(
            None, uuid="abc"
        )
        assert got.tenant_id == self.social.id

    @pytest.mark.asyncio
    async def test_record_in_a_brand_they_do_not_belong_to_is_not_found(self):
        with pytest.raises(GraphQLError):
            await _Service(self.client_user, self.stranger).get_detail_record(
                None, uuid="abc"
            )

    @pytest.mark.asyncio
    async def test_inactive_membership_does_not_count(self):
        from tenants.models import TenantedUser

        await TenantedUser.objects.filter(
            user=self.client_user, tenant=self.social
        ).aupdate(is_active=False)
        with pytest.raises(GraphQLError):
            await _Service(self.client_user, self.social).get_detail_record(
                None, uuid="abc"
            )
