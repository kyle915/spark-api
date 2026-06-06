"""Coverage for BA reliability scoring + its use in open-shift alert ordering.

* :func:`ambassadors.reliability.reliability_for_users` /
  :func:`compute_reliability` — completed + claimed over completed + claimed +
  dropped, ``None`` ("New") with no history, plus the tiered label.
* ``send_open_shift_alerts`` ranks its candidate pool most-reliable-first, so a
  capped fan-out pings the dependable BAs (this is the headline of the feature:
  "used to order open-shift alerts, most reliable first").
"""

from __future__ import annotations

import datetime
import uuid
from unittest import mock

import pytest
from django.core.management import call_command
from django.utils import timezone

from ambassadors.models import AmbassadorEvent, OpenShift, PushDevice
from ambassadors.reliability import compute_reliability, reliability_for_users
from ambassadors.tests.base import AmbassadorsGraphQLTestCase


@pytest.mark.django_db(transaction=True)
class TestReliability(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Girl Beer")

    # -- fixture helpers ------------------------------------------------

    def _ba(self, label):
        uid = str(uuid.uuid4())[:8]
        u = self.create_user(
            username=f"ba_{label}_{uid}@t.com",
            email=f"ba_{label}_{uid}@t.com",
            first_name=label.title(),
            last_name="Tester",
            role=self.roles["ambassador"],
        )
        return self.create_ambassador(u, is_active=True)

    def _past_event(self, name):
        when = timezone.now() - datetime.timedelta(days=7)
        return self.create_event(
            name=name,
            tenant=self.tenant,
            address="1 St",
            date=when,
            start_time=when,
            end_time=when + datetime.timedelta(hours=3),
        )

    def _future_event(self, name):
        when = timezone.now() + datetime.timedelta(days=3)
        return self.create_event(
            name=name,
            tenant=self.tenant,
            address="1 St",
            date=when,
            start_time=when,
            end_time=when + datetime.timedelta(hours=3),
        )

    def _completed(self, ba, n):
        for i in range(n):
            AmbassadorEvent.objects.create(
                ambassador=ba,
                event=self._past_event(f"done {i}"),
                tenant=self.tenant,
                is_approved=True,
                created_by=self.get_system_user(),
            )

    def _dropped(self, ba, n):
        # PAST events on purpose: reliability counts every released_by row
        # regardless of time, but the open-shift-alerts cron only processes
        # FUTURE unclaimed shifts — so these history rows don't leak into the
        # ordering test's fan-out.
        for _ in range(n):
            OpenShift.objects.create(
                event=self._past_event("dropped"), released_by=ba.user
            )

    def _claimed(self, ba, n):
        for _ in range(n):
            OpenShift.objects.create(
                event=self._past_event("claimed"), claimed_by=ba.user
            )

    def _device(self, ba):
        PushDevice.objects.create(
            user=ba.user,
            token=f"ExponentPushToken[{uuid.uuid4().hex[:18]}]",
            platform="ios",
            is_active=True,
        )

    # -- scoring --------------------------------------------------------

    def test_scores_and_labels(self):
        ace = self._ba("ace")
        self._completed(ace, 8)
        self._claimed(ace, 2)  # 100 * (8 + 2) / (8 + 2 + 0) = 100

        mixed = self._ba("mixed")
        self._completed(mixed, 3)
        self._dropped(mixed, 2)  # 100 * 3 / 5 = 60

        flaky = self._ba("flaky")
        self._completed(flaky, 2)
        self._dropped(flaky, 6)  # 100 * 2 / 8 = 25

        newbie = self._ba("newbie")  # no history at all

        rel = reliability_for_users(
            [ace.user_id, mixed.user_id, flaky.user_id, newbie.user_id]
        )

        assert rel[ace.user_id].score == 100
        assert rel[ace.user_id].label == "Excellent"
        assert rel[ace.user_id].completed == 8
        assert rel[ace.user_id].claimed == 2
        assert rel[ace.user_id].dropped == 0

        assert rel[mixed.user_id].score == 60
        assert rel[mixed.user_id].label == "Mixed"

        assert rel[flaky.user_id].score == 25
        assert rel[flaky.user_id].label == "Needs attention"

        assert rel[newbie.user_id].score is None
        assert rel[newbie.user_id].label == "New"

        # New BAs sort at a neutral rank: above a known dropper, below proven.
        assert (
            rel[flaky.user_id].sort_score
            < rel[newbie.user_id].sort_score
            < rel[ace.user_id].sort_score
        )

    def test_compute_single_and_unknown(self):
        ba = self._ba("solo")
        self._completed(ba, 4)
        r = compute_reliability(ba.user_id)
        assert r.score == 100 and r.completed == 4 and r.dropped == 0

        # A user with no history (or unknown id) is "New", score None.
        unknown = compute_reliability(999999999)
        assert unknown.score is None and unknown.label == "New"

    # -- alert ordering -------------------------------------------------

    def test_open_shift_alerts_rank_most_reliable_first(self):
        """With the fan-out capped at 1, the single push goes to the most
        reliable eligible BA — not whoever the queryset happened to return."""
        reliable = self._ba("reliable")
        self._completed(reliable, 8)  # score 100
        self._device(reliable)

        flaky = self._ba("flaky_alert")
        self._completed(flaky, 1)
        self._dropped(flaky, 6)  # score ~14
        self._device(flaky)

        # The BA who dropped THIS shift (released_by) — excluded from the pool.
        dropper = self._ba("dropper")

        open_event = self._future_event("Vons Pop-up")
        OpenShift.objects.create(event=open_event, released_by=dropper.user)

        sent: list[int] = []

        def _capture(user_id, **kwargs):
            sent.append(user_id)

        with mock.patch(
            "ambassadors.push._send_push_to_user_sync", side_effect=_capture
        ):
            call_command("send_open_shift_alerts", "--max-per-shift", "1")

        # Exactly one push (capped), and it went to the reliable BA.
        assert sent == [reliable.user_id]
