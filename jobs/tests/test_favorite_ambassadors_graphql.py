"""GraphQL tests for tenant-scoped Favorites (TenantFavoriteAmbassador).

Covers the clients-schema ``favoriteAmbassadors`` query, the
``addFavoriteAmbassador`` / ``removeFavoriteAmbassador`` mutations, and the
``Ambassador.isFavorited`` field. The favorites ops are backed by the existing
:class:`jobs.models.TenantFavoriteAmbassador` model (one row per
``(tenant, ambassador)``).

Scenarios:

* add -> the BA appears in ``favoriteAmbassadors`` (with denormalized
  name/email), remove -> it's gone,
* add is idempotent (a second add succeeds and does not create a duplicate row),
* favorites are isolated per tenant,
* a client can neither read, add to, nor remove from another tenant's roster
  (the ``tenantId`` argument is overridden to their own tenant),
* an admin (spark-admin) may target any tenant via ``tenantId``,
* ``Ambassador.isFavorited`` reflects the caller's own tenant's roster.
"""

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from config.schema_client import schema_clients
from jobs.models import TenantFavoriteAmbassador
from jobs.tests.base import JobsGraphQLTestCase


User = get_user_model()


FAVORITE_AMBASSADORS_QUERY = """
query FavoriteAmbassadors($tenantId: ID) {
  favoriteAmbassadors(tenantId: $tenantId) {
    uuid
    tenantId
    ambassadorId
    ambassadorUuid
    firstName
    lastName
    email
    note
    createdAt
  }
}
"""

ADD_FAVORITE_MUTATION = """
mutation AddFavorite($input: AddFavoriteAmbassadorInput!) {
  addFavoriteAmbassador(input: $input) {
    success
    message
    clientMutationId
  }
}
"""

REMOVE_FAVORITE_MUTATION = """
mutation RemoveFavorite($input: RemoveFavoriteAmbassadorInput!) {
  removeFavoriteAmbassador(input: $input) {
    success
    message
    clientMutationId
  }
}
"""

# isFavorited lives on the Ambassador type; the single-ambassador query is the
# simplest deterministic probe (it returns one Ambassador by id).
AMBASSADOR_QUERY = """
query Ambassador($id: ID!) {
  ambassador(id: $id) {
    id
    uuid
    isFavorited
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestFavoriteAmbassadorsGraphQL(JobsGraphQLTestCase):
    """GraphQL tests for tenant-scoped favorite-BA roster ops."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.tenant = self.create_tenant(name="Favs Tenant")
        self.other_tenant = self.create_tenant(name="Other Tenant")

    # -- fixtures ------------------------------------------------------------

    async def _client_user_for(self, username, email, tenant) -> User:
        """A client-role user who is a member of ``tenant``."""
        user = await sync_to_async(self.create_user)(
            username=username,
            email=email,
            role=self.roles["client"],
            password="password123",
        )
        await sync_to_async(self.create_tenanted_user)(user=user, tenant=tenant)
        return user

    async def _admin_user(self, username, email) -> User:
        return await sync_to_async(self.create_user)(
            username=username,
            email=email,
            role=self.roles["spark_admin"],
            password="password123",
        )

    async def _ambassador(self, username, email, first="Bay", last="Area"):
        """Create a BA (its own user + Ambassador row)."""
        ba_user = await sync_to_async(self.create_user)(
            username=username,
            email=email,
            role=self.roles["ambassador"],
            password="password123",
            first_name=first,
            last_name=last,
        )
        return await sync_to_async(self.create_ambassador)(user=ba_user)

    # -- tests ---------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_add_then_appears_in_query(self):
        """addFavoriteAmbassador persists; favoriteAmbassadors returns the BA
        with denormalized name/email."""
        user = await self._client_user_for("fav-add", "add@test.com", self.tenant)
        amb = await self._ambassador("ba-add", "ba-add@test.com", "Nik", "Tesla")

        add_result = await self._execute_mutation(
            ADD_FAVORITE_MUTATION,
            {
                "input": {
                    "ambassadorId": str(amb.id),
                    "tenantId": str(self.tenant.id),
                    "note": "Great at demos",
                    "clientMutationId": "add-1",
                }
            },
            self.endpoint_path,
            user=user,
        )
        assert add_result.errors is None
        payload = add_result.data["addFavoriteAmbassador"]
        assert payload["success"] is True
        assert payload["clientMutationId"] == "add-1"

        # Row persisted, scoped to the tenant, with note + added_by set.
        fav = await sync_to_async(
            TenantFavoriteAmbassador.objects.select_related("ambassador").get
        )(tenant_id=self.tenant.id, ambassador_id=amb.id)
        assert fav.note == "Great at demos"
        assert fav.added_by_id == user.id

        # Query reflects it, with denormalized BA fields.
        list_result = await self._execute_mutation(
            FAVORITE_AMBASSADORS_QUERY,
            {"tenantId": str(self.tenant.id)},
            self.endpoint_path,
            user=user,
        )
        assert list_result.errors is None
        favs = list_result.data["favoriteAmbassadors"]
        assert len(favs) == 1
        row = favs[0]
        assert row["ambassadorId"] == str(amb.id)
        assert row["ambassadorUuid"] == str(amb.uuid)
        assert row["firstName"] == "Nik"
        assert row["lastName"] == "Tesla"
        assert row["email"] == "ba-add@test.com"
        assert row["note"] == "Great at demos"
        assert row["tenantId"] == str(self.tenant.id)

    @pytest.mark.asyncio
    async def test_remove_makes_it_gone(self):
        """removeFavoriteAmbassador deletes the row; the query no longer
        returns the BA."""
        user = await self._client_user_for("fav-rm", "rm@test.com", self.tenant)
        amb = await self._ambassador("ba-rm", "ba-rm@test.com")
        await sync_to_async(TenantFavoriteAmbassador.objects.create)(
            tenant=self.tenant, ambassador=amb, added_by=user
        )

        remove_result = await self._execute_mutation(
            REMOVE_FAVORITE_MUTATION,
            {
                "input": {
                    "ambassadorId": str(amb.id),
                    "tenantId": str(self.tenant.id),
                    "clientMutationId": "rm-1",
                }
            },
            self.endpoint_path,
            user=user,
        )
        assert remove_result.errors is None
        payload = remove_result.data["removeFavoriteAmbassador"]
        assert payload["success"] is True
        assert payload["clientMutationId"] == "rm-1"

        exists = await sync_to_async(
            TenantFavoriteAmbassador.objects.filter(
                tenant_id=self.tenant.id, ambassador_id=amb.id
            ).exists
        )()
        assert exists is False

        list_result = await self._execute_mutation(
            FAVORITE_AMBASSADORS_QUERY,
            {"tenantId": str(self.tenant.id)},
            self.endpoint_path,
            user=user,
        )
        assert list_result.data["favoriteAmbassadors"] == []

    @pytest.mark.asyncio
    async def test_add_is_idempotent(self):
        """A second addFavoriteAmbassador succeeds and creates no duplicate row
        (unique on tenant+ambassador)."""
        user = await self._client_user_for("fav-idem", "idem@test.com", self.tenant)
        amb = await self._ambassador("ba-idem", "ba-idem@test.com")

        variables = {
            "input": {
                "ambassadorId": str(amb.id),
                "tenantId": str(self.tenant.id),
            }
        }
        first = await self._execute_mutation(
            ADD_FAVORITE_MUTATION, variables, self.endpoint_path, user=user
        )
        second = await self._execute_mutation(
            ADD_FAVORITE_MUTATION, variables, self.endpoint_path, user=user
        )
        assert first.data["addFavoriteAmbassador"]["success"] is True
        assert second.data["addFavoriteAmbassador"]["success"] is True

        count = await sync_to_async(
            TenantFavoriteAmbassador.objects.filter(
                tenant_id=self.tenant.id, ambassador_id=amb.id
            ).count
        )()
        assert count == 1

    @pytest.mark.asyncio
    async def test_remove_missing_is_safe(self):
        """Removing a BA that isn't favorited never raises; returns
        success=False."""
        user = await self._client_user_for("fav-rmx", "rmx@test.com", self.tenant)
        amb = await self._ambassador("ba-rmx", "ba-rmx@test.com")

        result = await self._execute_mutation(
            REMOVE_FAVORITE_MUTATION,
            {"input": {"ambassadorId": str(amb.id), "tenantId": str(self.tenant.id)}},
            self.endpoint_path,
            user=user,
        )
        assert result.errors is None
        assert result.data["removeFavoriteAmbassador"]["success"] is False

    @pytest.mark.asyncio
    async def test_favorites_are_isolated_per_tenant(self):
        """A client sees only their own tenant's favorites."""
        user = await self._client_user_for("fav-iso", "iso@test.com", self.tenant)
        mine = await self._ambassador("ba-mine", "mine@test.com", "Mine", "BA")
        theirs = await self._ambassador("ba-theirs", "theirs@test.com", "Their", "BA")
        await sync_to_async(TenantFavoriteAmbassador.objects.create)(
            tenant=self.tenant, ambassador=mine, added_by=user
        )
        await sync_to_async(TenantFavoriteAmbassador.objects.create)(
            tenant=self.other_tenant, ambassador=theirs
        )

        result = await self._execute_mutation(
            FAVORITE_AMBASSADORS_QUERY,
            {"tenantId": str(self.tenant.id)},
            self.endpoint_path,
            user=user,
        )
        favs = result.data["favoriteAmbassadors"]
        assert len(favs) == 1
        assert favs[0]["ambassadorId"] == str(mine.id)

    @pytest.mark.asyncio
    async def test_client_cannot_list_other_tenant_favorites(self):
        """A client passing another tenant's id is pinned to their own tenant."""
        user = await self._client_user_for("fav-xlist", "xlist@test.com", self.tenant)
        theirs = await self._ambassador("ba-xlist", "xlist-ba@test.com")
        await sync_to_async(TenantFavoriteAmbassador.objects.create)(
            tenant=self.other_tenant, ambassador=theirs
        )

        # Ask for the OTHER tenant's roster -> scoped down to caller's tenant
        # (empty, since the caller's tenant has no favorites).
        result = await self._execute_mutation(
            FAVORITE_AMBASSADORS_QUERY,
            {"tenantId": str(self.other_tenant.id)},
            self.endpoint_path,
            user=user,
        )
        assert result.errors is None
        assert result.data["favoriteAmbassadors"] == []

    @pytest.mark.asyncio
    async def test_client_cannot_add_to_other_tenant(self):
        """A client's addFavoriteAmbassador with another tenant's id writes to
        their OWN tenant, never the target."""
        user = await self._client_user_for("fav-xadd", "xadd@test.com", self.tenant)
        amb = await self._ambassador("ba-xadd", "xadd-ba@test.com")

        result = await self._execute_mutation(
            ADD_FAVORITE_MUTATION,
            {
                "input": {
                    "ambassadorId": str(amb.id),
                    "tenantId": str(self.other_tenant.id),
                }
            },
            self.endpoint_path,
            user=user,
        )
        assert result.errors is None
        assert result.data["addFavoriteAmbassador"]["success"] is True

        # Nothing landed on the targeted (other) tenant...
        leaked = await sync_to_async(
            TenantFavoriteAmbassador.objects.filter(
                tenant_id=self.other_tenant.id, ambassador_id=amb.id
            ).exists
        )()
        assert leaked is False
        # ...it was pinned to the caller's own tenant instead.
        own = await sync_to_async(
            TenantFavoriteAmbassador.objects.filter(
                tenant_id=self.tenant.id, ambassador_id=amb.id
            ).exists
        )()
        assert own is True

    @pytest.mark.asyncio
    async def test_client_cannot_remove_from_other_tenant(self):
        """A client's removeFavoriteAmbassador can't delete another tenant's
        favorite (it's scoped to the caller's own tenant)."""
        user = await self._client_user_for("fav-xrm", "xrm@test.com", self.tenant)
        amb = await self._ambassador("ba-xrm", "xrm-ba@test.com")
        await sync_to_async(TenantFavoriteAmbassador.objects.create)(
            tenant=self.other_tenant, ambassador=amb
        )

        result = await self._execute_mutation(
            REMOVE_FAVORITE_MUTATION,
            {
                "input": {
                    "ambassadorId": str(amb.id),
                    "tenantId": str(self.other_tenant.id),
                }
            },
            self.endpoint_path,
            user=user,
        )
        assert result.errors is None
        # Nothing in the caller's tenant to remove -> success=False...
        assert result.data["removeFavoriteAmbassador"]["success"] is False
        # ...and the OTHER tenant's favorite survived.
        survived = await sync_to_async(
            TenantFavoriteAmbassador.objects.filter(
                tenant_id=self.other_tenant.id, ambassador_id=amb.id
            ).exists
        )()
        assert survived is True

    @pytest.mark.asyncio
    async def test_admin_can_target_any_tenant(self):
        """A spark-admin may add to and list any tenant's roster via tenantId."""
        admin = await self._admin_user("fav-admin", "admin@test.com")
        amb = await self._ambassador("ba-admin", "admin-ba@test.com", "Adm", "Tgt")

        add_result = await self._execute_mutation(
            ADD_FAVORITE_MUTATION,
            {
                "input": {
                    "ambassadorId": str(amb.id),
                    "tenantId": str(self.other_tenant.id),
                }
            },
            self.endpoint_path,
            user=admin,
        )
        assert add_result.errors is None
        assert add_result.data["addFavoriteAmbassador"]["success"] is True

        landed = await sync_to_async(
            TenantFavoriteAmbassador.objects.filter(
                tenant_id=self.other_tenant.id, ambassador_id=amb.id
            ).exists
        )()
        assert landed is True

        list_result = await self._execute_mutation(
            FAVORITE_AMBASSADORS_QUERY,
            {"tenantId": str(self.other_tenant.id)},
            self.endpoint_path,
            user=admin,
        )
        favs = list_result.data["favoriteAmbassadors"]
        assert len(favs) == 1
        assert favs[0]["ambassadorId"] == str(amb.id)

    @pytest.mark.asyncio
    async def test_is_favorited_true_for_starred_ba(self):
        """Ambassador.isFavorited is True for a BA on the caller's own tenant
        roster."""
        user = await self._client_user_for("fav-flag", "flag@test.com", self.tenant)
        starred = await self._ambassador("ba-star", "star@test.com", "Star", "BA")
        await sync_to_async(TenantFavoriteAmbassador.objects.create)(
            tenant=self.tenant, ambassador=starred, added_by=user
        )

        result = await self._execute_mutation(
            AMBASSADOR_QUERY,
            {"id": str(starred.id)},
            self.endpoint_path,
            user=user,
        )
        assert result.errors is None
        assert result.data["ambassador"]["isFavorited"] is True

    @pytest.mark.asyncio
    async def test_is_favorited_false_for_unstarred_ba(self):
        """Ambassador.isFavorited is False for a BA not on the caller's roster."""
        user = await self._client_user_for("fav-flag2", "flag2@test.com", self.tenant)
        plain = await self._ambassador("ba-plain", "plain@test.com", "Plain", "BA")

        result = await self._execute_mutation(
            AMBASSADOR_QUERY,
            {"id": str(plain.id)},
            self.endpoint_path,
            user=user,
        )
        assert result.errors is None
        assert result.data["ambassador"]["isFavorited"] is False

    @pytest.mark.asyncio
    async def test_is_favorited_scoped_to_caller_tenant(self):
        """A BA favorited by ANOTHER tenant is not isFavorited for this caller."""
        user = await self._client_user_for("fav-flag3", "flag3@test.com", self.tenant)
        amb = await self._ambassador("ba-other-fav", "otherfav@test.com")
        # Starred by the OTHER tenant, not the caller's.
        await sync_to_async(TenantFavoriteAmbassador.objects.create)(
            tenant=self.other_tenant, ambassador=amb
        )

        result = await self._execute_mutation(
            AMBASSADOR_QUERY,
            {"id": str(amb.id)},
            self.endpoint_path,
            user=user,
        )
        assert result.errors is None
        assert result.data["ambassador"]["isFavorited"] is False
