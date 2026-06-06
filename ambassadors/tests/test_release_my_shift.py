"""
Tests for ShiftAttendanceMutations.release_my_shift — the BA-side "I can't
make this shift" drop (mobile schema).

Coverage:
- BA releases their OWN approved, future shift → row deleted, success
- Releasing a PENDING (unapproved) offer → success=False, row stays
- Releasing ANOTHER BA's shift → "not found", row stays (self-scope)
- Releasing a shift that already STARTED → success=False, row stays
"""

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.utils import timezone

from ambassadors.models import AmbassadorEvent
from ambassadors.tests.base import AmbassadorsGraphQLTestCase

User = get_user_model()

MUTATION = """
mutation Release($input: CancelShiftInviteInput!) {
  releaseMyShift(input: $input) {
    success
    message
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestReleaseMyShift(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_mobile import schema_mobile

        self.roles = self.setup_default_roles()
        self.schema = schema_mobile
        self.endpoint_path = "/api/v1/graphql/mobile"
        self.tenant = self.create_tenant(name="Drop Tenant")
        self.ba_user = self.create_user(
            username="ba-drop",
            email="ba-drop@test.com",
            role=self.roles["ambassador"],
        )
        self.ambassador = self.create_ambassador(self.ba_user)
        self.other_user = self.create_user(
            username="ba-other",
            email="ba-other@test.com",
            role=self.roles["ambassador"],
        )
        self.other_ambassador = self.create_ambassador(self.other_user)
        self.event = self.create_event(name="Drop Shift", tenant=self.tenant)

    async def _set_event_start(self, when):
        def _go():
            self.event.start_time = when
            self.event.save(update_fields=["start_time"])
        await sync_to_async(_go)()

    async def _book(self, *, ambassador, approved: bool) -> AmbassadorEvent:
        return await sync_to_async(AmbassadorEvent.objects.create)(
            ambassador=ambassador,
            event=self.event,
            tenant=self.tenant,
            is_approved=approved,
            created_by=self.ba_user,
        )

    @pytest.mark.asyncio
    async def test_release_own_future_booking(self):
        await self._set_event_start(timezone.now() + timezone.timedelta(days=2))
        ae = await self._book(ambassador=self.ambassador, approved=True)
        result = await self._execute_mutation(
            MUTATION,
            {"input": {"ambassadorEventUuid": str(ae.uuid)}},
            self.endpoint_path,
            user=self.ba_user,
        )
        assert result.errors is None, f"errored: {result.errors}"
        assert result.data["releaseMyShift"]["success"] is True
        exists = await sync_to_async(
            AmbassadorEvent.objects.filter(uuid=ae.uuid).exists
        )()
        assert exists is False  # slot freed

    @pytest.mark.asyncio
    async def test_cannot_release_pending_offer(self):
        await self._set_event_start(timezone.now() + timezone.timedelta(days=2))
        ae = await self._book(ambassador=self.ambassador, approved=False)
        result = await self._execute_mutation(
            MUTATION,
            {"input": {"ambassadorEventUuid": str(ae.uuid)}},
            self.endpoint_path,
            user=self.ba_user,
        )
        assert result.errors is None
        assert result.data["releaseMyShift"]["success"] is False
        exists = await sync_to_async(
            AmbassadorEvent.objects.filter(uuid=ae.uuid).exists
        )()
        assert exists is True  # untouched

    @pytest.mark.asyncio
    async def test_cannot_release_another_bas_shift(self):
        await self._set_event_start(timezone.now() + timezone.timedelta(days=2))
        ae = await self._book(ambassador=self.other_ambassador, approved=True)
        result = await self._execute_mutation(
            MUTATION,
            {"input": {"ambassadorEventUuid": str(ae.uuid)}},
            self.endpoint_path,
            user=self.ba_user,  # not the owner
        )
        assert result.errors is None
        payload = result.data["releaseMyShift"]
        assert payload["success"] is False
        assert "not found" in payload["message"].lower()
        exists = await sync_to_async(
            AmbassadorEvent.objects.filter(uuid=ae.uuid).exists
        )()
        assert exists is True  # other BA's row untouched

    @pytest.mark.asyncio
    async def test_cannot_release_started_shift(self):
        await self._set_event_start(timezone.now() - timezone.timedelta(hours=1))
        ae = await self._book(ambassador=self.ambassador, approved=True)
        result = await self._execute_mutation(
            MUTATION,
            {"input": {"ambassadorEventUuid": str(ae.uuid)}},
            self.endpoint_path,
            user=self.ba_user,
        )
        assert result.errors is None
        assert result.data["releaseMyShift"]["success"] is False
        exists = await sync_to_async(
            AmbassadorEvent.objects.filter(uuid=ae.uuid).exists
        )()
        assert exists is True
