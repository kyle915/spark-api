"""Tests for per-category push opt-outs (PushPreference gating in
ambassadors.push.send_push_to_user)."""

import pytest
from unittest.mock import AsyncMock

from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from ambassadors.models import PushDevice, PushNotification, PushPreference
from ambassadors.push import _push_category, send_push_to_user
from utils.expo_push import ExpoPushTicket

User = get_user_model()


@pytest.fixture
def user(db):
    from tenants.models import Role

    role, _ = Role.objects.get_or_create(
        slug=Role.AMBASSADOR_SLUG, defaults={"name": "Ambassador"}
    )
    return User.objects.create_user(
        username="ba-pref", email="ba-pref@example.com", role=role
    )


@pytest.fixture
def devices(user):
    PushDevice.objects.create(
        user=user, token="ExponentPushToken[ios-pref]", platform="ios"
    )
    PushDevice.objects.create(
        user=user, token="ExponentPushToken[and-pref]", platform="android"
    )


def _ok_client(n):
    c = AsyncMock()
    c.send.return_value = [ExpoPushTicket(status="ok", id=f"r-{i}") for i in range(n)]
    return c


# ── _push_category mapping ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "data,expected",
    [
        ({"type": "shift_offer", "ambassadorEventUuid": "x"}, "shift_offers"),
        ({"kind": "chat", "screen": "chat", "threadUuid": "x"}, "chat"),
        ({"kind": "payment"}, "pay"),
        ({"screen": "earnings", "paymentId": 1}, "pay"),
        ({"kind": "new_gig_nearby"}, "gigs"),
        ({"kind": "activation_reminder", "screen": "shifts"}, "reminders"),
        ({"screen": "recap", "eventUuid": "x"}, "reminders"),
        ({"kind": "pre_shift_checklist"}, "reminders"),
        # Transactional / unknown → not gated.
        ({"kind": "shift_dropped", "screen": "today"}, None),
        ({"kind": "job_assigned"}, None),
        ({"kind": "shift_cancelled"}, None),
        ({"screen": "shifts"}, None),
        ({}, None),
        (None, None),
    ],
)
def test_push_category_mapping(data, expected):
    assert _push_category(data) == expected


# ── gating behaviour ────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_muted_category_suppresses_send_but_records_inbox(user, devices):
    """Muting `chat` should stop the push banner but still log the inbox row."""
    await sync_to_async(PushPreference.objects.create)(user=user, chat=False)

    client = _ok_client(2)
    ok = await send_push_to_user(
        user,
        title="New message",
        body="hey",
        data={"kind": "chat", "screen": "chat", "threadUuid": "t1"},
        client=client,
    )

    assert ok == 0  # suppressed — no device send
    client.send.assert_not_called()
    # …but the inbox record is still written (history preserved).
    rows = await sync_to_async(
        lambda: list(PushNotification.objects.filter(user=user))
    )()
    assert len(rows) == 1
    assert rows[0].kind == "chat"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_other_category_still_sends_when_one_is_muted(user, devices):
    """Muting `chat` must not affect an unrelated category (shift offers)."""
    await sync_to_async(PushPreference.objects.create)(user=user, chat=False)

    client = _ok_client(2)
    ok = await send_push_to_user(
        user,
        title="New shift offered",
        body="Tap to accept",
        data={"type": "shift_offer", "ambassadorEventUuid": "ae1"},
        client=client,
    )

    assert ok == 2  # not muted → delivered
    client.send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_no_preference_row_sends_everything(user, devices):
    """A user who never set preferences gets all categories (default on)."""
    client = _ok_client(2)
    ok = await send_push_to_user(
        user,
        title="Payment sent",
        body="$120 on the way",
        data={"kind": "payment"},
        client=client,
    )
    assert ok == 2  # default = on
