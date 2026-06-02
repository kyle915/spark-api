"""Tests for the at-post-time geo-proximity "new gig near you" push.

The public entry point `notify_nearby_bas_of_new_gig(job)` now just ENQUEUES
a single background RQ task and returns immediately — the fan-out runs
off-request in `_notify(job)` (re-fetched via `_run_notify_nearby_bas_task`).
This split is what keeps the postJob mutation from stalling on large rosters.

So the WHO-gets-pushed assertions exercise the worker logic `_notify(job)`
directly:
  - a BA within 30mi (coords present on both sides) IS notified, with the
    rounded distance in the payload;
  - a BA beyond 30mi (coords present) is NOT notified;
  - a BA who already applied to the job is skipped;
  - the preferred-state fallback fires for a BA with NULL coords whose
    preferred_state_codes match the job's event state;
  - notify_new_gigs=False opts a BA out;
  - the favorites_only gate.

Plus two tests for the public fn's contract: it enqueues once with the task
fn + job.id and does NOT fan out inline, and a failing enqueue is swallowed
(never falls back to running the fan-out inline).

enqueue_push is stubbed so nothing hits the network / RQ.
"""
from unittest.mock import patch

import pytest

from jobs.tests.base import JobsGraphQLTestCase
from jobs import models
from events.models import State


@pytest.mark.django_db(transaction=True)
class TestNewGigNearbyPush(JobsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from ambassadors.models import PushDevice

        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Proximity Co")

        self.state_ca = State.objects.create(
            name="California", code="CA", created_by=self.get_system_user()
        )

        # Event in San Francisco (downtown). All distances are measured
        # from this point.
        self.sf_coords = [37.7749, -122.4194]
        self.event = self.create_event(
            name="Whole Foods SF Demo",
            tenant=self.tenant,
            address="1 Market St, San Francisco, CA",
            coordinates=self.sf_coords,
            state=self.state_ca,
        )
        self.job_title = self.create_job_title(name="Brand Ambassador", tenant=self.tenant)
        # Posted + open-to-all (favorites_only=False) so the favorites gate
        # doesn't filter anyone out — we're testing distance/fallback here.
        self.job = self.create_job(
            name="In-Store Sampling",
            code="JOB-NEAR-001",
            address="1 Market St",
            event=self.event,
            job_title=self.job_title,
            tenant=self.tenant,
            lifecycle_status=models.Job.STATUS_POSTED,
            favorites_only=False,
            public=True,
        )

        self._device_counter = 0

        def make_ba(username, **amb_kwargs):
            user = self.create_user(
                username=username,
                email=username,
                role=self.roles["ambassador"],
                password="testpass123",
            )
            self.create_tenanted_user(user=user, tenant=self.tenant)
            amb = self.create_ambassador(user=user, **amb_kwargs)
            self._device_counter += 1
            # Every BA in these tests has an active push device (reachable).
            PushDevice.objects.create(
                user=user,
                token=f"ExponentPushToken[near-{self._device_counter}]",
                platform="ios",
                is_active=True,
            )
            return user, amb

        self.make_ba = make_ba

        # ~13 mi south of SF (San Mateo-ish): within 30mi -> notified.
        self.near_user, self.near_amb = make_ba(
            "near@test.com", coordinates=[37.5630, -122.3255]
        )
        # ~90 mi away (Sacramento): beyond 30mi -> NOT notified.
        self.far_user, self.far_amb = make_ba(
            "far@test.com", coordinates=[38.5816, -121.4944]
        )
        # Within 30mi but already applied -> skipped.
        self.applied_user, self.applied_amb = make_ba(
            "applied@test.com", coordinates=[37.8044, -122.2712]  # Oakland ~8mi
        )
        models.JobApplication.objects.create(
            tenant=self.tenant,
            job=self.job,
            ambassador=self.applied_amb,
        )
        # No coordinates (NULL/empty) but preferred state CA matches the
        # event state -> state fallback fires.
        self.fallback_user, self.fallback_amb = make_ba(
            "fallback@test.com", coordinates=[]
        )
        models.AmbassadorJobPreference.objects.create(
            ambassador=self.fallback_amb,
            notify_new_gigs=True,
            preferred_state_codes=["CA"],
        )

    def test_proximity_and_fallback(self):
        from jobs.notifications import _notify

        with patch("ambassadors.push.enqueue_push") as mock_push:
            sent = _notify(self.job)

        # Notified: near (proximity) + fallback (state). NOT: far, applied.
        notified_user_ids = {call.args[0] for call in mock_push.call_args_list}

        assert self.near_user.id in notified_user_ids
        assert self.fallback_user.id in notified_user_ids
        assert self.far_user.id not in notified_user_ids
        assert self.applied_user.id not in notified_user_ids
        assert sent == 2
        assert mock_push.call_count == 2

        # Each BA pushed at most once (dedupe).
        assert len(mock_push.call_args_list) == len(notified_user_ids)

        # Payload shape per the mobile contract.
        by_user = {call.args[0]: call.kwargs for call in mock_push.call_args_list}

        near_kwargs = by_user[self.near_user.id]
        assert near_kwargs["title"] == "New gig near you"
        assert near_kwargs["data"]["screen"] == "jobs"
        assert near_kwargs["data"]["kind"] == "new_gig_nearby"
        assert near_kwargs["data"]["jobUuid"] == str(self.job.uuid)
        # Proximity match carries an integer distance ~13mi.
        assert "distanceMiles" in near_kwargs["data"]
        assert isinstance(near_kwargs["data"]["distanceMiles"], int)
        assert 5 <= near_kwargs["data"]["distanceMiles"] <= 20
        assert "mi away" in near_kwargs["body"]

        # Fallback match: no distanceMiles, state-flavored copy.
        fb_kwargs = by_user[self.fallback_user.id]
        assert "distanceMiles" not in fb_kwargs["data"]
        assert fb_kwargs["data"]["jobUuid"] == str(self.job.uuid)
        assert "CA" in fb_kwargs["body"]

    def test_notify_new_gigs_false_opts_out(self):
        from jobs.notifications import _notify

        # Turn the near BA's master switch off — they should be skipped even
        # though they're within range.
        models.AmbassadorJobPreference.objects.create(
            ambassador=self.near_amb,
            notify_new_gigs=False,
        )

        with patch("ambassadors.push.enqueue_push") as mock_push:
            _notify(self.job)

        notified_user_ids = {call.args[0] for call in mock_push.call_args_list}
        assert self.near_user.id not in notified_user_ids
        # Fallback BA still notified.
        assert self.fallback_user.id in notified_user_ids

    def test_favorites_only_gate(self):
        """A favorites_only job only reaches BAs on the tenant's favorites."""
        from jobs.notifications import _notify

        self.job.favorites_only = True
        self.job.save(update_fields=["favorites_only"])

        # Put only the near BA on the favorites roster.
        models.TenantFavoriteAmbassador.objects.create(
            tenant=self.tenant,
            ambassador=self.near_amb,
            added_by=self.get_system_user(),
        )

        with patch("ambassadors.push.enqueue_push") as mock_push:
            _notify(self.job)

        notified_user_ids = {call.args[0] for call in mock_push.call_args_list}
        # Near BA is favorited + in range -> notified.
        assert self.near_user.id in notified_user_ids
        # Fallback BA is NOT favorited -> gated out despite state match.
        assert self.fallback_user.id not in notified_user_ids
        # Far + applied still excluded.
        assert self.far_user.id not in notified_user_ids
        assert self.applied_user.id not in notified_user_ids

    def test_public_fn_enqueues_and_does_not_fan_out_inline(self):
        """The post path must NOT run the fan-out inline.

        `notify_nearby_bas_of_new_gig` should enqueue exactly one RQ task
        (the worker entrypoint + job.id) and return without touching
        `_notify` / `enqueue_push` synchronously. That's the whole point of
        the freeze fix: the postJob mutation can't block on the fan-out.
        """
        from jobs import notifications

        with patch("utils.queues.Queues") as mock_queues, patch.object(
            notifications, "_notify"
        ) as mock_notify, patch("ambassadors.push.enqueue_push") as mock_push:
            add = mock_queues.return_value.default.add
            result = notifications.notify_nearby_bas_of_new_gig(self.job)

        # Enqueued exactly once, with the worker fn + the int job id.
        add.assert_called_once_with(
            notifications._run_notify_nearby_bas_task, self.job.id
        )
        # Fan-out did NOT run inline in the request path.
        mock_notify.assert_not_called()
        mock_push.assert_not_called()
        # Public fn no longer returns a count — it's fire-and-forget.
        assert result is None

    def test_public_fn_swallows_enqueue_failure_no_inline_fallback(self):
        """If the enqueue raises, swallow it (log) and do NOT fan out inline.

        A queue/Redis outage must not block the post and must not silently
        degrade into the old inline fan-out — the daily digest still covers
        these BAs.
        """
        from jobs import notifications

        with patch("utils.queues.Queues") as mock_queues, patch.object(
            notifications, "_notify"
        ) as mock_notify, patch("ambassadors.push.enqueue_push") as mock_push:
            mock_queues.return_value.default.add.side_effect = RuntimeError(
                "redis down"
            )
            # Must not raise — the post path stays alive.
            result = notifications.notify_nearby_bas_of_new_gig(self.job)

        # Never fell back to running the fan-out inline.
        mock_notify.assert_not_called()
        mock_push.assert_not_called()
        assert result is None

    def test_worker_task_refetches_and_fans_out(self):
        """The worker entrypoint re-fetches the job by id and runs `_notify`."""
        from jobs import notifications

        with patch.object(
            notifications, "_notify", return_value=7
        ) as mock_notify:
            sent = notifications._run_notify_nearby_bas_task(self.job.id)

        assert sent == 7
        mock_notify.assert_called_once()
        # It re-fetched a real Job instance (not the int) for the same job.
        passed_job = mock_notify.call_args.args[0]
        assert passed_job.id == self.job.id

    def test_worker_task_missing_job_is_swallowed(self):
        """A job deleted before the worker runs is logged, not crashed."""
        from jobs import notifications

        with patch.object(notifications, "_notify") as mock_notify:
            sent = notifications._run_notify_nearby_bas_task(2_000_000_001)

        assert sent == 0
        mock_notify.assert_not_called()
