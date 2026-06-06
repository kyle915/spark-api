"""Tests for ambassadors.push.send_push_to_user."""

import pytest
from unittest.mock import AsyncMock, patch

from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from ambassadors.models import PushDevice
from ambassadors.push import send_push_to_user
from utils.expo_push import ExpoPushError, ExpoPushTicket

User = get_user_model()


@pytest.fixture
def user(db):
    # User.role is a non-nullable FK (Role, on_delete=RESTRICT), so a bare
    # create_user() violates the NOT NULL constraint. Attach an ambassador
    # Role (these are BA push tests).
    from tenants.models import Role

    role, _ = Role.objects.get_or_create(
        slug=Role.AMBASSADOR_SLUG, defaults={"name": "Ambassador"}
    )
    return User.objects.create_user(
        username="ba-push", email="ba-push@example.com", role=role
    )


@pytest.fixture
def devices(user):
    ios = PushDevice.objects.create(
        user=user, token="ExponentPushToken[ios-aaa]", platform="ios"
    )
    android = PushDevice.objects.create(
        user=user, token="ExponentPushToken[android-bbb]", platform="android"
    )
    return ios, android


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_send_push_to_user_fans_out_per_device(user, devices):
    ios, android = devices

    fake_client = AsyncMock()
    fake_client.send.return_value = [
        ExpoPushTicket(status="ok", id="recv-1"),
        ExpoPushTicket(status="ok", id="recv-2"),
    ]

    ok = await send_push_to_user(
        user,
        title="Heads up",
        body="Your shift starts in 15 min",
        data={"screen": "shifts"},
        client=fake_client,
    )

    assert ok == 2
    fake_client.send.assert_awaited_once()
    sent = fake_client.send.await_args.args[0]
    # Both devices get a message; send order isn't guaranteed, so compare
    # the token sets rather than a positional list.
    tokens = sorted(m.to for m in sent)
    assert tokens == sorted([ios.token, android.token])
    # Android message carries the channel id; iOS doesn't.
    by_platform = {m.to: m for m in sent}
    assert by_platform[android.token].channel_id == "default"
    assert by_platform[ios.token].channel_id is None


# ── Notifications inbox: send-side recording + read scoping ──────────────


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_send_push_records_notification_even_with_no_devices(user):
    """The in-app inbox must reflect everything we sent, even when the BA has
    no reachable device — so the record is written before the device check."""
    from ambassadors.models import PushNotification

    fake_client = AsyncMock()  # never called (no devices)
    ok = await send_push_to_user(
        user,
        title="Payment sent",
        body="$120 is on the way",
        data={"kind": "payment"},
        client=fake_client,
    )
    assert ok == 0  # no devices → nothing delivered
    rows = await sync_to_async(
        lambda: list(PushNotification.objects.filter(user=user))
    )()
    assert len(rows) == 1
    assert rows[0].title == "Payment sent"
    assert rows[0].kind == "payment"
    assert rows[0].read_at is None  # starts unread


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_send_push_records_kind_falls_back_to_screen(user):
    from ambassadors.models import PushNotification

    fake_client = AsyncMock()
    await send_push_to_user(
        user, title="t", body="b", data={"screen": "shifts"}, client=fake_client
    )
    row = await sync_to_async(
        lambda: PushNotification.objects.filter(user=user).first()
    )()
    assert row is not None
    assert row.kind == "shifts"  # no `kind` → derived from `screen`
    assert row.data == {"screen": "shifts"}


@pytest.mark.django_db
def test_mark_notifications_read_scopes_to_user_and_unread():
    """The mark-read query must only ever touch the caller's own UNREAD rows."""
    from django.utils import timezone
    from ambassadors.models import PushNotification
    from tenants.models import Role

    role, _ = Role.objects.get_or_create(
        slug=Role.AMBASSADOR_SLUG, defaults={"name": "Ambassador"}
    )
    me = User.objects.create_user(username="me", email="me@x.com", role=role)
    other = User.objects.create_user(username="other", email="other@x.com", role=role)

    mine_unread = PushNotification.objects.create(user=me, title="a")
    mine_read = PushNotification.objects.create(
        user=me, title="b", read_at=timezone.now()
    )
    theirs = PushNotification.objects.create(user=other, title="c")

    # Mirror the mutation's _mark: caller-scoped, unread-only, mark-all.
    marked = PushNotification.objects.filter(
        user=me, read_at__isnull=True
    ).update(read_at=timezone.now())

    assert marked == 1  # only mine_unread
    mine_unread.refresh_from_db()
    theirs.refresh_from_db()
    assert mine_unread.read_at is not None
    assert theirs.read_at is None  # other user's row untouched


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_send_push_deactivates_invalid_tokens(user, devices):
    ios, android = devices

    fake_client = AsyncMock()
    fake_client.send.return_value = [
        ExpoPushTicket(status="ok", id="recv-1"),
        ExpoPushTicket(
            status="error",
            message="...",
            details={"error": "DeviceNotRegistered"},
        ),
    ]

    ok = await send_push_to_user(user, title="t", body="b", client=fake_client)
    assert ok == 1

    refreshed = await sync_to_async(
        lambda: list(PushDevice.objects.filter(user=user).order_by("id"))
    )()
    by_token = {d.token: d for d in refreshed}
    # First message was ios → ok → still active. Second was android → bad → deactivated.
    assert by_token[ios.token].is_active is True
    assert by_token[android.token].is_active is False


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_send_push_no_devices_is_noop(user):
    fake_client = AsyncMock()
    ok = await send_push_to_user(user, title="t", body="b", client=fake_client)
    assert ok == 0
    fake_client.send.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_send_push_swallows_relay_errors(user, devices):
    fake_client = AsyncMock()
    fake_client.send.side_effect = ExpoPushError("relay 500")

    ok = await send_push_to_user(user, title="t", body="b", client=fake_client)
    assert ok == 0  # never raises
