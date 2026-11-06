"""Client event/request notes edits → Ignite ops email (+ Torch Baesha suppress)."""

from types import SimpleNamespace

import pytest
from django.test import override_settings


def test_notes_changed_ignores_whitespace_noop():
    from events.client_notes_notify import notes_changed

    assert notes_changed("hello", "hello") is False
    assert notes_changed("hello", " hello ") is False
    assert notes_changed(None, "") is False
    assert notes_changed("a", "b") is True
    assert notes_changed(None, "x") is True


def test_is_client_user_by_role_slug():
    from events.client_notes_notify import is_client_user

    client = SimpleNamespace(role=SimpleNamespace(slug="client"), role_id=3)
    admin = SimpleNamespace(role=SimpleNamespace(slug="spark-admin"), role_id=2)
    assert is_client_user(client) is True
    assert is_client_user(admin) is False
    assert is_client_user(None) is False


@override_settings(ADMIN_FRONTEND_URL="https://admin.igniteproductions.co")
def test_notify_skips_non_client_and_noop(monkeypatch):
    from events import client_notes_notify as mod

    envelopes = []
    monkeypatch.setattr(
        "utils.mailer.Mailer.send_now",
        lambda self: envelopes.append(self.envelope()),
    )
    monkeypatch.setattr(mod, "_ops_recipient_emails", lambda tenant: ["ops@igniteproductions.co"])

    admin = SimpleNamespace(
        email="kyle@igniteproductions.co",
        role=SimpleNamespace(slug="spark-admin"),
        role_id=2,
        get_full_name=lambda: "Kyle",
    )
    client = SimpleNamespace(
        email="lee@livfamus.com",
        role=SimpleNamespace(slug="client"),
        role_id=3,
        get_full_name=lambda: "Lee",
    )
    tenant = SimpleNamespace(name="FAMUS Energy", slug="famus-energy")
    event = SimpleNamespace(
        id=6711,
        uuid="01a154a6-ca79-703a-bebb-d459b3c05c79",
        address="2926 Nashville Rd",
        date=None,
        retailer=SimpleNamespace(name="Shamrock Liquors"),
        request=None,
        request_id=None,
    )

    assert (
        mod.notify_ops_client_notes_edit(
            user=admin,
            tenant=tenant,
            notes_before="old",
            notes_after="new",
            event=event,
            source="event",
        )
        is False
    )
    assert envelopes == []

    assert (
        mod.notify_ops_client_notes_edit(
            user=client,
            tenant=tenant,
            notes_before="same",
            notes_after=" same ",
            event=event,
            source="event",
        )
        is False
    )
    assert envelopes == []


@override_settings(ADMIN_FRONTEND_URL="https://admin.igniteproductions.co")
def test_notify_sends_before_after_to_ops(monkeypatch):
    from events import client_notes_notify as mod

    envelopes = []
    monkeypatch.setattr(
        "utils.mailer.Mailer.send_now",
        lambda self: envelopes.append(self.envelope()),
    )
    monkeypatch.setattr(
        mod,
        "_ops_recipient_emails",
        lambda tenant: ["kyle@igniteproductions.co", "events@igniteproductions.co"],
    )

    client = SimpleNamespace(
        email="lee@livfamus.com",
        role=SimpleNamespace(slug="client"),
        role_id=3,
        get_full_name=lambda: "Lee Killingsworth",
    )
    tenant = SimpleNamespace(name="FAMUS Energy", slug="famus-energy")
    event = SimpleNamespace(
        id=6711,
        uuid="01a154a6-ca79-703a-bebb-d459b3c05c79",
        address="2926 Nashville Rd",
        date=None,
        retailer=SimpleNamespace(name="Shamrock Liquors"),
        request=SimpleNamespace(id=9001, uuid="01a154a6-ca29-7f2d-b896-b0441ecd9d66"),
        request_id=9001,
    )

    assert (
        mod.notify_ops_client_notes_edit(
            user=client,
            tenant=tenant,
            notes_before="Park out front",
            notes_after="Park out front and thats where we do the activation",
            event=event,
            request=event.request,
            source="event",
        )
        is True
    )
    assert len(envelopes) == 1
    env = envelopes[0]
    assert env.to_emails == [
        "kyle@igniteproductions.co",
        "events@igniteproductions.co",
    ]
    assert "FAMUS Energy" in env.subject
    assert "Shamrock Liquors" in env.subject
    assert "Park out front" in env.html
    assert "thats where we do the activation" in env.html
    assert "lee@livfamus.com" in env.html
    assert (
        "https://admin.igniteproductions.co/event/view/"
        "01a154a6-ca79-703a-bebb-d459b3c05c79"
    ) in env.html
    assert "Event #6711" in env.html
    assert "Request #9001" in env.html


def test_ops_recipients_suppress_baesha_on_torch(monkeypatch):
    from events import client_notes_notify as mod

    monkeypatch.setattr(
        "events.mutations._get_spark_admin_emails",
        lambda: [
            "kyle@igniteproductions.co",
            "baesha@igniteproductions.co",
        ],
    )
    monkeypatch.setattr(
        "events.mutations._get_ignite_domain_emails",
        lambda: [
            "baesha@igniteproductions.co",
            "events@igniteproductions.co",
        ],
    )
    monkeypatch.setattr("events.routing.suppress_cc", lambda emails: list(emails))

    torch = SimpleNamespace(slug="keee-torch-thc", name="Torch", request_url_name="keee-torch-thc")
    famus = SimpleNamespace(slug="famus-energy", name="FAMUS Energy", request_url_name=None)

    torch_emails = {e.lower() for e in mod._ops_recipient_emails(torch)}
    assert "baesha@igniteproductions.co" not in torch_emails
    assert "kyle@igniteproductions.co" in torch_emails
    assert "events@igniteproductions.co" in torch_emails

    famus_emails = {e.lower() for e in mod._ops_recipient_emails(famus)}
    assert "baesha@igniteproductions.co" in famus_emails


@pytest.mark.asyncio
async def test_maybe_notify_skips_admin_without_calling_send(monkeypatch):
    from events import client_notes_notify as mod

    called = []
    monkeypatch.setattr(
        mod,
        "notify_ops_client_notes_edit",
        lambda **kwargs: called.append(kwargs) or True,
    )

    admin = SimpleNamespace(
        email="kyle@igniteproductions.co",
        role=SimpleNamespace(slug="spark-admin"),
        role_id=2,
    )
    await mod.maybe_notify_and_log_client_notes_edit(
        user=admin,
        tenant=SimpleNamespace(name="X", slug="x"),
        notes_before="a",
        notes_after="b",
        source="event",
    )
    assert called == []
