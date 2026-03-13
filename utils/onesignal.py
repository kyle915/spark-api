import httpx

from django.conf import settings


class OneSignalError(Exception):
    """Raised when OneSignal returns an error or the client is misconfigured."""


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
            raise OneSignalError(
                f"OneSignal request failed with status {exc.response.status_code}: {detail}"
            ) from exc

        body = response.json()
        if body.get("errors"):
            raise OneSignalError(str(body["errors"]))

        return body


one_signal_client = OneSignalClient()
