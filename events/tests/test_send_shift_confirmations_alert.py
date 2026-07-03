"""The unconfirmed-shift alert must send via the house Resend mailer.

It used django.core.mail.EmailMessage → SMTP on localhost, which doesn't
exist on Cloud Run — every alert since the feature shipped died with
ConnectionRefusedError (the error monitor's first-ever catch, 2026-07-03).
"""

from types import SimpleNamespace

import pytest

from events.management.commands.send_shift_confirmations import Command

pytestmark = pytest.mark.django_db


def _fake_row():
    event = SimpleNamespace(
        name="Miami — Brickell · 7/4",
        date=None,
        start_time=None,
        end_time=None,
        timezone=None,
    )
    user = SimpleNamespace(
        email="ba@example.com",
        get_full_name=lambda: "Test BA",
    )
    return SimpleNamespace(
        event_id=1,
        ambassador_id=2,
        event=event,
        ambassador=SimpleNamespace(user=user),
        confirmation_requested_at=None,
    )


def test_alert_sends_via_resend_mailer(monkeypatch):
    sent = {}

    monkeypatch.setattr(
        "tenants.support._resolve_ignite_recipients",
        lambda: ["ops@igniteproductions.co"],
    )

    def fake_send_now(self):
        env = self.envelope()
        sent["subject"] = env.subject
        sent["to"] = list(env.to_emails)
        sent["html"] = env.html

    monkeypatch.setattr("utils.mailer.Mailer.send_now", fake_send_now)

    ok = Command()._send_alert([_fake_row()], dry=False)

    # True = the caller stamps the alert as delivered; False would mean we
    # regressed back into the swallowed-exception path.
    assert ok is True
    assert "1 unconfirmed BA" in sent["subject"]
    assert sent["to"] == ["ops@igniteproductions.co"]
    assert "Test BA" in sent["html"]


def test_dry_run_does_not_send(monkeypatch):
    monkeypatch.setattr(
        "tenants.support._resolve_ignite_recipients",
        lambda: ["ops@igniteproductions.co"],
    )

    def boom(self):  # pragma: no cover - guard
        raise AssertionError("dry-run must not send")

    monkeypatch.setattr("utils.mailer.Mailer.send_now", boom)
    assert Command()._send_alert([_fake_row()], dry=True) is False
