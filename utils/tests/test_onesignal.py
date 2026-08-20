"""OneSignal client: expected no-subscription payloads must not raise."""

import json

import httpx
import pytest

from utils.onesignal import (
    OneSignalClient,
    OneSignalError,
    is_no_subscription_error,
    onesignal_delivered,
)


class FakeAsyncClient:
    def __init__(self, response: httpx.Response):
        self._response = response
        self.calls: list[tuple[str, dict | None]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, path, json=None, headers=None):
        self.calls.append((path, json))
        return self._response


def _response(status: int, payload: dict) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        request=httpx.Request("POST", "https://api.onesignal.com/notifications"),
    )


class TestNoSubscriptionClassification:
    def test_invalid_aliases_dict(self):
        assert is_no_subscription_error(
            {"invalid_aliases": {"external_id": ["abc"]}}
        )

    def test_not_subscribed_list(self):
        assert is_no_subscription_error(
            ["All included players are not subscribed"]
        )

    def test_real_outage_is_not_expected(self):
        assert is_no_subscription_error("Internal Server Error") is False
        assert is_no_subscription_error({"app_id": "is required"}) is False


class TestOnesignalDelivered:
    def test_recipients(self):
        assert onesignal_delivered({"id": "n1", "recipients": 2}) is True
        assert onesignal_delivered({"id": "n1", "recipients": 0}) is False

    def test_invalid_aliases_not_delivered(self):
        assert (
            onesignal_delivered(
                {"id": "", "errors": {"invalid_aliases": {"external_id": ["x"]}}}
            )
            is False
        )

    def test_mock_success_passthrough(self):
        assert onesignal_delivered(object()) is True
        assert onesignal_delivered(None) is False


@pytest.fixture
def onesignal_settings(settings):
    settings.ONESIGNAL_APP_ID = "app-id"
    settings.ONESIGNAL_REST_API_KEY = "rest-key"
    settings.ONESIGNAL_API_URL = "https://api.onesignal.com"
    settings.ONESIGNAL_TARGET_CHANNEL = "push"
    settings.ONESIGNAL_TIMEOUT_SECONDS = 10.0


class TestSendPush:
    @pytest.mark.asyncio
    async def test_success(self, monkeypatch, onesignal_settings):
        captured = FakeAsyncClient(_response(200, {"id": "n1", "recipients": 1}))
        monkeypatch.setattr(
            "utils.onesignal.httpx.AsyncClient", lambda **kwargs: captured
        )
        body = await OneSignalClient().send_push(
            external_ids=["user-1"],
            title="t",
            message="m",
        )
        assert body["id"] == "n1"
        assert captured.calls[0][0] == "/notifications"
        assert captured.calls[0][1]["include_aliases"] == {
            "external_id": ["user-1"]
        }

    @pytest.mark.asyncio
    async def test_invalid_aliases_returns_quietly(self, monkeypatch, onesignal_settings):
        payload = {"id": "", "errors": {"invalid_aliases": {"external_id": ["user-1"]}}}
        monkeypatch.setattr(
            "utils.onesignal.httpx.AsyncClient",
            lambda **kwargs: FakeAsyncClient(_response(200, payload)),
        )
        body = await OneSignalClient().send_push(
            external_ids=["user-1"],
            title="t",
            message="m",
        )
        assert body["errors"]["invalid_aliases"]

    @pytest.mark.asyncio
    async def test_http_400_not_subscribed_returns_quietly(
        self, monkeypatch, onesignal_settings
    ):
        payload = {"errors": ["All included players are not subscribed"]}
        monkeypatch.setattr(
            "utils.onesignal.httpx.AsyncClient",
            lambda **kwargs: FakeAsyncClient(_response(400, payload)),
        )
        body = await OneSignalClient().send_push(
            external_ids=["user-1"],
            title="t",
            message="m",
        )
        assert "not subscribed" in json.dumps(body["errors"])

    @pytest.mark.asyncio
    async def test_http_500_still_raises(self, monkeypatch, onesignal_settings):
        monkeypatch.setattr(
            "utils.onesignal.httpx.AsyncClient",
            lambda **kwargs: FakeAsyncClient(
                _response(500, {"errors": ["Internal Server Error"]})
            ),
        )
        with pytest.raises(OneSignalError, match="status 500"):
            await OneSignalClient().send_push(
                external_ids=["user-1"],
                title="t",
                message="m",
            )

    @pytest.mark.asyncio
    async def test_unrelated_body_errors_still_raise(
        self, monkeypatch, onesignal_settings
    ):
        monkeypatch.setattr(
            "utils.onesignal.httpx.AsyncClient",
            lambda **kwargs: FakeAsyncClient(
                _response(200, {"errors": ["app_id is required"]})
            ),
        )
        with pytest.raises(OneSignalError, match="app_id is required"):
            await OneSignalClient().send_push(
                external_ids=["user-1"],
                title="t",
                message="m",
            )
