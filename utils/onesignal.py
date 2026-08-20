import json
import logging

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

# OneSignal treats "this alias has no subscribed player" as an error payload
# even on HTTP 200. Those are expected for BAs who never installed the app
# (or never called OneSignal.login) — they must not page the error monitor.
_NO_SUBSCRIPTION_MARKERS = (
    "invalid_aliases",
    "not subscribed",
    "no subscribed",
    "no_subscribers",
    "no players",
    "no valid targets",
    "no valid aliases",
    "could not find any",
    "not a valid player",
    "all included players",
    "include_player_ids",
)


class OneSignalError(Exception):
    """Raised when OneSignal returns an error or the client is misconfigured."""


def _flatten_onesignal_detail(detail: object) -> str:
    if detail is None:
        return ""
    if isinstance(detail, str):
        return detail
    try:
        return json.dumps(detail)
    except TypeError:
        return str(detail)


def is_no_subscription_error(detail: object) -> bool:
    """True when OneSignal is saying 'nobody to send to', not a real outage."""
    blob = _flatten_onesignal_detail(detail).lower()
    return any(marker in blob for marker in _NO_SUBSCRIPTION_MARKERS)


def onesignal_delivered(body: object) -> bool:
    """True when the Create Notification response reached at least one device.

    Non-dict return values (test doubles) are treated as delivered so callers
    that mock ``send_push`` keep their existing success path.
    """
    if not isinstance(body, dict):
        return body is not None
    errors = body.get("errors")
    if errors and is_no_subscription_error(errors):
        return False
    recipients = body.get("recipients")
    if recipients is not None:
        try:
            return int(recipients) > 0
        except (TypeError, ValueError):
            return False
    return bool(body.get("id"))


class OneSignalClient:
    async def send_push(
        self,
        *,
        external_ids: list[str],
        title: str,
        message: str,
        url: str | None = None,
        data: dict | None = None,
    ) -> dict:
        if not settings.ONESIGNAL_APP_ID or not settings.ONESIGNAL_REST_API_KEY:
            raise OneSignalError(
                "OneSignal is not configured. Set ONESIGNAL_APP_ID and ONESIGNAL_REST_API_KEY."
            )

        if not external_ids:
            raise OneSignalError("At least one external user id is required.")

        payload = {
            "app_id": settings.ONESIGNAL_APP_ID,
            "target_channel": settings.ONESIGNAL_TARGET_CHANNEL,
            "include_aliases": {"external_id": external_ids},
            "headings": {"en": title},
            "contents": {"en": message},
        }
        if url:
            payload["url"] = url
        if data:
            payload["data"] = data

        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "Authorization": f"Key {settings.ONESIGNAL_REST_API_KEY}",
        }

        async with httpx.AsyncClient(
            base_url=settings.ONESIGNAL_API_URL,
            timeout=settings.ONESIGNAL_TIMEOUT_SECONDS,
        ) as client:
            response = await client.post("/notifications", json=payload, headers=headers)

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text
            if is_no_subscription_error(detail):
                logger.info(
                    "OneSignal has no push subscription for external_ids=%s: %s",
                    external_ids,
                    detail,
                )
                try:
                    return exc.response.json()
                except ValueError:
                    return {"id": "", "recipients": 0, "errors": detail}
            raise OneSignalError(
                f"OneSignal request failed with status {exc.response.status_code}: {detail}"
            ) from exc

        body = response.json()
        errors = body.get("errors")
        if errors:
            if is_no_subscription_error(errors):
                logger.info(
                    "OneSignal has no push subscription for external_ids=%s: %s",
                    external_ids,
                    errors,
                )
                return body
            raise OneSignalError(str(errors))

        return body


one_signal_client = OneSignalClient()
