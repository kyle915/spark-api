"""
Tests for the self-serve "Open shifts" board — my_open_shifts query +
claim_open_shift mutation (mobile schema), and that release_my_shift opens
the slot.

Coverage:
- An eligible BA (worked with the brand) sees a dropped shift and claims it
  → instantly booked (approved AmbassadorEvent) + OpenShift resolved
- Claiming an already-claimed slot → success=False (race-safe)
- A BA with no history at the brand can't see or claim it (tenant-safe)
- Can't claim a shift that already started
- release_my_shift creates the OpenShift (the full drop→board loop)
"""

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.utils import timezone

from ambassadors.models import AmbassadorEvent, OpenShift
from ambassadors.tests.base import AmbassadorsGraphQLTestCase

User = get_user_model()

CLAIM = """
mutation Claim($input: ClaimOpenShiftInput!) {
  claimOpenShift(input: $input) { success message }
}
"""

OPEN_SHIFTS = """
query { myOpenShifts { openShiftUuid eventUuid eventName } }
"""

RELEASE = """
mutation R($input: CancelShiftInviteInput!) {
  releaseMyShift(input: $input) { success }
}
"""


@pytest.mark.django_db(transaction=True)
class TestClaimOpenShift(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_mobile import schema_mobile

        self.roles = self.setup_default_roles()
        self.schema = schema_mobile
        self.endpoint_path = "/api/v1/graphql/mobile"
        self.tenant = self.create_tenant(name="Open Tenant")

        self.dropper_user = self.create_user(
            username="ba-dropper", email="d@t.com", role=self.roles["ambassador"]
        )
        self.dropper = self.create_ambassador(self.dropper_user)
        self.claimer_user = self.create_user(
            username="ba-claimer", email="c@t.com", role=self.roles["ambassador"]
        )
        self.claimer = self.create_ambassador(self.claimer_user)
        self.stranger_user = self.create_user(
            username="ba-stranger", email="s@t.com", role=self.roles["ambassador"]
        )
        self.stranger = self.create_ambassador(self.stranger_user)

        self.event = self.create_event(name="Open Shift", tenant=self.tenant)
        # A separate event that gives the claimer history with this brand.
        self.history_event = self.create_event(name="Past Gig", tenant=self.tenant)

    async def _set_start(self, event, when):
        def _go():
            event.start_time = when
            event.save(update_fields=["start_time"])

        await sync_to_async(_go)()

    async def _seed(self):
        """Claimer gets brand history; the shift is future; one open slot."""
        await sync_to_async(AmbassadorEvent.objects.create)(
            ambassador=self.claimer,
            event=self.history_event,
            tenant=self.tenant,
            is_approved=True,
            created_by=self.claimer_user,
        )
        await self._set_start(self.event, timezone.now() + timezone.timedelta(days=2))
        return await sync_to_async(OpenShift.objects.create)(
            event=self.event, released_by=self.dropper_user
        )

    @pytest.mark.asyncio
    async def test_eligible_ba_sees_and_claims(self):
        row = await self._seed()

        q = await self._execute_mutation(
            OPEN_SHIFTS, {}, self.endpoint_path, user=self.claimer_user
        )
        assert q.errors is None, f"errored: {q.errors}"
        uuids = [r["openShiftUuid"] for r in q.data["myOpenShifts"]]
        assert str(row.uuid) in uuids

        r = await self._execute_mutation(
            CLAIM,
            {"input": {"openShiftUuid": str(row.uuid)}},
            self.endpoint_path,
            user=self.claimer_user,
        )
        assert r.errors is None, f"errored: {r.errors}"
        assert r.data["claimOpenShift"]["success"] is True

        booked = await sync_to_async(
            AmbassadorEvent.objects.filter(
                ambassador=self.claimer, event=self.event, is_approved=True
            ).exists
        )()
        assert booked is True
        refreshed = await sync_to_async(lambda: OpenShift.objects.get(uuid=row.uuid))()
        assert refreshed.claimed_at is not None
        assert refreshed.claimed_by_id == self.claimer_user.id

    @pytest.mark.asyncio
    async def test_double_claim_only_first_wins(self):
        row = await self._seed()
        r1 = await self._execute_mutation(
            CLAIM,
            {"input": {"openShiftUuid": str(row.uuid)}},
            self.endpoint_path,
            user=self.claimer_user,
        )
        assert r1.data["claimOpenShift"]["success"] is True
        r2 = await self._execute_mutation(
            CLAIM,
            {"input": {"openShiftUuid": str(row.uuid)}},
            self.endpoint_path,
            user=self.claimer_user,
        )
        assert r2.data["claimOpenShift"]["success"] is False
        assert "claimed" in r2.data["claimOpenShift"]["message"].lower()

    @pytest.mark.asyncio
    async def test_stranger_without_history_cannot_see_or_claim(self):
        row = await self._seed()
        q = await self._execute_mutation(
            OPEN_SHIFTS, {}, self.endpoint_path, user=self.stranger_user
        )
        assert q.errors is None
        assert q.data["myOpenShifts"] == []

        r = await self._execute_mutation(
            CLAIM,
            {"input": {"openShiftUuid": str(row.uuid)}},
            self.endpoint_path,
            user=self.stranger_user,
        )
        assert r.data["claimOpenShift"]["success"] is False
        # not claimed by the stranger
        refreshed = await sync_to_async(lambda: OpenShift.objects.get(uuid=row.uuid))()
        assert refreshed.claimed_at is None

    @pytest.mark.asyncio
    async def test_cannot_claim_started_shift(self):
        row = await self._seed()
        await self._set_start(self.event, timezone.now() - timezone.timedelta(hours=1))
        r = await self._execute_mutation(
            CLAIM,
            {"input": {"openShiftUuid": str(row.uuid)}},
            self.endpoint_path,
            user=self.claimer_user,
        )
        assert r.data["claimOpenShift"]["success"] is False

    @pytest.mark.asyncio
    async def test_release_creates_open_shift(self):
        await self._set_start(self.event, timezone.now() + timezone.timedelta(days=3))
        ae = await sync_to_async(AmbassadorEvent.objects.create)(
            ambassador=self.dropper,
            event=self.event,
            tenant=self.tenant,
            is_approved=True,
            created_by=self.dropper_user,
        )
        r = await self._execute_mutation(
            RELEASE,
            {"input": {"ambassadorEventUuid": str(ae.uuid)}},
            self.endpoint_path,
            user=self.dropper_user,
        )
        assert r.data["releaseMyShift"]["success"] is True
        cnt = await sync_to_async(
            OpenShift.objects.filter(
                event=self.event, claimed_at__isnull=True
            ).count
        )()
        assert cnt >= 1
