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
