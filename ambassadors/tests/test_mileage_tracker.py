"""
Tests for the GPS mileage tracker (mobile schema):

- startMileageSession opens an active session for a gig that has
  Event.track_mileage = True
- recordMileageBreadcrumbs ingests the GPS trail
- stopMileageSession sums the ordered breadcrumbs into total miles and
  computes reimbursement = miles * the event's snapshotted rate
- myMileageSessions returns the BA's completed trip (with the trail)
- eventMileageSummary rolls up miles + reimbursement
- A gig with track_mileage = False can't open a session

Mileage is asserted against MileageService._miles_from_points so the test
tracks the production haversine exactly rather than hard-coding a constant.
"""

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from ambassadors.models import MileageSession
from ambassadors.services import MileageService
from ambassadors.tests.base import AmbassadorsGraphQLTestCase

User = get_user_model()

START = """
mutation Start($input: StartMileageSessionInput!) {
  startMileageSession(input: $input) {
    success message session { uuid status eventUuid }
  }
}
"""

RECORD = """
mutation Record($input: RecordMileageBreadcrumbsInput!) {
  recordMileageBreadcrumbs(input: $input) { success message }
}
"""

STOP = """
mutation Stop($input: StopMileageSessionInput!) {
  stopMileageSession(input: $input) {
    success message
    session {
      uuid status totalMiles ratePerMile reimbursementAmount
      breadcrumbCount breadcrumbs { lat lng }
    }
  }
}
"""

MY_SESSIONS = """
query Mine($eventUuid: ID!) {
  myMileageSessions(eventUuid: $eventUuid) {
    uuid status totalMiles reimbursementAmount breadcrumbCount
  }
}
"""

SUMMARY = """
query Summary($eventId: ID!) {
  eventMileageSummary(eventId: $eventId) {
    eventUuid totalMiles totalReimbursement sessionCount
    sessions { uuid status totalMiles }
  }
}
"""

# Three points heading due north, ~0.01 deg latitude apart (~0.69 mi each).
TRAIL = [
    {"lat": 40.0000, "lng": -105.0000, "accuracyMeters": 5.0,
     "recordedAt": "2026-06-18T17:00:00+00:00"},
    {"lat": 40.0100, "lng": -105.0000, "accuracyMeters": 5.0,
     "recordedAt": "2026-06-18T17:05:00+00:00"},
    {"lat": 40.0200, "lng": -105.0000, "accuracyMeters": 5.0,
     "recordedAt": "2026-06-18T17:10:00+00:00"},
]


@pytest.mark.django_db(transaction=True)
class TestMileageTracker(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_mobile import schema_mobile

        self.roles = self.setup_default_roles()
        self.schema = schema_mobile
        self.endpoint_path = "/api/v1/graphql/mobile"
        self.tenant = self.create_tenant(name="Mileage Tenant")

        self.ba_user = self.create_user(
            username="ba-miles", email="ba@t.com",
            role=self.roles["ambassador"],
        )
        self.ambassador = self.create_ambassador(self.ba_user)

        # The gig that tracks mileage at $0.70/mile.
        self.event = self.create_event(
            name="Sampling Gig", tenant=self.tenant,
            track_mileage=True, mileage_rate=Decimal("0.70"),
        )
        # A gig that does NOT track mileage.
        self.no_track_event = self.create_event(
            name="No-Track Gig", tenant=self.tenant,
        )

    @pytest.mark.asyncio
    async def test_full_start_record_stop_flow(self):
        # Start
        res = await self._execute_mutation(
            START, {"input": {"eventUuid": str(self.event.uuid)}},
            user=self.ba_user,
        )
        assert res.errors is None, res.errors
        start = res.data["startMileageSession"]
        assert start["success"] is True
        assert start["session"]["status"] == MileageSession.STATUS_ACTIVE
        assert start["session"]["eventUuid"] == str(self.event.uuid)
        session_uuid = start["session"]["uuid"]

        # Record the first two points
        res = await self._execute_mutation(
            RECORD,
            {"input": {"sessionUuid": session_uuid, "points": TRAIL[:2]}},
            user=self.ba_user,
        )
        assert res.errors is None, res.errors
        assert res.data["recordMileageBreadcrumbs"]["success"] is True

        # Stop, sending the trailing point with the stop call
        res = await self._execute_mutation(
            STOP,
            {"input": {"sessionUuid": session_uuid, "points": TRAIL[2:]}},
            user=self.ba_user,
        )
        assert res.errors is None, res.errors
        stopped = res.data["stopMileageSession"]
        assert stopped["success"] is True
        sess = stopped["session"]
        assert sess["status"] == MileageSession.STATUS_COMPLETED
        assert sess["breadcrumbCount"] == 3

        expected_miles = MileageService._miles_from_points(
            [(p["lat"], p["lng"]) for p in TRAIL]
        )
        assert expected_miles > 1.0  # sanity: ~1.38 mi
        assert sess["totalMiles"] == pytest.approx(expected_miles, abs=0.01)
        assert sess["ratePerMile"] == pytest.approx(0.70, abs=0.001)
        expected_reimb = float(
            (Decimal(str(expected_miles)) * Decimal("0.70")).quantize(
                Decimal("0.01")
            )
        )
        assert sess["reimbursementAmount"] == pytest.approx(
            expected_reimb, abs=0.01
        )

        # myMileageSessions shows the completed trip
        res = await self._execute_mutation(
            MY_SESSIONS, {"eventUuid": str(self.event.uuid)},
            user=self.ba_user,
        )
        assert res.errors is None, res.errors
        mine = res.data["myMileageSessions"]
        assert len(mine) == 1
        assert mine[0]["status"] == MileageSession.STATUS_COMPLETED
        assert mine[0]["breadcrumbCount"] == 3

        # eventMileageSummary rolls it up
        res = await self._execute_mutation(
            SUMMARY, {"eventId": str(self.event.uuid)}, user=self.ba_user,
        )
        assert res.errors is None, res.errors
        summary = res.data["eventMileageSummary"]
        assert summary["sessionCount"] == 1
        assert summary["totalMiles"] == pytest.approx(expected_miles, abs=0.01)
        assert summary["totalReimbursement"] == pytest.approx(
            expected_reimb, abs=0.01
        )

    @pytest.mark.asyncio
    async def test_cannot_start_when_tracking_disabled(self):
        res = await self._execute_mutation(
            START, {"input": {"eventUuid": str(self.no_track_event.uuid)}},
            user=self.ba_user,
        )
        assert res.errors is None, res.errors
        start = res.data["startMileageSession"]
        assert start["success"] is False
        assert "enabled" in start["message"].lower()
        assert start["session"] is None

    @pytest.mark.asyncio
    async def test_start_resumes_existing_active_session(self):
        """A second start while one is active resumes it (no duplicate)."""
        first = await self._execute_mutation(
            START, {"input": {"eventUuid": str(self.event.uuid)}},
            user=self.ba_user,
        )
        second = await self._execute_mutation(
            START, {"input": {"eventUuid": str(self.event.uuid)}},
            user=self.ba_user,
        )
        assert first.data["startMileageSession"]["success"] is True
        assert second.data["startMileageSession"]["success"] is True
        assert (
            first.data["startMileageSession"]["session"]["uuid"]
            == second.data["startMileageSession"]["session"]["uuid"]
        )
