"""Territory-scoped Regional Manager Alert Subscriptions.

Allows brand regional managers to subscribe to instant email and/or HMAC-signed
HTTPS webhook alerts filtered to their own US/CA states and/or retail chains
(e.g. "Texas H-E-B: alert immediately on out-of-stock, >5% MSRP drift, or
missing price tag").

Security & reliability invariants:
- Strictly tenant-scoped: subscriptions only evaluate shelf audits for their own brand.
- Reuses ``pilot.webhooks.validate_webhook_url`` (HTTPS + SSRF protection) and
  ``sign_webhook_payload`` (HMAC-SHA256) for outbound webhook deliveries.
- Production shelf-audit dispatch runs in a daemon thread so a slow endpoint
  never delays or breaks shelf reading or staff corrections.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from django.utils import timezone

from pilot.models import RegionalAlertDelivery, RegionalAlertSubscription
from pilot.permissions import ResolvedWorkspaceAccess, log_workspace_audit
from pilot.webhooks import (
    WEBHOOK_TIMEOUT_SECONDS,
    generate_webhook_secret,
    mask_webhook_secret,
    sign_webhook_payload,
    validate_webhook_url,
)
from tenants.models import parse_recipient_emails

logger = logging.getLogger(__name__)

MAX_SUBSCRIPTIONS_PER_TENANT = 25
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_STATE_RE = re.compile(r"^[A-Z]{2}$")


def normalize_chain_key(text: str) -> str:
    """Fold punctuation/spacing so 'H-E-B' matches 'HEB #104' or 'H-E-B Austin'."""
    return _NON_ALNUM_RE.sub("", (text or "").lower())


def normalize_states(states: list[str] | tuple[str, ...] | None) -> list[str]:
    clean: list[str] = []
    seen: set[str] = set()
    for raw in states or []:
        for token in str(raw or "").replace(";", ",").split(","):
            code = token.strip().upper()
            if not code:
                continue
            if not _STATE_RE.match(code):
                raise ValueError(
                    f"Invalid state code {token!r}. Use 2-letter codes like TX, CA, NY."
                )
            if code not in seen:
                seen.add(code)
                clean.append(code)
    return clean[:30]


def normalize_chains(chains: list[str] | tuple[str, ...] | None) -> list[str]:
    clean: list[str] = []
    seen: set[str] = set()
    for raw in chains or []:
        for token in str(raw or "").replace(";", ",").split(","):
            name = token.strip()[:80]
            key = normalize_chain_key(name)
            if not name or not key:
                continue
            if key not in seen:
                seen.add(key)
                clean.append(name)
    return clean[:20]


def _parse_decimal_opt(
    raw: Any, *, field_label: str, min_val: Decimal, max_val: Decimal
) -> Decimal | None:
    if raw in (None, ""):
        return None
    cleaned = str(raw).strip().lstrip("$").rstrip("%").replace(",", "")
    if not cleaned:
        return None
    try:
        dec = Decimal(cleaned)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_label} must be a valid number.") from exc
    if dec < min_val or dec > max_val:
        raise ValueError(f"{field_label} must be between {min_val} and {max_val}.")
    return dec


def evaluate_regional_alert_match(
    sub: RegionalAlertSubscription,
    *,
    store_name: str,
    state_code: str,
    product_on_shelf: str | None = None,
    out_of_stock_gap: str | None = None,
    price_tag_visible: str | None = None,
    observed_price: Decimal | float | str | None = None,
) -> dict[str, Any] | None:
    """Return a structured trigger payload if a shelf audit matches ``sub``, else None."""
    if not getattr(sub, "is_active", False):
        return None

    # 1. State territory filter (empty = all states)
    allowed_states = [str(s).strip().upper() for s in (sub.states or []) if str(s).strip()]
    clean_state = (state_code or "").strip().upper()
    if allowed_states and clean_state not in allowed_states:
        return None

    # 2. Chain / retailer substring filter (empty = all chains)
    allowed_chains = [str(c).strip() for c in (sub.chains or []) if str(c).strip()]
    store_key = normalize_chain_key(store_name)
    if allowed_chains:
        if not store_key or not any(
            normalize_chain_key(c) in store_key for c in allowed_chains if normalize_chain_key(c)
        ):
            return None

    # 3. Trigger condition checks
    reasons: list[str] = []
    trigger_kinds: list[str] = []
    drift_pct: float | None = None
    observed_str: str | None = None
    target_str: str | None = (
        f"{sub.target_msrp:.2f}" if sub.target_msrp is not None else None
    )

    if sub.alert_oos and (
        (product_on_shelf or "").lower() == "no"
        or (out_of_stock_gap or "").lower() == "yes"
    ):
        reasons.append("Out-of-stock / product not found on shelf")
        trigger_kinds.append(RegionalAlertDelivery.TRIGGER_OOS)

    if sub.alert_missing_tag and (price_tag_visible or "").lower() == "no":
        reasons.append("Missing shelf price tag")
        trigger_kinds.append(RegionalAlertDelivery.TRIGGER_MISSING_TAG)

    if sub.alert_price_drift and sub.target_msrp is not None and observed_price not in (None, ""):
        try:
            obs = Decimal(str(observed_price))
            target = Decimal(str(sub.target_msrp))
            threshold = float(sub.drift_threshold_pct or Decimal("5.0"))
            if target > 0 and obs > 0:
                observed_str = f"{obs.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"
                pct = float(((obs - target) / target) * Decimal("100"))
                pct_r = float(Decimal(str(pct)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
                if abs(pct_r) >= threshold:
                    drift_pct = pct_r
                    sign = "+" if pct_r > 0 else ""
                    reasons.append(
                        f"Price drift {sign}{pct_r:.1f}% (${observed_str} vs. ${target_str} MSRP)"
                    )
                    trigger_kinds.append(RegionalAlertDelivery.TRIGGER_PRICE_DRIFT)
        except (InvalidOperation, ValueError, ZeroDivisionError):
            pass

    if not reasons:
        return None

    return {
        "trigger_kind": trigger_kinds[0],
        "trigger_kinds": trigger_kinds,
        "reasons": reasons,
        "summary": " · ".join(reasons)[:320],
        "observed_price": observed_str,
        "target_msrp": target_str,
        "drift_pct": drift_pct,
    }


def list_regional_alert_subscriptions(
    access: ResolvedWorkspaceAccess,
) -> list[RegionalAlertSubscription]:
    return list(
        RegionalAlertSubscription.objects.filter(tenant=access.tenant)
        .prefetch_related("deliveries")
        .order_by("-created_at", "-id")
    )


def upsert_regional_alert_subscription(
    access: ResolvedWorkspaceAccess,
    *,
    subscription_uuid: str | None = None,
    name: str,
    states: list[str] | None = None,
    chains: list[str] | None = None,
    alert_oos: bool = True,
    alert_price_drift: bool = True,
    alert_missing_tag: bool = True,
    target_msrp: str | None = None,
    drift_threshold_pct: str | None = "5.0",
    notify_email: bool = True,
    recipient_emails: str = "",
    notify_webhook: bool = False,
    webhook_url: str = "",
    is_active: bool = True,
) -> RegionalAlertSubscription:
    clean_name = (name or "").strip()[:120]
    if not clean_name:
        raise ValueError("Give this regional alert rule a name (e.g. Texas H-E-B Watch).")

    clean_states = normalize_states(states)
    clean_chains = normalize_chains(chains)

    if not (alert_oos or alert_price_drift or alert_missing_tag):
        raise ValueError("Select at least one shelf condition to alert on.")

    msrp_dec = _parse_decimal_opt(
        target_msrp,
        field_label="Target MSRP",
        min_val=Decimal("0.01"),
        max_val=Decimal("9999.99"),
    )
    if msrp_dec is not None:
        msrp_dec = msrp_dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if alert_price_drift and msrp_dec is None and not (alert_oos or alert_missing_tag):
        raise ValueError("Enter a Target MSRP to trigger price-drift alerts.")

    thresh_dec = _parse_decimal_opt(
        drift_threshold_pct if drift_threshold_pct not in (None, "") else "5.0",
        field_label="Drift threshold %",
        min_val=Decimal("1.0"),
        max_val=Decimal("100.0"),
    ) or Decimal("5.0")
    thresh_dec = thresh_dec.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)

    if not (notify_email or notify_webhook):
        raise ValueError("Enable Email or Webhook delivery (or both).")

    parsed_emails = parse_recipient_emails(recipient_emails or "")
    if notify_email and not parsed_emails:
        fallback_email = (getattr(access.user, "email", "") or "").strip()
        if fallback_email:
            parsed_emails = [fallback_email]
        else:
            raise ValueError("Enter at least one recipient email for email alerts.")

    clean_webhook_url = ""
    if notify_webhook:
        clean_webhook_url = validate_webhook_url(webhook_url)
    elif (webhook_url or "").strip():
        clean_webhook_url = validate_webhook_url(webhook_url)

    if subscription_uuid:
        sub = RegionalAlertSubscription.objects.filter(
            tenant=access.tenant, uuid=subscription_uuid
        ).first()
        if sub is None:
            raise ValueError("Regional alert subscription not found.")
        sub.name = clean_name
        sub.is_active = bool(is_active)
        sub.states = clean_states
        sub.chains = clean_chains
        sub.alert_oos = bool(alert_oos)
        sub.alert_price_drift = bool(alert_price_drift)
        sub.alert_missing_tag = bool(alert_missing_tag)
        sub.target_msrp = msrp_dec
        sub.drift_threshold_pct = thresh_dec
        sub.notify_email = bool(notify_email)
        sub.recipient_emails = ", ".join(parsed_emails)
        sub.notify_webhook = bool(notify_webhook)
        sub.webhook_url = clean_webhook_url
        if sub.notify_webhook and not sub.webhook_secret:
            sub.webhook_secret = generate_webhook_secret()
        sub.save()
        action = "regional_alert.updated"
    else:
        if (
            RegionalAlertSubscription.objects.filter(tenant=access.tenant).count()
            >= MAX_SUBSCRIPTIONS_PER_TENANT
        ):
            raise ValueError(
                f"Maximum of {MAX_SUBSCRIPTIONS_PER_TENANT} regional alert rules per workspace reached."
            )
        sub = RegionalAlertSubscription.objects.create(
            tenant=access.tenant,
            name=clean_name,
            is_active=bool(is_active),
            states=clean_states,
            chains=clean_chains,
            alert_oos=bool(alert_oos),
            alert_price_drift=bool(alert_price_drift),
            alert_missing_tag=bool(alert_missing_tag),
            target_msrp=msrp_dec,
            drift_threshold_pct=thresh_dec,
            notify_email=bool(notify_email),
            recipient_emails=", ".join(parsed_emails),
            notify_webhook=bool(notify_webhook),
            webhook_url=clean_webhook_url,
            webhook_secret=generate_webhook_secret() if notify_webhook else "",
            created_by=access.user if getattr(access.user, "pk", None) else None,
        )
        action = "regional_alert.created"

    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="alerts",
        action=action,
        summary=f"{'Updated' if subscription_uuid else 'Created'} regional shelf alert '{sub.name}'.",
        metadata={
            "subscription_uuid": str(sub.uuid),
            "states": sub.states,
            "chains": sub.chains,
        },
    )
    return sub


def delete_regional_alert_subscription(
    access: ResolvedWorkspaceAccess,
    *,
    subscription_uuid: str,
) -> bool:
    sub = RegionalAlertSubscription.objects.filter(
        tenant=access.tenant, uuid=subscription_uuid
    ).first()
    if sub is None:
        return False
    name = sub.name
    sub.delete()
    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="alerts",
        action="regional_alert.deleted",
        summary=f"Deleted regional shelf alert '{name}'.",
        metadata={"subscription_uuid": str(subscription_uuid)},
    )
    return True


def _deliver_alert_email(
    sub: RegionalAlertSubscription,
    *,
    trigger_kind: str,
    store_name: str,
    state_code: str,
    event_date: str,
    reasons: list[str],
    summary: str,
    observed_price: str | None = None,
    target_msrp: str | None = None,
    drift_pct: float | None = None,
    photo_url: str | None = None,
    is_test: bool = False,
) -> RegionalAlertDelivery:
    from pilot.envelopes import RegionalShelfAlertMailer

    recipients = parse_recipient_emails(sub.recipient_emails or "")
    status = RegionalAlertDelivery.STATUS_FAILED
    error_message = ""
    if not recipients:
        error_message = "No email recipients configured."
    else:
        try:
            mailer = RegionalShelfAlertMailer(
                recipients=recipients,
                tenant_name=getattr(sub.tenant, "name", None) or "Brand",
                subscription_name=sub.name,
                store_name=store_name,
                state_code=state_code,
                event_date=event_date,
                reasons=reasons,
                observed_price=observed_price,
                target_msrp=target_msrp,
                drift_pct=drift_pct,
                photo_url=photo_url,
                is_test=is_test,
            )
            ok = mailer.send_now()
            if ok is True:
                status = RegionalAlertDelivery.STATUS_SENT
            else:
                error_message = "Mail provider did not accept delivery."
        except Exception as exc:  # noqa: BLE001
            error_message = str(exc)[:300]

    return RegionalAlertDelivery.objects.create(
        subscription=sub,
        tenant=sub.tenant,
        trigger_kind=trigger_kind,
        channel=RegionalAlertDelivery.CHANNEL_EMAIL,
        store_name=store_name[:160],
        state_code=state_code[:12],
        summary=summary[:320],
        status=status,
        error_message=error_message[:300],
    )


def _deliver_alert_webhook(
    sub: RegionalAlertSubscription,
    *,
    trigger_kind: str,
    store_name: str,
    state_code: str,
    event_date: str,
    reasons: list[str],
    summary: str,
    observed_price: str | None = None,
    target_msrp: str | None = None,
    drift_pct: float | None = None,
    photo_url: str | None = None,
    is_test: bool = False,
) -> RegionalAlertDelivery:
    status = RegionalAlertDelivery.STATUS_FAILED
    error_message = ""
    ts = int(time.time())
    payload = {
        "event": "regional_alert.test" if is_test else "regional_alert.triggered",
        "timestamp": ts,
        "tenantId": sub.tenant_id,
        "tenantName": getattr(sub.tenant, "name", None) or "",
        "subscription": {
            "uuid": str(sub.uuid),
            "name": sub.name,
            "states": list(sub.states or []),
            "chains": list(sub.chains or []),
        },
        "data": {
            "triggerKind": trigger_kind,
            "storeName": store_name,
            "stateCode": state_code,
            "eventDate": event_date,
            "reasons": reasons,
            "summary": summary,
            "observedPrice": observed_price,
            "targetMsrp": target_msrp,
            "driftPct": drift_pct,
            "photoUrl": photo_url or None,
            "isTest": is_test,
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    secret = sub.webhook_secret or generate_webhook_secret()
    signature = sign_webhook_payload(secret, ts, raw_body)

    try:
        validate_webhook_url(sub.webhook_url, resolve_dns=True)
        req = urllib.request.Request(
            sub.webhook_url,
            data=raw_body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Spark-Regional-Alerts/1.0",
                "X-Spark-Event": payload["event"],
                "X-Spark-Timestamp": str(ts),
                "X-Spark-Signature": signature,
            },
        )
        with urllib.request.urlopen(req, timeout=WEBHOOK_TIMEOUT_SECONDS) as resp:
            code = int(getattr(resp, "status", 200) or 200)
            if 200 <= code < 300:
                status = RegionalAlertDelivery.STATUS_SENT
            else:
                error_message = f"HTTP {code}"
    except urllib.error.HTTPError as exc:
        error_message = f"HTTP {exc.code}: {exc.reason}"[:300]
    except Exception as exc:  # noqa: BLE001
        error_message = str(exc)[:300]

    return RegionalAlertDelivery.objects.create(
        subscription=sub,
        tenant=sub.tenant,
        trigger_kind=trigger_kind,
        channel=RegionalAlertDelivery.CHANNEL_WEBHOOK,
        store_name=store_name[:160],
        state_code=state_code[:12],
        summary=summary[:320],
        status=status,
        error_message=error_message[:300],
    )


def send_test_regional_alert(
    access: ResolvedWorkspaceAccess,
    *,
    subscription_uuid: str,
) -> list[RegionalAlertDelivery]:
    """Send a synchronous test alert across every enabled channel for a rule."""
    sub = (
        RegionalAlertSubscription.objects.select_related("tenant")
        .filter(tenant=access.tenant, uuid=subscription_uuid)
        .first()
    )
    if sub is None:
        raise ValueError("Regional alert subscription not found.")

    sample_chain = (sub.chains or ["Sample Grocery"])[0]
    sample_state = (sub.states or ["TX"])[0]
    sample_store = f"{sample_chain} #104 (Test)"
    reasons = ["Test alert: simulated out-of-stock & MSRP check"]
    summary = f"Test alert for {sample_store} ({sample_state})"
    today_iso = timezone.localdate().isoformat()
    deliveries: list[RegionalAlertDelivery] = []

    if sub.notify_email:
        deliveries.append(
            _deliver_alert_email(
                sub,
                trigger_kind=RegionalAlertDelivery.TRIGGER_TEST,
                store_name=sample_store,
                state_code=sample_state,
                event_date=today_iso,
                reasons=reasons,
                summary=summary,
                target_msrp=f"{sub.target_msrp:.2f}" if sub.target_msrp is not None else None,
                is_test=True,
            )
        )
    if sub.notify_webhook and sub.webhook_url:
        deliveries.append(
            _deliver_alert_webhook(
                sub,
                trigger_kind=RegionalAlertDelivery.TRIGGER_TEST,
                store_name=sample_store,
                state_code=sample_state,
                event_date=today_iso,
                reasons=reasons,
                summary=summary,
                target_msrp=f"{sub.target_msrp:.2f}" if sub.target_msrp is not None else None,
                is_test=True,
            )
        )
    return deliveries


def dispatch_regional_shelf_alerts(
    *,
    tenant_id: int | None,
    store_name: str,
    state_code: str,
    event_date: str = "",
    product_on_shelf: str | None = None,
    out_of_stock_gap: str | None = None,
    price_tag_visible: str | None = None,
    observed_price: Decimal | float | str | None = None,
    photo_url: str | None = None,
    sync: bool = False,
) -> int:
    """Evaluate all active regional alert subscriptions for ``tenant_id`` and dispatch matches.

    Runs asynchronously in a daemon thread by default so shelf photo audits and
    staff corrections never block on email/webhook I/O.
    """
    if not tenant_id:
        return 0
    try:
        subs = list(
            RegionalAlertSubscription.objects.select_related("tenant").filter(
                tenant_id=tenant_id, is_active=True
            )
        )
    except Exception:  # noqa: BLE001
        logger.debug("regional alert lookup skipped tenant=%s", tenant_id)
        return 0

    matches: list[tuple[RegionalAlertSubscription, dict[str, Any]]] = []
    for sub in subs:
        match = evaluate_regional_alert_match(
            sub,
            store_name=store_name,
            state_code=state_code,
            product_on_shelf=product_on_shelf,
            out_of_stock_gap=out_of_stock_gap,
            price_tag_visible=price_tag_visible,
            observed_price=observed_price,
        )
        if match is not None:
            matches.append((sub, match))

    if not matches:
        return 0

    def _run_all() -> None:
        for sub, match in matches:
            sent_any = False
            if sub.notify_email:
                d = _deliver_alert_email(
                    sub,
                    trigger_kind=match["trigger_kind"],
                    store_name=store_name,
                    state_code=state_code,
                    event_date=event_date,
                    reasons=match["reasons"],
                    summary=match["summary"],
                    observed_price=match["observed_price"],
                    target_msrp=match["target_msrp"],
                    drift_pct=match["drift_pct"],
                    photo_url=photo_url,
                )
                sent_any = sent_any or (d.status == RegionalAlertDelivery.STATUS_SENT)
            if sub.notify_webhook and sub.webhook_url:
                d = _deliver_alert_webhook(
                    sub,
                    trigger_kind=match["trigger_kind"],
                    store_name=store_name,
                    state_code=state_code,
                    event_date=event_date,
                    reasons=match["reasons"],
                    summary=match["summary"],
                    observed_price=match["observed_price"],
                    target_msrp=match["target_msrp"],
                    drift_pct=match["drift_pct"],
                    photo_url=photo_url,
                )
                sent_any = sent_any or (d.status == RegionalAlertDelivery.STATUS_SENT)
            if sent_any:
                sub.last_triggered_at = timezone.now()
                sub.trigger_count = int(sub.trigger_count or 0) + 1
                sub.save(update_fields=["last_triggered_at", "trigger_count", "updated_at"])

    if sync:
        _run_all()
    else:
        threading.Thread(
            target=_run_all,
            name=f"spark-regional-alerts-{tenant_id}",
            daemon=True,
        ).start()
    return len(matches)


def mask_alert_webhook_secret(secret: str) -> str:
    return mask_webhook_secret(secret) if secret else ""
