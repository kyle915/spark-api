"""Tests for the cron staleness watchdog (`check_cron_health`).

Covers the decision logic (overdue / errored / healthy / never-seen),
the per-cron throttle, and dry-run safety. The mailer + Ignite-recipient
resolver are patched so no real email is smoked; we assert on whether an
alert would have been dispatched and whether last_alerted_at was stamped.
"""

from __future__ import annotations

import datetime
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone


@pytest.fixture
def capture_alert(monkeypatch):
    """Patch the alert rails: fake Ignite recipients + a send_now spy.
    Returns the list of send_now calls (one per digest email)."""
    import tenants.support
    from utils.mailer import Mailer

    calls: list[int] = []
    monkeypatch.setattr(
        tenants.support,
        "_resolve_ignite_recipients",
        lambda: ["ops@igniteproductions.co"],
    )
    monkeypatch.setattr(Mailer, "send_now", lambda self: calls.append(1))
    return calls


def _run(**flags) -> str:
    out = StringIO()
    args = []
    if flags.get("dry_run"):
        args.append("--dry-run")
    if flags.get("alert_never_seen"):
        args.append("--alert-never-seen")
    if "throttle_hours" in flags:
        args += ["--throttle-hours", str(flags["throttle_hours"])]
    call_command("check_cron_health", *args, stdout=out)
    return out.getvalue()


@pytest.mark.django_db
class TestCheckCronHealth:
    def _seed(self, name, *, hours_ago, ok, alerted_hours_ago=None):
        from digest.models import CronRun

        now = timezone.now()
        return CronRun.objects.create(
            name=name,
            last_run_at=now - datetime.timedelta(hours=hours_ago),
            last_status=200 if ok else 500,
            last_ok=ok,
            run_count=1,
            last_alerted_at=(
                now - datetime.timedelta(hours=alerted_hours_ago)
                if alerted_hours_ago is not None
                else None
            ),
        )

    def test_overdue_and_errored_alert_healthy_ignored(self, capture_alert):
        # activation-reminders cadence is 1h → 5h ago is overdue.
        overdue = self._seed("activation-reminders", hours_ago=5, ok=True)
        # recap-nudges recent but last run errored (non-2xx).
        errored = self._seed("recap-nudges", hours_ago=0.2, ok=False)
        # send-admin-digest cadence 30h → 2h ago is healthy.
        healthy = self._seed("send-admin-digest", hours_ago=2, ok=True)

        out = _run()

        assert len(capture_alert) == 1  # one digest email dispatched
        overdue.refresh_from_db()
        errored.refresh_from_db()
        healthy.refresh_from_db()
        assert overdue.last_alerted_at is not None
        assert errored.last_alerted_at is not None
        assert healthy.last_alerted_at is None  # healthy never stamped
        assert "activation-reminders" in out
        assert "recap-nudges" in out

    def test_dry_run_sends_nothing(self, capture_alert):
        row = self._seed("activation-reminders", hours_ago=10, ok=True)
        out = _run(dry_run=True)
        assert len(capture_alert) == 0
        row.refresh_from_db()
        assert row.last_alerted_at is None
        assert "DRY-RUN" in out

    def test_throttle_suppresses_repeat(self, capture_alert):
        # Overdue, but alerted 1h ago and throttle is 12h → suppressed.
        self._seed(
            "activation-reminders", hours_ago=10, ok=True, alerted_hours_ago=1
        )
        _run(throttle_hours=12)
        assert len(capture_alert) == 0

    def test_throttle_window_elapsed_realerts(self, capture_alert):
        # Overdue and last alert was 20h ago (> 12h throttle) → re-alerts.
        self._seed(
            "activation-reminders", hours_ago=10, ok=True, alerted_hours_ago=20
        )
        _run(throttle_hours=12)
        assert len(capture_alert) == 1

    def test_never_seen_not_alerted_by_default(self, capture_alert):
        # No CronRun rows at all → nothing to alert on by default.
        out = _run()
        assert len(capture_alert) == 0
        assert "never-seen" in out  # reported, just not alerted

    def test_never_seen_alerts_when_opted_in(self, capture_alert):
        _run(alert_never_seen=True)
        # Every watched cron is missing → one digest with all of them.
        assert len(capture_alert) == 1
