"""Coverage for the smart-staffing SUGGESTIONS backend:

* :func:`ambassadors.staffing_suggestions.suggest_ambassadors_for_event` — the
  transparent weighted best-fit ranker that scores the tenant's BAs for one
  event from the signals that exist (rating, brand experience, availability,
  favorited, proximity), and
* the ``staffingSuggestions(eventId, limit)`` GraphQL query on the clients
  schema — the tenant-scoped, never-raises shell around it.

The helper tests assert the weight-driven ranking (a higher-rated /
brand-experienced / favorited / available / closer BA ranks above one
without), per-tenant scoping (a BA's OTHER-tenant brand experience + ratings
must NOT leak into THIS tenant's score), the missing-signal degrade (no
coordinates / no availability data simply drop out, no crash, no fabricated
fields), and that already-rostered BAs are excluded. The GraphQL tests assert
the ``StaffingSuggestion`` shape, tenant scoping (clients pinned to their own
tenant's events, admins any), and the deny/empty posture for an out-of-scope
or unknown event (never an error).

Mirrors the fixture style of test_tenant_ba_leaderboard.py and
test_my_pending_offers.py.
"""

from datetime import datetime, timedelta, timezone as _tz

import pytest
from asgiref.sync import sync_to_async

from ambassadors import models as amb_models
from ambassadors.staffing_suggestions import suggest_ambassadors_for_event
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from availability.models import AmbassadorAvailability
from jobs.models import TenantFavoriteAmbassador


SUGGESTIONS_QUERY = """
query Suggest($eventId: ID!, $limit: Int) {
  staffingSuggestions(eventId: $eventId, limit: $limit) {
    baId
    name
    score
    avgRating
    gigsForBrand
    isFavorited
    isAvailable
    distanceMi
    reasons
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestStaffingSuggestions(AmbassadorsGraphQLTestCase):
    """Weighted best-fit BA suggestions for one event, scoped to a tenant."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()

        self.tenant = self.create_tenant(name="Girl Beer")
        self.other_tenant = self.create_tenant(name="Liquid Death")

        self.spark_admin = self.create_user(
            username="admin-sugg",
            email="admin-sugg@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="client-sugg",
            email="client-sugg@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        # A client of the OTHER tenant, to prove cross-tenant denial.
        self.other_client = self.create_user(
            username="client-other-sugg",
            email="client-other-sugg@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.other_client, self.other_tenant)

        # The event we're staffing — a future shift with a known window so the
        # availability matcher has a date/time to work with.
        self.event = self._event(name="WF Burbank", tenant=self.tenant)
        # Its weekday, for building a covering recurring availability slot.
        self._event_weekday = self.event.start_time.weekday()
        self._event_start_t = self.event.start_time.time()
        self._event_end_t = self.event.end_time.time()

        # --- BAs (all members of OUR tenant) --------------------------------
        # high: top rating, brand experience, favorited, available, has coords.
        self.high = self._ba("high", "High", "Fit", coordinates=[34.18, -118.31])
        # low: no ratings, no brand experience, not favorited, no coords.
        self.low = self._ba("low", "Low", "Fit")
        self._member(self.high)
        self._member(self.low)

    # -- fixture helpers ----------------------------------------------------

    def _event(self, name, tenant, days_ahead: int = 7):
        start = datetime.now(_tz.utc) + timedelta(days=days_ahead)
        return self.create_event(
            name=name,
            tenant=tenant,
            date=start,
            start_time=start,
            end_time=start + timedelta(hours=4),
            coordinates=[34.20, -118.34],  # Burbank-ish
        )

    def _ba(self, username, first, last, coordinates=None):
        user = self.create_user(
            username=username,
            email=f"{username}@test.com",
            role=self.roles["ambassador"],
            first_name=first,
            last_name=last,
        )
        return self.create_ambassador(user=user, coordinates=coordinates or [])

    def _member(self, ambassador, tenant=None):
        """Make the BA's user an active member of the tenant (candidate pool)."""
        return self.create_tenanted_user(ambassador.user, tenant or self.tenant)

    def _shift(self, ambassador, event, tenant=None):
        return amb_models.AmbassadorEvent.objects.create(
            ambassador=ambassador,
            event=event,
            tenant=tenant or self.tenant,
            created_by=self.system_user,
            updated_by=self.system_user,
        )

    def _rating(self, ambassador, event, score, tenant=None, by_client=False):
        return amb_models.AmbassadorRating.objects.create(
            ambassador=ambassador,
            event=event,
            tenant=tenant or self.tenant,
            score=score,
            by_client=by_client,
            created_by=self.system_user,
            updated_by=self.system_user,
        )

    def _favorite(self, ambassador, tenant=None):
        return TenantFavoriteAmbassador.objects.create(
            tenant=tenant or self.tenant,
            ambassador=ambassador,
            added_by=self.system_user,
        )

    def _recurring_availability(self, ambassador, weekday, start_t, end_t):
        return AmbassadorAvailability.objects.create(
            ambassador=ambassador,
            weekday=weekday,
            is_recurring=True,
            start_time=start_t,
            end_time=end_t,
            created_by=self.system_user,
        )

    def _by_id(self, rows):
        return {int(r["ba_id"]): r for r in rows}

    # -- helper-level tests -------------------------------------------------

    def test_high_fit_outranks_low_fit(self):
        # Give `high` every signal; `low` gets none.
        self._rating(self.high, self.event, 5)
        self._shift(self.high, self._event(name="past1", tenant=self.tenant))
        self._shift(self.high, self._event(name="past2", tenant=self.tenant))
        self._favorite(self.high)
        self._recurring_availability(
            self.high, self._event_weekday,
            self._event_start_t, self._event_end_t,
        )

        rows = suggest_ambassadors_for_event(self.event.id, self.tenant.id)
        order = [r["ba_id"] for r in rows]
        assert order[0] == self.high.id
        by = self._by_id(rows)
        # high scores strictly above low, and well above zero.
        assert by[self.high.id]["score"] > by[self.low.id]["score"]
        assert by[self.high.id]["score"] > 50

    def test_reasons_reflect_present_signals(self):
        self._rating(self.high, self.event, 5)  # 5.0 -> "5★"
        self._shift(self.high, self._event(name="p1", tenant=self.tenant))
        self._favorite(self.high)
        self._recurring_availability(
            self.high, self._event_weekday,
            self._event_start_t, self._event_end_t,
        )
        by = self._by_id(suggest_ambassadors_for_event(self.event.id, self.tenant.id))
        reasons = by[self.high.id]["reasons"]
        assert "5★" in reasons
        assert "available" in reasons
        assert "favorited" in reasons
        assert "1 gig for this brand" in reasons
        # high has coordinates AND the event has coordinates -> a distance reason.
        assert any("mi away" in r for r in reasons)

    def test_rating_alone_lifts_score(self):
        # Two otherwise-identical BAs; only one is rated -> rated ranks first.
        self._rating(self.high, self.event, 5)
        by = self._by_id(suggest_ambassadors_for_event(self.event.id, self.tenant.id))
        assert by[self.high.id]["score"] > by[self.low.id]["score"]
        assert by[self.high.id]["avg_rating"] == 5.0
        assert by[self.low.id]["avg_rating"] is None

    def test_brand_experience_alone_lifts_score(self):
        self._shift(self.high, self._event(name="p1", tenant=self.tenant))
        self._shift(self.high, self._event(name="p2", tenant=self.tenant))
        by = self._by_id(suggest_ambassadors_for_event(self.event.id, self.tenant.id))
        assert by[self.high.id]["gigs_for_brand"] == 2
        assert by[self.low.id]["gigs_for_brand"] == 0
        assert by[self.high.id]["score"] > by[self.low.id]["score"]

    def test_favorited_alone_lifts_score(self):
        self._favorite(self.high)
        by = self._by_id(suggest_ambassadors_for_event(self.event.id, self.tenant.id))
        assert by[self.high.id]["is_favorited"] is True
        assert by[self.low.id]["is_favorited"] is False
        assert by[self.high.id]["score"] > by[self.low.id]["score"]

    def test_available_alone_lifts_score(self):
        # Only `high` has a covering availability slot; both see is_available
        # as a real bool (the event has a date/time window).
        self._recurring_availability(
            self.high, self._event_weekday,
            self._event_start_t, self._event_end_t,
        )
        by = self._by_id(suggest_ambassadors_for_event(self.event.id, self.tenant.id))
        assert by[self.high.id]["is_available"] is True
        assert by[self.low.id]["is_available"] is False
        assert by[self.high.id]["score"] > by[self.low.id]["score"]

    def test_per_tenant_scoping_excludes_other_tenant_history(self):
        # `high` has a blistering record for the OTHER tenant; NONE of it may
        # touch their Girl Beer score (the core scoping assertion).
        other_event = self._event(name="Valero", tenant=self.other_tenant)
        self._shift(self.high, other_event, tenant=self.other_tenant)
        self._shift(self.high, other_event, tenant=self.other_tenant)
        self._rating(self.high, other_event, 5, tenant=self.other_tenant)
        self._favorite(self.high, tenant=self.other_tenant)

        by = self._by_id(suggest_ambassadors_for_event(self.event.id, self.tenant.id))
        high = by[self.high.id]
        # Brand experience for THIS tenant is zero (other-tenant shifts excluded).
        assert high["gigs_for_brand"] == 0
        # No Girl Beer rating -> avg_rating None (the other-tenant 5★ excluded).
        assert high["avg_rating"] is None
        # Not favorited by THIS tenant (other-tenant favorite excluded).
        assert high["is_favorited"] is False

    def test_excludes_already_rostered_ba(self):
        # A BA already on the event's roster is not suggested.
        self._shift(self.high, self.event)  # rostered on THE event itself
        rows = suggest_ambassadors_for_event(self.event.id, self.tenant.id)
        ids = {r["ba_id"] for r in rows}
        assert self.high.id not in ids
        assert self.low.id in ids

    def test_missing_coordinates_degrades_distance_to_null(self):
        # `low` has no coordinates -> distance_mi null, no distance reason, no crash.
        by = self._by_id(suggest_ambassadors_for_event(self.event.id, self.tenant.id))
        assert by[self.low.id]["distance_mi"] is None
        assert not any("mi away" in r for r in by[self.low.id]["reasons"])

    def test_no_event_datetime_makes_availability_null(self):
        # An event with no start/end window -> is_available null for everyone,
        # and the availability signal is simply omitted (no crash).
        dateless = self.create_event(
            name="No Window", tenant=self.tenant, address="x"
        )
        self._recurring_availability(
            self.high, 0, self._event_start_t, self._event_end_t
        )
        by = self._by_id(
            suggest_ambassadors_for_event(dateless.id, self.tenant.id)
        )
        assert by[self.high.id]["is_available"] is None
        assert "available" not in by[self.high.id]["reasons"]

    def test_denorm_rating_used_when_no_tenant_ratings(self):
        # No AmbassadorRating rows for this tenant, but the denormalized
        # Ambassador.rating is set -> it's used as the avg_rating fallback.
        self.high.rating = 4
        self.high.save(update_fields=["rating"])
        by = self._by_id(suggest_ambassadors_for_event(self.event.id, self.tenant.id))
        assert by[self.high.id]["avg_rating"] == 4.0
        assert "4★" in by[self.high.id]["reasons"]

    def test_unknown_event_returns_empty(self):
        assert suggest_ambassadors_for_event(987654321, self.tenant.id) == []

    def test_event_in_other_tenant_returns_empty(self):
        # Passing the event id but a mismatched tenant id -> empty (the helper
        # re-scopes the event to the tenant as defense in depth).
        assert suggest_ambassadors_for_event(self.event.id, self.other_tenant.id) == []

    def test_limit_caps_results(self):
        rows = suggest_ambassadors_for_event(self.event.id, self.tenant.id, limit=1)
        assert len(rows) == 1

    # -- GraphQL resolver tests --------------------------------------------

    @pytest.mark.asyncio
    async def test_graphql_admin_any_tenant_event(self):
        await sync_to_async(self._rating)(self.high, self.event, 5)
        await sync_to_async(self._favorite)(self.high)
        result = await self._execute_query_authenticated(
            SUGGESTIONS_QUERY,
            {"eventId": str(self.event.uuid), "limit": 20},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        rows = result.data["staffingSuggestions"]
        ids = {int(r["baId"]) for r in rows}
        assert ids == {self.high.id, self.low.id}
        by = {int(r["baId"]): r for r in rows}
        assert by[self.high.id]["avgRating"] == 5.0
        assert by[self.high.id]["isFavorited"] is True
        # Best-fit first in the GraphQL output.
        assert int(rows[0]["baId"]) == self.high.id

    @pytest.mark.asyncio
    async def test_graphql_client_can_see_own_tenant_event(self):
        result = await self._execute_query_authenticated(
            SUGGESTIONS_QUERY,
            {"eventId": str(self.event.uuid), "limit": 20},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        rows = result.data["staffingSuggestions"]
        ids = {int(r["baId"]) for r in rows}
        assert ids == {self.high.id, self.low.id}

    @pytest.mark.asyncio
    async def test_graphql_client_denied_other_tenant_event_empty(self):
        # A client of the OTHER tenant asks about THIS tenant's event ->
        # deny/empty, never an error (the event is out of their scope).
        result = await self._execute_query_authenticated(
            SUGGESTIONS_QUERY,
            {"eventId": str(self.event.uuid), "limit": 20},
            self.other_client,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        assert result.data["staffingSuggestions"] == []

    @pytest.mark.asyncio
    async def test_graphql_unknown_event_empty_not_error(self):
        result = await self._execute_query_authenticated(
            SUGGESTIONS_QUERY,
            {"eventId": "987654321", "limit": 20},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        assert result.data["staffingSuggestions"] == []
