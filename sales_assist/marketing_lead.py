"""Sync self-serve Spark onboarding signups into Close as leads.

Marketing pages (ignite-webflow-assets / spark-by-ignite) send brands to
``/onboarding`` with UTM params. On successful Free-tier workspace create we:

1. Find an existing Close lead by brand name or contact email, or create one.
2. Write a note with the UTM attribution (no auto-emails, no sequences).
3. Link the Spark tenant → Close lead via :class:`CloseLink` so sales-assist
   prospect drafts skip this brand (existing-client path) and renewals can
   attach later.

This is intentionally different from fuzzy ``suggest-links``: the brand just
created their own Spark workspace, so the link is authored by the signup, not
guessed from a name match. Failures never block onboarding.

Callers must invoke this **after** the signup transaction commits (e.g.
``transaction.on_commit``) so Close I/O cannot stall or roll back the DB write
and cannot leave orphaned CRM state when signup fails.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from django.conf import settings

from sales_assist.close_client import (
    CloseClient,
    CloseError,
    CloseNotConfigured,
    UnsafeCloseWrite,
    close_configured,
)
from sales_assist.models import CloseLink

logger = logging.getLogger(__name__)

UTM_KEYS = (
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_id",
    "utm_content",
    "utm_term",
)

NOTE_MARKER = "SPARK SELF-SERVE ONBOARDING"


class CloseLookupError(Exception):
    """Close search/get failed; callers must not treat this as 'no match'."""


def sanitize_utm(raw: dict[str, Any] | None) -> dict[str, str]:
    """Keep only the six approved campaign tags with marketing-safe values."""
    if not raw:
        return {}
    out: dict[str, str] = {}
    for key in UTM_KEYS:
        value = raw.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        # Mirror ignite-webflow-assets ig-routes.js campaignValue rules.
        if not re.match(r"^[a-z0-9][a-z0-9 ._~+-]{0,159}$", text, re.I):
            continue
        if key != "utm_id" and re.match(r"^\+?[\d ().-]{7,}$", text):
            continue
        out[key] = text
    return out


def _format_utm_lines(utm: dict[str, str]) -> str:
    if not utm:
        return "(none captured)"
    return "\n".join(f"  {k}: {utm[k]}" for k in UTM_KEYS if k in utm)


def _build_note(
    *,
    brand_name: str,
    email: str,
    tenant_slug: str,
    tenant_uuid: str,
    utm: dict[str, str],
    landing_path: str,
) -> str:
    lines = [
        NOTE_MARKER,
        "",
        f"Brand: {brand_name}",
        f"Contact email: {email}",
        f"Spark tenant slug: {tenant_slug}",
        f"Spark tenant uuid: {tenant_uuid}",
        f"Landing: {landing_path or '/onboarding'}",
        "",
        "Marketing attribution (UTM):",
        _format_utm_lines(utm),
        "",
        "Source: Spark self-serve Free signup (no card required).",
        "Reps: do not send sales-assist prospect drafts for this lead —",
        "it is already a Spark workspace. Use renewal / check-in drafts once linked.",
    ]
    return "\n".join(lines)


def lead_contact_emails(lead: dict) -> set[str]:
    """Every email on every contact on a Close lead (lowercased)."""
    emails: set[str] = set()
    for contact in lead.get("contacts") or []:
        for item in contact.get("emails") or []:
            email = (item.get("email") or "").strip().lower()
            if email:
                emails.add(email)
    return emails


def _find_existing_lead(close: CloseClient, *, brand_name: str, email: str) -> dict | None:
    """Return a matching lead, ``None`` if none found.

    Raises :class:`CloseLookupError` when Close search/get fails so callers do
    not create a duplicate lead after a transient API error.
    """
    queries = []
    if email:
        queries.append(email)
    if brand_name:
        queries.append(brand_name)
    seen: set[str] = set()
    for q in queries:
        try:
            hits = close.search_leads(q, limit=5)
        except CloseError as exc:
            raise CloseLookupError(f"Close search failed for {q!r}: {exc}") from exc
        for hit in hits:
            lid = hit.get("id") or ""
            if not lid or lid in seen:
                continue
            seen.add(lid)
            try:
                full = close.get_lead(lid)
            except CloseError as exc:
                raise CloseLookupError(f"Close get_lead failed for {lid}: {exc}") from exc
            name = (full.get("display_name") or full.get("name") or "").strip()
            if brand_name and name.casefold() == brand_name.casefold():
                return full
            if email and email.casefold() in lead_contact_emails(full):
                return full
    return None


def sync_self_serve_signup_to_close(
    *,
    tenant,
    user,
    brand_name: str,
    email: str,
    first_name: str = "",
    last_name: str = "",
    utm: dict[str, Any] | None = None,
    landing_path: str = "/onboarding",
    client: CloseClient | None = None,
) -> dict[str, Any]:
    """Best-effort Close lead sync. Returns ``{ok, close_lead_id, created, reason}``."""
    clean_utm = sanitize_utm(utm)
    result: dict[str, Any] = {
        "ok": False,
        "close_lead_id": "",
        "created": False,
        "utm": clean_utm,
        "reason": "",
    }
    if not close_configured() and client is None:
        result["reason"] = "close-not-configured"
        return result
    if not getattr(settings, "STANDALONE_SIGNUP_CLOSE_SYNC_ENABLED", True):
        result["reason"] = "close-sync-disabled"
        return result

    brand = (brand_name or getattr(tenant, "name", "") or "").strip()
    contact_email = (email or getattr(user, "email", "") or "").strip().lower()
    if not brand:
        result["reason"] = "missing-brand-name"
        return result

    try:
        close = client or CloseClient()
    except CloseNotConfigured:
        result["reason"] = "close-not-configured"
        return result

    note = _build_note(
        brand_name=brand,
        email=contact_email,
        tenant_slug=getattr(tenant, "slug", "") or "",
        tenant_uuid=str(getattr(tenant, "uuid", "") or ""),
        utm=clean_utm,
        landing_path=landing_path or "/onboarding",
    )
    description = (
        f"Spark self-serve signup · {brand}"
        + (f" · utm_campaign={clean_utm['utm_campaign']}" if "utm_campaign" in clean_utm else "")
    )

    try:
        existing = _find_existing_lead(close, brand_name=brand, email=contact_email)
    except CloseLookupError as exc:
        logger.warning(
            "Close marketing lead lookup failed for tenant %s (aborting create): %s",
            getattr(tenant, "id", "?"),
            exc,
        )
        result["reason"] = f"lookup-failed: {exc}"[:280]
        return result

    try:
        if existing and existing.get("id"):
            lead_id = existing["id"]
            close.create_note(lead_id, note)
            result.update(ok=True, close_lead_id=lead_id, created=False, reason="updated-existing")
        else:
            contact_name = " ".join(
                p for p in [(first_name or "").strip(), (last_name or "").strip()] if p
            ).strip() or contact_email or brand
            contacts = []
            if contact_email:
                contacts.append(
                    {
                        "name": contact_name,
                        "emails": [{"email": contact_email, "type": "office"}],
                    }
                )
            created = close.create_lead(
                name=brand,
                description=description[:1000],
                contacts=contacts or None,
            )
            lead_id = created.get("id") or ""
            if not lead_id:
                result["reason"] = "create-returned-no-id"
                return result
            close.create_note(lead_id, note)
            result.update(ok=True, close_lead_id=lead_id, created=True, reason="created")
    except (CloseError, UnsafeCloseWrite, CloseNotConfigured) as exc:
        logger.warning("Close marketing lead sync failed for tenant %s: %s", getattr(tenant, "id", "?"), exc)
        result["reason"] = str(exc)[:280]
        return result

    # Link tenant → lead so prospect drafts treat this as an existing client.
    try:
        CloseLink.objects.update_or_create(
            tenant=tenant,
            defaults={
                "close_lead_id": result["close_lead_id"],
                "contact_email": contact_email,
                "active": True,
                "linked_by": user if getattr(user, "pk", None) else None,
            },
        )
    except Exception as exc:  # noqa: BLE001 — never block signup on link write
        logger.warning("CloseLink write failed after lead sync: %s", exc)
        result["reason"] = f"{result['reason']}; link-failed"

    return result
