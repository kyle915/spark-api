"""GraphQL tests for per-user Settings preferences.

Covers the clients-schema ``myPreferences`` query and ``setMyPreferences``
mutation backed by :class:`tenants.models.UserPreference`:

* defaults are returned when the user has never saved,
* set -> get round-trips the stored prefs,
* prefs are isolated per user,
* a partial update shallow-merges (untouched keys survive),
* unauthenticated reads/writes degrade safely (never raise).
"""

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from config.schema_client import schema_clients
from tenants.models import UserPreference
from tenants.tests.base import BaseGraphQLTestCase


User = get_user_model()

MY_PREFERENCES_QUERY = """
query MyPreferences {
  myPreferences {
    prefs
  }
}
"""

SET_MY_PREFERENCES_MUTATION = """
mutation SetMyPreferences($input: SetMyPreferencesInput!) {
  setMyPreferences(input: $input) {
    success
    message
    preferences {
      prefs
    }
    clientMutationId
  }
}
"""

DEFAULT_PREFS = {
    "timezone": "America/Chicago",
    "currency": "USD ($)",
    "activations": {"retail": True, "onprem": True, "event": True},
}


@pytest.mark.django_db(transaction=True)
class TestUserPreferencesGraphQL(BaseGraphQLTestCase):
    """GraphQL tests for myPreferences / setMyPreferences on the clients schema."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.tenant = self.create_tenant(name="Prefs Tenant")

    async def _create_client_user(self, username: str, email: str) -> User:
        return await sync_to_async(self.create_user)(
            username=username,
            email=email,
            role=self.roles["client"],
            password="password123",
        )

    @pytest.mark.asyncio
    async def test_my_preferences_returns_defaults_when_unset(self):
        """A user who has never saved gets the baseline defaults."""
        user = await self._create_client_user("prefs-defaults", "defaults@test.com")

        result = await self._execute_mutation(
            MY_PREFERENCES_QUERY, {}, self.endpoint_path, user=user
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["myPreferences"]["prefs"] == DEFAULT_PREFS

        # No row was created merely by reading.
        exists = await sync_to_async(
            UserPreference.objects.filter(user=user).exists
        )()
        assert exists is False

    @pytest.mark.asyncio
    async def test_set_then_get_round_trip(self):
        """setMyPreferences persists; a follow-up read returns the saved values."""
        user = await self._create_client_user("prefs-rt", "rt@test.com")

        variables = {
            "input": {
                "prefs": {
                    "timezone": "America/New_York",
                    "currency": "EUR (€)",
                    "activations": {
                        "retail": False,
                        "onprem": True,
                        "event": False,
                    },
                },
                "clientMutationId": "rt-1",
            }
        }

        set_result = await self._execute_mutation(
            SET_MY_PREFERENCES_MUTATION, variables, self.endpoint_path, user=user
        )
        assert set_result.errors is None
        payload = set_result.data["setMyPreferences"]
        assert payload["success"] is True
        assert payload["clientMutationId"] == "rt-1"
        assert payload["preferences"]["prefs"]["timezone"] == "America/New_York"
        assert payload["preferences"]["prefs"]["currency"] == "EUR (€)"
        assert payload["preferences"]["prefs"]["activations"] == {
            "retail": False,
            "onprem": True,
            "event": False,
        }

        # Row persisted.
        pref = await sync_to_async(UserPreference.objects.get)(user=user)
        assert pref.prefs["timezone"] == "America/New_York"

        # Follow-up read reflects the saved values.
        get_result = await self._execute_mutation(
            MY_PREFERENCES_QUERY, {}, self.endpoint_path, user=user
        )
        assert get_result.errors is None
        prefs = get_result.data["myPreferences"]["prefs"]
        assert prefs["timezone"] == "America/New_York"
        assert prefs["currency"] == "EUR (€)"
        assert prefs["activations"]["retail"] is False

    @pytest.mark.asyncio
    async def test_preferences_are_isolated_per_user(self):
        """One user's saved prefs never leak into another user's read."""
        alice = await self._create_client_user("prefs-alice", "alice@test.com")
        bob = await self._create_client_user("prefs-bob", "bob@test.com")

        await self._execute_mutation(
            SET_MY_PREFERENCES_MUTATION,
            {"input": {"prefs": {"timezone": "America/Denver"}}},
            self.endpoint_path,
            user=alice,
        )

        # Bob still sees defaults.
        bob_result = await self._execute_mutation(
            MY_PREFERENCES_QUERY, {}, self.endpoint_path, user=bob
        )
        assert bob_result.data["myPreferences"]["prefs"] == DEFAULT_PREFS

        # Alice sees her own override (merged over defaults).
        alice_result = await self._execute_mutation(
            MY_PREFERENCES_QUERY, {}, self.endpoint_path, user=alice
        )
        alice_prefs = alice_result.data["myPreferences"]["prefs"]
        assert alice_prefs["timezone"] == "America/Denver"
        assert alice_prefs["currency"] == "USD ($)"  # default preserved

    @pytest.mark.asyncio
    async def test_partial_update_merges_over_existing(self):
        """A partial update changes only the supplied keys; the rest survive."""
        user = await self._create_client_user("prefs-partial", "partial@test.com")

        # First save: timezone + currency.
        await self._execute_mutation(
            SET_MY_PREFERENCES_MUTATION,
            {
                "input": {
                    "prefs": {
                        "timezone": "America/Phoenix",
                        "currency": "CAD ($)",
                    }
                }
            },
            self.endpoint_path,
            user=user,
        )

        # Second save: only currency. timezone must remain Phoenix.
        result = await self._execute_mutation(
            SET_MY_PREFERENCES_MUTATION,
            {"input": {"prefs": {"currency": "USD ($)"}}},
            self.endpoint_path,
            user=user,
        )
        prefs = result.data["setMyPreferences"]["preferences"]["prefs"]
        assert prefs["timezone"] == "America/Phoenix"  # untouched key survived
        assert prefs["currency"] == "USD ($)"  # updated
        # Defaults still fill keys never set.
        assert prefs["activations"] == DEFAULT_PREFS["activations"]

        # Confirm at the DB layer the stored blob kept both keys.
        pref = await sync_to_async(UserPreference.objects.get)(user=user)
        assert pref.prefs == {"timezone": "America/Phoenix", "currency": "USD ($)"}

    @pytest.mark.asyncio
    async def test_my_preferences_unauthenticated_returns_defaults(self):
        """An unauthenticated read returns defaults rather than erroring."""
        result = await self._execute_mutation(
            MY_PREFERENCES_QUERY, {}, self.endpoint_path
        )
        assert result.errors is None
        assert result.data["myPreferences"]["prefs"] == DEFAULT_PREFS

    @pytest.mark.asyncio
    async def test_set_my_preferences_unauthenticated_is_safe(self):
        """An unauthenticated write returns success=False, never raises."""
        result = await self._execute_mutation(
            SET_MY_PREFERENCES_MUTATION,
            {"input": {"prefs": {"timezone": "America/New_York"}}},
            self.endpoint_path,
        )
        assert result.errors is None
        payload = result.data["setMyPreferences"]
        assert payload["success"] is False
        assert "not authenticated" in payload["message"].lower()
        assert payload["preferences"] is None
