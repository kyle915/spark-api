"""A deliberately narrow Close CRM client: read leads, write NOTES and DRAFT emails.

Close uses HTTP Basic auth with the API key as the username
(https://developer.close.com/). The key comes only from
``settings.CLOSE_API_KEY``; it is never logged, echoed or put in an error.

What this client can do to Close, and nothing else:

* ``GET`` leads, notes and opportunities;
* ``POST /activity/note/`` — a note on a lead;
* ``POST /activity/email/`` with ``status: "draft"`` — a draft a rep opens,
  edits and sends from Close themselves.

There is no method that updates or deletes anything, and :meth:`_request`
refuses every method other than GET and POST, any POST outside the two paths
above, and any email whose status is not ``draft`` — so a bug elsewhere
cannot turn into a sent email (in Close, an email is sent by setting its
status to ``outbox``/``scheduled``, or by creating it that way).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

from django.conf import settings

logger = logging.getLogger(__name__)

CLOSE_API_BASE = "https://api.close.com/api/v1"
TIMEOUT_SECONDS = 30
MAX_429_RETRIES = 3

_WRITE_PATHS = ("/activity/note/", "/activity/email/")
LEAD_FIELDS = "id,display_name,name,url,description,status_id,status_label,date_created,contacts,custom"


class CloseNotConfigured(Exception):
    """CLOSE_API_KEY is empty: callers no-op."""


class CloseError(Exception):
    """A Close call failed. The message never contains the key."""


class UnsafeCloseWrite(Exception):
    """A write this client refuses to make (anything but a note or a draft)."""


def close_configured() -> bool:
    return bool((getattr(settings, "CLOSE_API_KEY", "") or "").strip())


def _assert_safe(method: str, path: str, payload: dict | None) -> None:
    method = method.upper()
    if method == "GET":
        return
    if method != "POST":
        raise UnsafeCloseWrite(f"{method} is not allowed")
    if path not in _WRITE_PATHS:
        raise UnsafeCloseWrite(f"POST {path} is not allowed")
    if path == "/activity/email/":
        status = (payload or {}).get("status")
        if status != "draft":
            raise UnsafeCloseWrite("emails are created as drafts only")
        for key in ("send_as_id", "date_scheduled", "followup_sequence_id", "template_id"):
            if key in (payload or {}):
                raise UnsafeCloseWrite(f"{key} is not allowed on a draft")


class CloseClient:
    def __init__(self, api_key: str | None = None, *, session=None):
        key = (api_key if api_key is not None else getattr(settings, "CLOSE_API_KEY", "")) or ""
        key = key.strip()
        if not key:
            raise CloseNotConfigured("CLOSE_API_KEY is not set")
        if session is None:
            import requests

            session = requests.Session()
        self._session = session
        self._session.auth = (key, "")
        self.requests = 0
        self.writes = 0

    # ── transport ───────────────────────────────────────────────────────

    def _request(self, method: str, path: str, *, params=None, json=None) -> dict:
        _assert_safe(method, path, json)
        url = CLOSE_API_BASE + path
        for attempt in range(MAX_429_RETRIES + 1):
            self.requests += 1
            try:
                resp = self._session.request(
                    method.upper(), url, params=params, json=json, timeout=TIMEOUT_SECONDS
                )
            except Exception as exc:  # noqa: BLE001 — never surface request details
                raise CloseError(f"Close request failed ({type(exc).__name__})") from None
            status = getattr(resp, "status_code", 0)
            if status == 429 and attempt < MAX_429_RETRIES:
                try:
                    wait = float(resp.headers.get("ratelimit-reset", "2"))
                except (TypeError, ValueError):
                    wait = 2.0
                time.sleep(min(wait + 0.25, 10))
                continue
            if status >= 400:
                raise CloseError(f"Close returned HTTP {status} for {method.upper()} {path}")
            if method.upper() == "POST":
                self.writes += 1
            try:
                return resp.json() if getattr(resp, "content", b"") else {}
            except ValueError:
                raise CloseError("Close returned invalid JSON") from None
        raise CloseError("Close rate limit")

    # ── reads ───────────────────────────────────────────────────────────

    def get_lead(self, lead_id: str) -> dict:
        return self._request("GET", f"/lead/{lead_id}/", params={"_fields": LEAD_FIELDS})

    def recent_leads(self, *, limit: int = 100) -> list[dict]:
        """The newest leads, newest first (``_order_by=-date_created``)."""
        data = self._request(
            "GET",
            "/lead/",
            params={"_order_by": "-date_created", "_limit": limit, "_fields": LEAD_FIELDS},
        )
        return list(data.get("data") or [])

    def search_leads(self, text: str, *, limit: int = 5) -> list[dict]:
        data = self._request(
            "GET",
            "/lead/",
            params={"query": text, "_limit": limit, "_fields": "id,display_name,name,url,status_label"},
        )
        return list(data.get("data") or [])

    def lead_notes(self, lead_id: str, *, limit: int = 100) -> list[dict]:
        data = self._request(
            "GET",
            "/activity/note/",
            params={"lead_id": lead_id, "_limit": limit, "_fields": "id,note"},
        )
        return list(data.get("data") or [])

    def lead_opportunities(self, lead_id: str) -> list[dict]:
        data = self._request(
            "GET",
            "/opportunity/",
            params={
                "lead_id": lead_id,
                "_fields": "id,status_label,status_type,date_won,expected_value,note",
            },
        )
        return list(data.get("data") or [])

    # ── writes (note + DRAFT email only) ────────────────────────────────

    def create_note(self, lead_id: str, text: str) -> dict:
        return self._request("POST", "/activity/note/", json={"lead_id": lead_id, "note": text})

    def create_draft_email(
        self,
        lead_id: str,
        *,
        to: list[str],
        subject: str,
        body_text: str,
        contact_id: str | None = None,
    ) -> dict:
        payload = {
            "lead_id": lead_id,
            "status": "draft",
            "direction": "outgoing",
            "to": list(to),
            "subject": subject,
            "body_text": body_text,
        }
        if contact_id:
            payload["contact_id"] = contact_id
        return self._request("POST", "/activity/email/", json=payload)


def parse_close_datetime(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def first_contact_email(lead: dict) -> tuple[str, str | None, str]:
    """``(email, contact_id, first_name)`` of the first contact with an email."""
    for contact in lead.get("contacts") or []:
        for item in contact.get("emails") or []:
            email = (item.get("email") or "").strip()
            if email:
                name = (contact.get("name") or contact.get("display_name") or "").strip()
                return email, contact.get("id"), (name.split()[0] if name else "")
    return "", None, ""
