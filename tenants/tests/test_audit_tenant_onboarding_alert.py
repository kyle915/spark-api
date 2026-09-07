"""Onboarding-audit --notify must send via the house Resend mailer.

It used django.core.mail.EmailMessage → SMTP on localhost:1025, which does
not exist on Cloud Run — every --notify run died with ConnectionRefusedError
and the ERROR-level logger.exception paged via ErrorEventLogHandler
(2026-09-07).
"""

from tenants.management.commands.audit_tenant_onboarding import Command


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

    cmd = Command()
    cmd._report = ["Tenant onboarding audit — mode=REPORT-ONLY.", "  gap line"]
    ok = cmd._send_alert(2, 1)

    assert ok is True
    assert "2 tenant(s) with seed gaps" in sent["subject"]
    assert "1 cross-tenant recap file(s)" in sent["subject"]
    assert sent["to"] == ["ops@igniteproductions.co"]
    assert "gap line" in sent["html"]


def test_alert_skips_when_no_recipients(monkeypatch):
    monkeypatch.setattr("tenants.support._resolve_ignite_recipients", lambda: [])

    def boom(self):  # pragma: no cover - guard
        raise AssertionError("must not send with empty recipients")

    monkeypatch.setattr("utils.mailer.Mailer.send_now", boom)

    cmd = Command()
    cmd._report = []
    assert cmd._send_alert(1, 0) is False


def test_alert_soft_fails_without_raising(monkeypatch):
    monkeypatch.setattr(
        "tenants.support._resolve_ignite_recipients",
        lambda: ["ops@igniteproductions.co"],
    )

    def boom(self):
        raise ConnectionRefusedError("[Errno 61] Connection refused")

    monkeypatch.setattr("utils.mailer.Mailer.send_now", boom)

    cmd = Command()
    cmd._report = ["report"]
    assert cmd._send_alert(1, 0) is False
