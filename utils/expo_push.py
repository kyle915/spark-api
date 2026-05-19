"""Expo Push relay client.

spark-mobile uses expo-notifications, which yields tokens shaped like
``ExponentPushToken[xxxxxxxxxxxxxxxxxxxxxx]``. To deliver pushes to
those tokens we POST to https://exp.host/--/api/v2/push/send — Expo
then bridges to APNs (iOS) and FCM (Android) under the hood.

This module is the low-level client. The high-level helper that fans
a push out to all of a user's registered devices lives in
``ambassadors/push.py``.

Docs: https://docs.expo.dev/push-notifications/sending-notifications/

Send response (per message), as returned by Expo::

    {"status": "ok", "id": "xxx-yyy"}
    {"status": "error", "message": "...", "details": {"error": "DeviceNotRegistered"}}

When ``details.error == "DeviceNotRegistered"`` the token is permanently
invalid — the caller should deactivate it locally. Other errors
(``MessageTooBig``, ``MessageRateExceeded``, ``MismatchSenderId``,
``InvalidCredentials``) are surfaced verbatim for the caller to log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx
from django.conf import settings


class ExpoPushError(Exception):
    """Raised when the Expo Push API returns an error or is misconfigured."""


@dataclass
class ExpoPushMessage:
    """A single push message bound for one Expo token.

    Mirrors the Expo Push API request shape — see
    https://docs.expo.dev/push-notifications/sending-notifications/#message-request-format
    for the full list of optional fields. We expose the ones we use today
    and keep the dict open via ``extra`` so callers can pass anything we
    haven't surfaced yet (e.g. ``categoryId``, ``mutableContent``).
    """

    to: str
    title: str | None = None
    body: str | None = None
    data: dict[str, Any] | None = None
    sound: str | None = "default"
    badge: int | None = None
    channel_id: str | None = None  # Android only — matches the channel set on the device
    priority: str | None = None  # "default" | "normal" | "high"
    ttl: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"to": self.to}
        if self.title is not None:
            payload["title"] = self.title
        if self.body is not None:
            payload["body"] = self.body
        if self.data is not None:
            payload["data"] = self.data
        if self.sound is not None:
            payload["sound"] = self.sound
        if self.badge is not None:
            payload["badge"] = self.badge
        if self.channel_id is not None:
            payload["channelId"] = self.channel_id
        if self.priority is not None:
            payload["priority"] = self.priority
        if self.ttl is not None:
            payload["ttl"] = self.ttl
        if self.extra:
            payload.update(self.extra)
        return payload


@dataclass
class ExpoPushTicket:
    """One ticket in the Expo response.

    The Expo API returns ``status: "ok"`` with an id for messages it
    accepted, and ``status: "error"`` with a message + details for ones
    it rejected. ``details.error`` is the machine-readable code we
    branch on (e.g. ``DeviceNotRegistered``).
    """

    status: str
    id: str | None = None
    message: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def error_code(self) -> str | None:
        return self.details.get("error") if isinstance(self.details, dict) else None

    @property
    def is_invalid_token(self) -> bool:
        return self.error_code == "DeviceNotRegistered"


class ExpoPushClient:
    """Async client for the Expo Push relay."""

    # Expo accepts up to 100 messages per HTTP request.
    BATCH_SIZE = 100

    async def send(
        self,
        messages: Iterable[ExpoPushMessage],
    ) -> list[ExpoPushTicket]:
        """Send 1..N messages. Tickets are returned in input order."""
        msgs = list(messages)
        if not msgs:
            return []

        headers = {
            "accept": "application/json",
            "accept-encoding": "gzip, deflate",
            "content-type": "application/json",
        }
        token = getattr(settings, "EXPO_PUSH_ACCESS_TOKEN", "") or ""
        if token:
            headers["Authorization"] = f"Bearer {token}"

        base_url = getattr(settings, "EXPO_PUSH_API_URL", "https://exp.host/--/api/v2/push")
        timeout = float(getattr(settings, "EXPO_PUSH_TIMEOUT_SECONDS", 10.0))

        tickets: list[ExpoPushTicket] = []
        async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as client:
            for i in range(0, len(msgs), self.BATCH_SIZE):
                batch = msgs[i : i + self.BATCH_SIZE]
                payload = [m.to_payload() for m in batch]
                try:
                    response = await client.post("/send", json=payload, headers=headers)
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise ExpoPushError(
                        f"Expo Push HTTP {exc.response.status_code}: {exc.response.text}"
                    ) from exc
                except httpx.HTTPError as exc:
                    raise ExpoPushError(f"Expo Push request failed: {exc}") from exc

                body = response.json()
                data = body.get("data")
                if not isinstance(data, list):
                    # Expo can also return errors at the envelope level
                    # (e.g. ``{"errors": [...]}``). Surface them rather
                    # than swallow.
                    if body.get("errors"):
                        raise ExpoPushError(str(body["errors"]))
                    raise ExpoPushError(f"Unexpected Expo Push response: {body!r}")

                for entry in data:
                    if not isinstance(entry, dict):
                        tickets.append(ExpoPushTicket(status="error", message=str(entry)))
                        continue
                    tickets.append(
                        ExpoPushTicket(
                            status=entry.get("status", "error"),
                            id=entry.get("id"),
                            message=entry.get("message"),
                            details=entry.get("details") or {},
                        )
                    )

        return tickets


expo_push_client = ExpoPushClient()
