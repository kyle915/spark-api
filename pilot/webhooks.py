"""Outbound HTTPS Webhooks (recap.approved, request.created, shelf.oos_detected).

Security & reliability invariants:
- Strictly tenant-scoped management (brand admins / Ignite staff).
- Strict HTTPS-only + SSRF protection rejecting localhost, private RFC1918,
  loopback, link-local, and cloud metadata endpoints.
- HMAC-SHA256 signing via ``X-Spark-Signature: t=<unix_ts>,v1=<hex_sig>``.
- Buyer-safe payloads only: never includes BA pay, internal staff notes, or PII.
- Production event dispatch runs asynchronously on a daemon thread with short
  timeouts so a slow or failing webhook never delays or breaks Spark mutations.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from django.utils import timezone

from pilot.models import WebhookDelivery, WebhookEndpoint
from pilot.permissions import ResolvedWorkspaceAccess, log_workspace_audit

logger = logging.getLogger(__name__)

WEBHOOK_TIMEOUT_SECONDS = 5.0
MAX_RESPONSE_EXCERPT = 280
MAX_ENDPOINTS_PER_TENANT = 15

SUPPORTED_WEBHOOK_EVENTS: tuple[dict[str, str], ...] = (
    {
        "event": WebhookEndpoint.EVENT_RECAP_APPROVED,
        "label": "Recap Approved",
        "description": "Fires whenever a field activation recap is approved for client reporting.",
    },
    {
        "event": WebhookEndpoint.EVENT_REQUEST_CREATED,
        "label": "Request Created",
        "description": "Fires when a new activation request is created via Spark or an express form.",
    },
    {
        "event": WebhookEndpoint.EVENT_SHELF_OOS_DETECTED,
        "label": "Shelf Out-of-Stock Detected",
        "description": "Fires when a shelf photo audit or correction flags product not found or an OOS gap.",
    },
)
SUPPORTED_EVENT_SET = {item["event"] for item in SUPPORTED_WEBHOOK_EVENTS}

_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata.google.internal",
        "metadata",
        "169.254.169.254",
    }
)


def generate_webhook_secret() -> str:
    return f"whsec_{secrets.token_hex(24)}"


def mask_webhook_secret(secret: str) -> str:
    raw = secret or ""
    if len(raw) <= 12:
        return "whsec_••••••••"
    return f"{raw[:10]}••••••••{raw[-4:]}"


def _is_disallowed_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_webhook_url(raw_url: str, *, resolve_dns: bool = False) -> str:
    """Validate that a webhook target is a public HTTPS endpoint (SSRF-safe)."""
    url = (raw_url or "").strip()
    if not url:
        raise ValueError("Webhook URL is required.")
    if len(url) > 500:
        raise ValueError("Webhook URL must be 500 characters or fewer.")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() != "https":
        raise ValueError("Webhook URL must use HTTPS (https://...).")
    if parsed.username or parsed.password:
        raise ValueError("Webhook URL cannot contain embedded credentials.")
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host or "." not in host:
        raise ValueError("Webhook URL must point to a valid public hostname.")
    if host in _BLOCKED_HOSTNAMES or host.endswith((".local", ".internal", ".localhost")):
        raise ValueError("Private, loopback, or metadata hosts are not allowed.")

    # Direct IP literal check
    try:
        ipaddress.ip_address(host)
        if _is_disallowed_ip(host):
            raise ValueError("Private or reserved IP addresses are not allowed.")
    except ValueError as exc:
        if "not allowed" in str(exc):
            raise

    if resolve_dns:
        try:
            infos = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError(f"Could not resolve webhook host {host!r}: {exc}") from exc
        for info in infos:
            addr = info[4][0]
            if _is_disallowed_ip(addr):
                raise ValueError(
                    "Webhook destination resolves to a private or reserved network address."
                )
    return url


def normalize_webhook_events(events: list[str] | tuple[str, ...]) -> list[str]:
    clean: list[str] = []
    seen: set[str] = set()
    for raw in events or []:
        ev = str(raw or "").strip()
        if ev not in SUPPORTED_EVENT_SET:
            raise ValueError(f"Unsupported webhook event: {ev!r}")
        if ev not in seen:
            seen.add(ev)
            clean.append(ev)
    if not clean:
        raise ValueError("Select at least one webhook event to subscribe to.")
    return clean


def sign_webhook_payload(secret: str, timestamp: int, raw_body: bytes) -> str:
    """Compute Stripe-style ``t=<ts>,v1=<hmac_sha256>`` signature header."""
    signed_input = f"{int(timestamp)}.".encode("utf-8") + raw_body
    digest = hmac.new(
        secret.encode("utf-8"),
        signed_input,
        hashlib.sha256,
    ).hexdigest()
    return f"t={int(timestamp)},v1={digest}"


def verify_webhook_signature(
    secret: str,
    signature_header: str,
    raw_body: bytes,
    *,
    tolerance_seconds: int = 300,
    now_ts: int | None = None,
) -> bool:
    """Verify an ``X-Spark-Signature`` header (used in docs and unit tests)."""
    parts = dict(
        kv.split("=", 1) for kv in (signature_header or "").split(",") if "=" in kv
    )
    try:
        ts = int(parts["t"])
        sig = parts["v1"]
    except (KeyError, ValueError):
        return False
    ref = int(now_ts if now_ts is not None else time.time())
    if abs(ref - ts) > tolerance_seconds:
        return False
    expected = sign_webhook_payload(secret, ts, raw_body).split("v1=", 1)[1]
    return hmac.compare_digest(expected, sig)


def list_webhook_endpoints(access: ResolvedWorkspaceAccess) -> list[WebhookEndpoint]:
    return list(
        WebhookEndpoint.objects.filter(tenant=access.tenant)
        .prefetch_related("deliveries")
        .order_by("-created_at", "-id")
    )


def _require_webhook_admin(access: ResolvedWorkspaceAccess) -> None:
    if not (access.is_brand_admin or access.can_manage_pos):
        raise PermissionError(
            "Only brand administrators can manage outbound webhooks."
        )


def create_webhook_endpoint(
    access: ResolvedWorkspaceAccess,
    *,
    name: str,
    url: str,
    events: list[str],
    description: str = "",
) -> tuple[WebhookEndpoint, str]:
    _require_webhook_admin(access)
    if WebhookEndpoint.objects.filter(tenant=access.tenant).count() >= MAX_ENDPOINTS_PER_TENANT:
        raise ValueError(
            f"Maximum of {MAX_ENDPOINTS_PER_TENANT} webhook endpoints per workspace reached."
        )
    clean_name = (name or "").strip()[:120]
    if not clean_name:
        raise ValueError("Webhook name is required.")
    clean_url = validate_webhook_url(url)
    clean_events = normalize_webhook_events(events)
    secret = generate_webhook_secret()

    endpoint = WebhookEndpoint.objects.create(
        tenant=access.tenant,
        name=clean_name,
        url=clean_url,
        description=(description or "").strip()[:240],
        events=clean_events,
        signing_secret=secret,
        is_active=True,
        created_by=access.user if getattr(access.user, "pk", None) else None,
    )
    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="integrations",
        action="webhook.created",
        summary=f"Created webhook endpoint '{endpoint.name}' ({', '.join(clean_events)}).",
        metadata={"endpoint_uuid": str(endpoint.uuid), "url": clean_url},
    )
    return endpoint, secret


def update_webhook_endpoint(
    access: ResolvedWorkspaceAccess,
    *,
    endpoint_uuid: str,
    name: str | None = None,
    url: str | None = None,
    events: list[str] | None = None,
    description: str | None = None,
    is_active: bool | None = None,
) -> WebhookEndpoint:
    _require_webhook_admin(access)
    endpoint = WebhookEndpoint.objects.filter(
        tenant=access.tenant, uuid=endpoint_uuid
    ).first()
    if endpoint is None:
        raise ValueError("Webhook endpoint not found.")

    if name is not None:
        clean_name = name.strip()[:120]
        if not clean_name:
            raise ValueError("Webhook name cannot be empty.")
        endpoint.name = clean_name
    if url is not None:
        endpoint.url = validate_webhook_url(url)
    if events is not None:
        endpoint.events = normalize_webhook_events(events)
    if description is not None:
        endpoint.description = description.strip()[:240]
    if is_active is not None:
        endpoint.is_active = bool(is_active)
        if endpoint.is_active:
            endpoint.consecutive_failures = 0
    endpoint.save()
    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="integrations",
        action="webhook.updated",
        summary=f"Updated webhook endpoint '{endpoint.name}'.",
        metadata={"endpoint_uuid": str(endpoint.uuid), "is_active": endpoint.is_active},
    )
    return endpoint


def rotate_webhook_secret(
    access: ResolvedWorkspaceAccess,
    *,
    endpoint_uuid: str,
) -> tuple[WebhookEndpoint, str]:
    _require_webhook_admin(access)
    endpoint = WebhookEndpoint.objects.filter(
        tenant=access.tenant, uuid=endpoint_uuid
    ).first()
    if endpoint is None:
        raise ValueError("Webhook endpoint not found.")
    new_secret = generate_webhook_secret()
    endpoint.signing_secret = new_secret
    endpoint.save(update_fields=["signing_secret", "updated_at"])
    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="integrations",
        action="webhook.secret_rotated",
        summary=f"Rotated signing secret for webhook '{endpoint.name}'.",
        metadata={"endpoint_uuid": str(endpoint.uuid)},
    )
    return endpoint, new_secret


def delete_webhook_endpoint(
    access: ResolvedWorkspaceAccess,
    *,
    endpoint_uuid: str,
) -> bool:
    _require_webhook_admin(access)
    endpoint = WebhookEndpoint.objects.filter(
        tenant=access.tenant, uuid=endpoint_uuid
    ).first()
    if endpoint is None:
        return False
    name = endpoint.name
    endpoint.delete()
    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="integrations",
        action="webhook.deleted",
        summary=f"Deleted webhook endpoint '{name}'.",
        metadata={"endpoint_uuid": str(endpoint_uuid)},
    )
    return True


def _deliver_once(
    endpoint: WebhookEndpoint,
    *,
    event_type: str,
    data: dict[str, Any],
) -> WebhookDelivery:
    event_id = f"evt_{secrets.token_hex(12)}"
    ts = int(time.time())
    envelope = {
        "id": event_id,
        "event": event_type,
        "createdAt": timezone.now().isoformat(),
        "tenantUuid": str(endpoint.tenant.uuid),
        "tenantName": endpoint.tenant.name or "",
        "data": data,
    }
    raw_body = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = sign_webhook_payload(endpoint.signing_secret, ts, raw_body)

    status = WebhookDelivery.STATUS_FAILED
    http_status: int | None = None
    response_excerpt = ""
    error_message = ""
    t0 = time.monotonic()

    try:
        # Re-validate destination with live DNS resolution to defeat DNS rebinding SSRF.
        validate_webhook_url(endpoint.url, resolve_dns=True)
        req = urllib.request.Request(
            endpoint.url,
            data=raw_body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Spark-Webhooks/1.0",
                "X-Spark-Event": event_type,
                "X-Spark-Delivery": event_id,
                "X-Spark-Timestamp": str(ts),
                "X-Spark-Signature": signature,
            },
        )
        with urllib.request.urlopen(req, timeout=WEBHOOK_TIMEOUT_SECONDS) as resp:
            http_status = int(getattr(resp, "status", 200) or 200)
            body_preview = resp.read(MAX_RESPONSE_EXCERPT).decode("utf-8", errors="replace")
            response_excerpt = body_preview.strip()[:MAX_RESPONSE_EXCERPT]
            if 200 <= http_status < 300:
                status = WebhookDelivery.STATUS_DELIVERED
            else:
                error_message = f"HTTP {http_status}"
    except urllib.error.HTTPError as exc:
        http_status = int(exc.code)
        try:
            response_excerpt = (
                exc.read(MAX_RESPONSE_EXCERPT).decode("utf-8", errors="replace").strip()
            )
        except Exception:  # noqa: BLE001
            response_excerpt = ""
        error_message = f"HTTP {http_status}: {exc.reason}"[:300]
    except Exception as exc:  # noqa: BLE001
        error_message = str(exc)[:300]

    duration_ms = max(1, int((time.monotonic() - t0) * 1000))
    delivery = WebhookDelivery.objects.create(
        endpoint=endpoint,
        tenant=endpoint.tenant,
        event_type=event_type,
        event_id=event_id,
        payload=envelope,
        status=status,
        http_status=http_status,
        duration_ms=duration_ms,
        response_excerpt=response_excerpt[:300],
        error_message=error_message[:300],
    )

    now = timezone.now()
    endpoint.last_delivery_at = now
    endpoint.last_status_code = http_status
    if status == WebhookDelivery.STATUS_DELIVERED:
        endpoint.consecutive_failures = 0
    else:
        endpoint.consecutive_failures = min(999, int(endpoint.consecutive_failures or 0) + 1)
    endpoint.save(
        update_fields=[
            "last_delivery_at",
            "last_status_code",
            "consecutive_failures",
            "updated_at",
        ]
    )
    return delivery


def send_test_webhook(
    access: ResolvedWorkspaceAccess,
    *,
    endpoint_uuid: str,
) -> WebhookDelivery:
    """Send a synchronous ``webhook.test`` ping and return the logged delivery."""
    _require_webhook_admin(access)
    endpoint = (
        WebhookEndpoint.objects.select_related("tenant")
        .filter(tenant=access.tenant, uuid=endpoint_uuid)
        .first()
    )
    if endpoint is None:
        raise ValueError("Webhook endpoint not found.")
    sample_event = (endpoint.events or [WebhookEndpoint.EVENT_RECAP_APPROVED])[0]
    return _deliver_once(
        endpoint,
        event_type="webhook.test",
        data={
            "message": "Test delivery from Spark Webhooks.",
            "sampleSubscribedEvent": sample_event,
            "endpointName": endpoint.name,
        },
    )


def dispatch_webhook_event(
    *,
    tenant_id: int | None,
    event_type: str,
    data: dict[str, Any],
    sync: bool = False,
) -> int:
    """Fire an outbound webhook event to every active subscribed endpoint for ``tenant_id``.

    Runs asynchronously in a daemon thread by default so it never delays or breaks
    recap approvals, request creation, or shelf checks.
    """
    if not tenant_id or event_type not in SUPPORTED_EVENT_SET:
        return 0
    try:
        endpoints = list(
            WebhookEndpoint.objects.select_related("tenant").filter(
                tenant_id=tenant_id, is_active=True
            )
        )
    except Exception:  # noqa: BLE001
        logger.debug("webhook lookup skipped tenant=%s event=%s", tenant_id, event_type)
        return 0

    matching = [ep for ep in endpoints if event_type in (ep.events or [])]
    if not matching:
        return 0

    def _run_all() -> None:
        from django.db import close_old_connections

        close_old_connections()
        for ep in matching:
            try:
                _deliver_once(ep, event_type=event_type, data=data)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "webhook delivery failed endpoint=%s event=%s", ep.uuid, event_type
                )
        close_old_connections()

    if sync:
        _run_all()
    else:
        t = threading.Thread(
            target=_run_all,
            name=f"spark-webhook-{event_type}",
            daemon=True,
        )
        t.start()
    return len(matching)
