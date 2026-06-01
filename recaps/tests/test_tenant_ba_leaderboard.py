"""Coverage for the per-BA performance leaderboard backend:

* :func:`recaps.tenant_ba_leaderboard.tenant_ba_leaderboard` — the per-BA
  roll-up that ranks the Brand Ambassadors who worked FOR ONE TENANT by
  shifts worked (:class:`ambassadors.models.AmbassadorEvent`), recaps filed
  (legacy ``Recap`` + custom ``CustomRecap``), and gig ratings
  (:class:`ambassadors.models.AmbassadorRating`), and
* the ``tenantBaLeaderboard(tenantId, year)`` GraphQL query on the clients
  schema — the tenant-scoped, never-raises shell around it.

The helper tests assert the per-tenant scoping (a BA's OTHER-tenant activity
must never leak), the metric math (counts + avg_rating/ratings_count, with
unrated -> None), the year filter, and the sort order, directly against the
DB. The GraphQL tests assert the ``BaLeaderboardEntry`` shape, tenant scoping
(clients pinned to their own tenant, admin may target any), and the
degrade-to-empty-list posture (missing / out-of-scope tenant, never an
error). Mirrors the fixture style of test_tenant_market_performance.py.

All rows are created "now"; for the year-filter test the relevant rows are
back-dated with ``.update(created_at=...)`` after creation because every
source model's ``created_at`` is ``auto_now_add`` and can't be set at create.
"""

import datetime

import pytest
from django.utils import timezone

from ambassadors import models as amb_models
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models
from recaps.tenant_ba_leaderboard import tenant_ba_leaderboard


LEADERBOARD_QUERY = """
query Board($tenantId: ID!, $year: Int) {
  tenantBaLeaderboard(tenantId: $tenantId, year: $year) {
    baId
    name
    shiftsWorked
    recapsFiled
    avgRating
    ratingsCount
    reliabilityPct
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestTenantBaLeaderboard(AmbassadorsGraphQLTestCase):
    """Per-BA leaderboard scoped to one tenant."""

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
            username="admin-board",
            email="admin-board@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="client-board",
            email="client-board@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        # --- BAs -------------------------------------------------------
        # alice: highly rated, most active for OUR tenant.
        self.alice = self._ba("alice", "Alice", "Ace")
        # bob: rated lower, fewer recaps.
        self.bob = self._ba("bob", "Bob", "Bee")
        # carol: worked our tenant (a shift + a recap) but is UNRATED.
        self.carol = self._ba("carol", "Carol", "Cee")
        # dave: works ONLY for the OTHER tenant — must never appear here.
        self.dave = self._ba("dave", "Dave", "Dee")

        # --- our tenant's events --------------------------------------
        self.ev1 = self.create_event(name="WF Burbank", tenant=self.tenant)
        self.ev2 = self.create_event(name="WF Pasadena", tenant=self.tenant)
        self.template = self._template("GB Template")

        # --- shifts (AmbassadorEvent roster rows), our tenant ----------
        # alice: 2 shifts; bob: 1; carol: 1.
        self._shift(self.alice, self.ev1)
        self._shift(self.alice, self.ev2)
        self._shift(self.bob, self.ev1)
        self._shift(self.carol, self.ev2)

        # --- recaps filed, our tenant ---------------------------------
        # alice: 1 legacy + 1 custom = 2; bob: 1 legacy; carol: 1 legacy.
        self._legacy_recap(self.alice, self.ev1, name="alice legacy")
        self._custom_recap(self.alice, self.ev2, name="alice custom")
        self._legacy_recap(self.bob, self.ev1, name="bob legacy")
        self._legacy_recap(self.carol, self.ev2, name="carol legacy")
        # An EXTERNAL typed-name recap (no ambassador FK) — excluded from
        # the leaderboard (no rankable id), must not crash or add a row.
        recap_models.Recap.objects.create(
            name="ext recap",
            event=self.ev1,
            ambassador=None,
            external_ba_name="Walk-in Helper",
            created_by=self.system_user,
            updated_by=self.system_user,
        )

        # --- ratings on our tenant's gigs -----------------------------
        # alice: 5 and 4 -> avg 4.5 over 2; bob: 3 -> avg 3.0 over 1.
        self._rating(self.alice, self.ev1, 5)
        self._rating(self.alice, self.ev2, 4, by_client=True)
        self._rating(self.bob, self.ev1, 3)
        # carol: NO rating -> avg_rating None, ratings_count 0.

        # --- OTHER tenant activity for OUR BAs ------------------------
        # alice ALSO works for Liquid Death: a shift, a recap, and a
        # blistering 1-star rating. NONE of this may touch her Girl Beer
        # numbers (this is the core per-tenant-scoping assertion).
        self.other_event = self.create_event(
            name="Valero", tenant=self.other_tenant
        )
        self._shift(self.alice, self.other_event, tenant=self.other_tenant)
        self._legacy_recap(
            self.alice, self.other_event, name="alice other", tenant_check=False
        )
        self._rating(
            self.alice, self.other_event, 1, tenant=self.other_tenant
        )

        # dave is purely an other-tenant BA.
        self._shift(self.dave, self.other_event, tenant=self.other_tenant)
        self._rating(self.dave, self.other_event, 5, tenant=self.other_tenant)

    # -- fixture helpers ------------------------------------------------

    def _ba(self, username: str, first: str, last: str) -> amb_models.Ambassador:
        user = self.create_user(
            username=username,
            email=f"{username}@test.com",
            role=self.roles["ambassador"],
            first_name=first,
            last_name=last,
        )
        return self.create_ambassador(user=user)

    def _template(self, name: str) -> recap_models.CustomRecapTemplate:
        event_type = self.create_event_type(name="Sampling", tenant=self.tenant)
        return recap_models.CustomRecapTemplate.objects.create(
            name=name,
            event_type=event_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )

    def _shift(self, ambassador, event, tenant=None) -> amb_models.AmbassadorEvent:
        return amb_models.AmbassadorEvent.objects.create(
            ambassador=ambassador,
            event=event,
            tenant=tenant or self.tenant,
            created_by=self.system_user,
            updated_by=self.system_user,
        )

    def _legacy_recap(
        self, ambassador, event, name: str, tenant_check=True
    ) -> recap_models.Recap:
        return recap_models.Recap.objects.create(
            name=name,
            event=event,
            ambassador=ambassador,
            created_by=self.system_user,
            updated_by=self.system_user,
        )

    def _custom_recap(
        self, ambassador, event, name: str
    ) -> recap_models.CustomRecap:
        return recap_models.CustomRecap.objects.create(
            name=name,
            event=event,
            ambassador=ambassador,
            tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.system_user,
            updated_by=self.system_user,
        )

    def _rating(
        self, ambassador, event, score: int, by_client=False, tenant=None
    ) -> amb_models.AmbassadorRating:
        return amb_models.AmbassadorRating.objects.create(
            ambassador=ambassador,
            event=event,
            tenant=tenant or self.tenant,
            score=score,
            by_client=by_client,
            created_by=self.system_user,
            updated_by=self.system_user,
        )

    def _by_id(self, rows):
        return {int(r["ba_id"]): r for r in rows}

    # -- helper-level tests ---------------------------------------------

    def test_includes_only_bas_active_for_this_tenant(self):
        rows = tenant_ba_leaderboard(self.tenant.id)
        ids = {r["ba_id"] for r in rows}
        # alice, bob, carol worked for Girl Beer; dave (other tenant) excluded.
        assert ids == {self.alice.id, self.bob.id, self.carol.id}
        assert self.dave.id not in ids

    def test_per_tenant_scoping_excludes_other_tenant_activity(self):
        # alice's Girl Beer numbers must NOT include her Liquid Death shift,
        # recap, or 1-star rating.
        by = self._by_id(tenant_ba_leaderboard(self.tenant.id))
        alice = by[self.alice.id]
        assert alice["shifts_worked"] == 2  # ev1 + ev2, NOT the Valero shift
        assert alice["recaps_filed"] == 2  # 1 legacy + 1 custom, NOT the other
        # avg over [5, 4] == 4.5; the other-tenant 1-star is excluded.
        assert alice["avg_rating"] == 4.5
        assert alice["ratings_count"] == 2

    def test_counts_recaps_and_shifts(self):
        by = self._by_id(tenant_ba_leaderboard(self.tenant.id))
        assert by[self.bob.id]["shifts_worked"] == 1
        assert by[self.bob.id]["recaps_filed"] == 1
        assert by[self.carol.id]["shifts_worked"] == 1
        assert by[self.carol.id]["recaps_filed"] == 1

    def test_avg_rating_and_count_with_unrated_none(self):
        by = self._by_id(tenant_ba_leaderboard(self.tenant.id))
        assert by[self.bob.id]["avg_rating"] == 3.0
        assert by[self.bob.id]["ratings_count"] == 1
        # carol worked but was never rated -> None / 0, never 0.0.
        assert by[self.carol.id]["avg_rating"] is None
        assert by[self.carol.id]["ratings_count"] == 0

    def test_reliability_pct_omitted(self):
        # Reliability is intentionally not computed (attendance data unclean).
        for row in tenant_ba_leaderboard(self.tenant.id):
            assert row["reliability_pct"] is None

    def test_names_resolve_from_user(self):
        by = self._by_id(tenant_ba_leaderboard(self.tenant.id))
        assert by[self.alice.id]["name"] == "Alice Ace"
        assert by[self.carol.id]["name"] == "Carol Cee"

    def test_sort_order_rating_then_recaps_then_shifts(self):
        rows = tenant_ba_leaderboard(self.tenant.id)
        order = [r["ba_id"] for r in rows]
        # alice (4.5) > bob (3.0) > carol (unrated, last).
        assert order == [self.alice.id, self.bob.id, self.carol.id]
        # unrated BA is strictly last.
        assert rows[-1]["ba_id"] == self.carol.id
        assert rows[-1]["avg_rating"] is None

    def test_sort_tiebreak_recaps_then_shifts_among_unrated(self):
        # Two unrated BAs: ranked by recaps_filed desc, then shifts desc.
        erin = self._ba("erin", "Erin", "Eff")
        finn = self._ba("finn", "Finn", "Gee")
        ev3 = self.create_event(name="Extra", tenant=self.tenant)
        # erin: 2 recaps, 1 shift; finn: 1 recap, 3 shifts. erin sorts first
        # (recaps dominates shifts).
        self._legacy_recap(erin, ev3, name="erin r1")
        self._legacy_recap(erin, self.ev1, name="erin r2")
        self._shift(erin, ev3)
        self._legacy_recap(finn, ev3, name="finn r1")
        self._shift(finn, ev3)
        self._shift(finn, self.ev1)
        self._shift(finn, self.ev2)

        rows = tenant_ba_leaderboard(self.tenant.id)
        order = [r["ba_id"] for r in rows]
        # Among the unrated tail, erin (2 recaps) precedes finn (1 recap),
        # which precedes carol (1 recap, 1 shift) — finn has 3 shifts so beats
        # carol on the shifts tiebreak.
        assert order.index(erin.id) < order.index(finn.id)
        assert order.index(finn.id) < order.index(self.carol.id)

    def test_year_filter(self):
        # Back-date ALL of bob's current-year activity into 2021, then assert
        # a 2021 query sees bob and a 2020 query sees nobody for bob.
        when = timezone.make_aware(datetime.datetime(2021, 6, 15, 12, 0))
        amb_models.AmbassadorEvent.objects.filter(
            ambassador=self.bob, tenant=self.tenant
        ).update(created_at=when)
        recap_models.Recap.objects.filter(
            ambassador=self.bob, event__tenant=self.tenant
        ).update(created_at=when)
        amb_models.AmbassadorRating.objects.filter(
            ambassador=self.bob, tenant=self.tenant
        ).update(created_at=when)

        by_2021 = self._by_id(tenant_ba_leaderboard(self.tenant.id, year=2021))
        assert self.bob.id in by_2021
        assert by_2021[self.bob.id]["shifts_worked"] == 1
        assert by_2021[self.bob.id]["recaps_filed"] == 1
        assert by_2021[self.bob.id]["avg_rating"] == 3.0
        # alice/carol activity is still "now", so they're absent from 2021.
        assert self.alice.id not in by_2021
        assert self.carol.id not in by_2021

        # A year with no activity at all -> empty list.
        assert tenant_ba_leaderboard(self.tenant.id, year=2019) == []

    def test_empty_tenant_returns_empty_list(self):
        empty = self.create_tenant(name="No Activity")
        assert tenant_ba_leaderboard(empty.id) == []

    def test_unknown_tenant_is_empty(self):
        assert tenant_ba_leaderboard(987654321) == []

    # -- GraphQL resolver tests -----------------------------------------

    @pytest.mark.asyncio
    async def test_admin_targets_tenant(self):
        result = await self._execute_query_authenticated(
            LEADERBOARD_QUERY,
            {"tenantId": str(self.tenant.id)},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        rows = result.data["tenantBaLeaderboard"]
        ids = {int(r["baId"]) for r in rows}
        assert ids == {self.alice.id, self.bob.id, self.carol.id}
        by = {int(r["baId"]): r for r in rows}
        assert by[self.alice.id]["avgRating"] == 4.5
        assert by[self.alice.id]["shiftsWorked"] == 2
        assert by[self.carol.id]["avgRating"] is None
        assert by[self.carol.id]["ratingsCount"] == 0
        assert all(r["reliabilityPct"] is None for r in rows)
        # Sorted best-first in the GraphQL output too.
        assert [int(r["baId"]) for r in rows] == [
            self.alice.id,
            self.bob.id,
            self.carol.id,
        ]

    @pytest.mark.asyncio
    async def test_client_pinned_to_own_tenant(self):
        # Client passes the OTHER tenant's id but is pinned to their own.
        result = await self._execute_query_authenticated(
            LEADERBOARD_QUERY,
            {"tenantId": str(self.other_tenant.id)},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        rows = result.data["tenantBaLeaderboard"]
        ids = {int(r["baId"]) for r in rows}
        # Their own (Girl Beer) BAs, NOT Liquid Death's dave.
        assert ids == {self.alice.id, self.bob.id, self.carol.id}
        assert self.dave.id not in ids
        # And alice's numbers are the Girl Beer ones (avg 4.5, not the
        # other tenant's 1-star).
        by = {int(r["baId"]): r for r in rows}
        assert by[self.alice.id]["avgRating"] == 4.5

    @pytest.mark.asyncio
    async def test_admin_unknown_tenant_empty_not_error(self):
        result = await self._execute_query_authenticated(
            LEADERBOARD_QUERY,
            {"tenantId": "987654321"},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        assert result.data["tenantBaLeaderboard"] == []
