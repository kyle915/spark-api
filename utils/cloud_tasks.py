"""Best-effort Cloud Tasks enqueue for offloading slow approval work (REST).

The slow part of approving a recap is the client/RMM notification email plus
the recap-PDF generation. When this feature is configured we hand that work to
a Google Cloud Tasks queue so the GraphQL `approve*` mutation returns instantly;
the queue then calls our secret-protected handler (see `tasks/views.py`), which
runs the same notify code in the background.

Auth + dependency posture mirrors `receipts/ocr.py`: we call the Cloud Tasks
REST API with Application Default Credentials via
`google.auth.transport.requests.AuthorizedSession` — the SAME service-account
auth GCS already uses on Cloud Run — so there is NO extra Python dependency
(no `google-cloud-tasks`) and no API key to manage.

FEATURE FLAG / safety: the feature is OFF unless ALL of CLOUD_TASKS_QUEUE,
CLOUD_TASKS_LOCATION, CLOUD_TASKS_HANDLER_BASE_URL and CLOUD_TASKS_SECRET are
non-empty (the project comes from the existing GS_PROJECT_ID). `enqueue()`
returns False the instant the feature is off, and ANY error while enqueuing
also degrades to False — so the caller always falls back to running the work
inline, exactly as it did before this feature existed. `enqueue()` therefore
never raises and only returns True on a confirmed 2xx CreateTask response.
"""

from __future__ import annotations

import base64
import json
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

_CLOUD_TASKS_HOST = "https://cloudtasks.googleapis.com"
_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
_TIMEOUT_SECONDS = 10


def _is_enabled() -> bool:
    """The feature is ON only when all four vars (plus the project) are set."""
    return all(
        bool(getattr(settings, name, ""))
        for name in (
            "GS_PROJECT_ID",
            "CLOUD_TASKS_QUEUE",
            "CLOUD_TASKS_LOCATION",
            "CLOUD_TASKS_HANDLER_BASE_URL",
            "CLOUD_TASKS_SECRET",
        )
    )


def enqueue(path: str, payload: dict) -> bool:
    """Enqueue a Cloud Task that will POST `payload` (as JSON) to `path`.

    `path` is appended to CLOUD_TASKS_HANDLER_BASE_URL to form the task's
    target URL (e.g. "/api/tasks/recap-approved-notify"). The task carries the
    shared secret in the `X-Tasks-Secret` header so the handler can authorize
    it.

    Returns True only on a confirmed 2xx from the CreateTask call. Returns
    False immediately when the feature is off, and False on ANY error — so the
    caller can safely fall back to inline execution. Never raises.
    """
    if not _is_enabled():
        return False

    try:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        project = settings.GS_PROJECT_ID
        location = settings.CLOUD_TASKS_LOCATION
        queue = settings.CLOUD_TASKS_QUEUE
        base_url = settings.CLOUD_TASKS_HANDLER_BASE_URL.rstrip("/")

        endpoint = (
            f"{_CLOUD_TASKS_HOST}/v2/projects/{project}"
            f"/locations/{location}/queues/{queue}/tasks"
        )
        body_bytes = json.dumps(payload).encode("utf-8")
        task = {
            "task": {
                "httpRequest": {
                    "url": base_url + path,
                    "httpMethod": "POST",
                    "headers": {
                        "Content-Type": "application/json",
                        "X-Tasks-Secret": settings.CLOUD_TASKS_SECRET,
                    },
                    "body": base64.b64encode(body_bytes).decode("ascii"),
                }
            }
        }

        creds, _ = google.auth.default(scopes=_SCOPES)
        session = AuthorizedSession(creds)
        resp = session.post(endpoint, json=task, timeout=_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 — degrade to inline, never crash approve
        logger.warning("cloud_tasks.enqueue failed for %s: %s", path, exc)
        return False

    if 200 <= resp.status_code < 300:
        return True
    logger.warning(
        "cloud_tasks.enqueue got non-2xx for %s: %s %s",
        path,
        resp.status_code,
        (resp.text or "")[:500],
    )
    return False
