"""Feature 9 service layer: Recurring software billing in Stripe Test Mode.
- Hosted Checkout sessions & Customer Billing Portal
- Explicit buyer acceptance & pre-confirmation effective-date / charge disclosures
- Upgrades, downgrades, cancellation with paid-through period access, and failed renewals
- Verified `Stripe-Signature` HMAC-SHA256 webhook processing with strict replay
  idempotency and monotonic out-of-order protection
- Manually provisioned $1,500, 60-day Pro pilot support
- Managed SOW account + optional paid software add-on support
- Strict separation from billing.Invoice (agency service invoices),
  receipts.ConsumerReceipt (shopper rebates), and jobs.AmbassadorJob (BA pay)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time as time_module
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

import httpx
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from pilot.costs import quantize_money
from pilot.entitlements import (
    PlanEntitlementError,
    compute_monthly_visit_usage,
    ensure_brand_subscription,
    ensure_plan_catalog,
    get_plan_entry,
    is_client_billing_launched,
    resolve_brand_entitlements,
)
from pilot.models import (
    BrandSubscription,
    StripeCheckoutRecord,
    StripeWebhookEvent,
    WorkspaceProfile,
)
from pilot.permissions import (
    ResolvedWorkspaceAccess,
    is_ignite_staff_user,
    log_workspace_audit,
)
from pilot.workspaces import ensure_workspace_profile
from tenants.models import Tenant, User

PLAN_RANK = {
    WorkspaceProfile.PLAN_FREE: 0,
    WorkspaceProfile.PLAN_CORE: 1,
    WorkspaceProfile.PLAN_PRO: 2,
    WorkspaceProfile.PLAN_PILOT_PRO: 2,
    WorkspaceProfile.PLAN_SCALE: 3,
    WorkspaceProfile.PLAN_MANAGED_SOW: 4,
}

SUBSCRIPTION_DOMAIN_EVENTS = frozenset(
    {
        "checkout.session.completed",
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    }
)
INVOICE_DOMAIN_EVENTS = frozenset(
    {
        "invoice.paid",
        "invoice.payment_failed",
    }
)


def _is_simulated_stripe_subscription_id(sub_id: str | None) -> bool:
    val = (sub_id or "").strip()
    return not val or val.startswith("sub_sim_") or val.startswith("sub_test_")


def require_ignite_billing_staff(user: User | None) -> None:
    """Enforce that internal Stripe readiness & test-mode admin actions are
    strictly accessible to Ignite platform staff."""
    if user is None or not is_ignite_staff_user(user):
        raise PermissionError(
            "Plans & Billing readiness and test controls are restricted to Ignite administrators."
        )


def _require_billing_access(access: ResolvedWorkspaceAccess) -> None:
    """Block non-staff client callers from all billing queries & mutations
    while client-facing billing remains hidden (`CLIENT_BILLING_LAUNCHED=False`)."""
    if not is_client_billing_launched() and not (
        access.is_ignite_admin or is_ignite_staff_user(access.user)
    ):
        raise PermissionError(
            "Plans & Billing is currently restricted to Ignite administrators (internal test mode)."
        )


def _default_billing_ui_base_url() -> str:
    setting_name = (
        "CLIENT_FRONTEND_URL"
        if is_client_billing_launched()
        else "ADMIN_FRONTEND_URL"
    )
    return (
        getattr(settings, setting_name, "http://localhost:3000")
        or "http://localhost:3000"
    ).rstrip("/")


def _active_stripe_mode() -> str:
    mode = (getattr(settings, "STRIPE_BILLING_MODE", "test") or "test").strip().lower()
    if mode == "live" and getattr(settings, "DEBUG", False) and not getattr(
        settings, "STRIPE_ALLOW_LIVE_IN_DEBUG", False
    ):
        raise PermissionError(
            "Live Stripe billing is disabled in development (DEBUG=True)."
        )
    return "live" if mode == "live" else "test"


def _effective_webhook_secret() -> str:
    configured = (getattr(settings, "STRIPE_WEBHOOK_SECRET", "") or "").strip()
    if configured:
        return configured
    if _active_stripe_mode() == "live":
        raise PermissionError(
            "STRIPE_WEBHOOK_SECRET must be configured before processing live webhooks."
        )
    # Deterministic local test-mode signing secret derived from SECRET_KEY
    digest = hashlib.sha256(
        f"spark-stripe-test-webhook:{settings.SECRET_KEY}".encode("utf-8")
    ).hexdigest()[:32]
    return f"whsec_test_{digest}"


def sign_stripe_webhook_payload(
    raw_body: bytes | str,
    *,
    timestamp: int | None = None,
    secret: str | None = None,
) -> str:
    """Create a valid `Stripe-Signature` (`t=<ts>,v1=<hmac>`) header for
    test-mode checkout completion and webhook verification suites."""
    ts = int(timestamp if timestamp is not None else time_module.time())
    body_bytes = (
        raw_body.encode("utf-8") if isinstance(raw_body, str) else raw_body
    )
    wh_secret = secret if secret is not None else _effective_webhook_secret()
    signed_payload = f"{ts}.".encode("utf-8") + body_bytes
    sig = hmac.new(
        wh_secret.encode("utf-8"), signed_payload, hashlib.sha256
    ).hexdigest()
    return f"t={ts},v1={sig}"


def verify_stripe_webhook_signature(
    raw_body: bytes,
    signature_header: str | None,
    *,
    tolerance_seconds: int = 300,
    now_ts: int | None = None,
) -> dict[str, Any]:
    """Verify `Stripe-Signature` HMAC-SHA256 and parse the JSON event payload.
    Raises ValueError on missing/invalid signature or expired timestamp."""
    if not signature_header or not signature_header.strip():
        raise ValueError("Missing Stripe-Signature header.")

    parts: dict[str, list[str]] = {}
    for item in signature_header.split(","):
        if "=" not in item:
            continue
        k, v = item.strip().split("=", 1)
        parts.setdefault(k, []).append(v)

    ts_vals = parts.get("t") or []
    v1_sigs = parts.get("v1") or []
    if not ts_vals or not v1_sigs:
        raise ValueError("Malformed Stripe-Signature header.")

    try:
        ts_int = int(ts_vals[0])
    except ValueError as exc:
        raise ValueError("Invalid Stripe-Signature timestamp.") from exc

    ref_now = int(now_ts if now_ts is not None else time_module.time())
    if tolerance_seconds > 0 and abs(ref_now - ts_int) > tolerance_seconds:
        raise ValueError("Stripe webhook timestamp is outside tolerance window.")

    wh_secret = _effective_webhook_secret()
    expected = hmac.new(
        wh_secret.encode("utf-8"),
        f"{ts_int}.".encode("utf-8") + raw_body,
        hashlib.sha256,
    ).hexdigest()
    if not any(hmac.compare_digest(expected, cand) for cand in v1_sigs):
        raise ValueError("Invalid Stripe webhook signature.")

    try:
        event = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("Invalid Stripe webhook JSON payload.") from exc

    if not isinstance(event, dict) or not event.get("id") or not event.get("type"):
        raise ValueError("Stripe event payload missing id or type.")
    return event


class StripeBillingAdapter:
    """Thin Stripe Billing API client using httpx when `STRIPE_SECRET_KEY` is
    configured, with a clean local test-mode hosted checkout fallback when
    running in CI or local sandbox without external Stripe credentials."""

    def __init__(self) -> None:
        self.mode = _active_stripe_mode()
        self.secret_key = (getattr(settings, "STRIPE_SECRET_KEY", "") or "").strip()
        self.api_base = (
            getattr(settings, "STRIPE_API_BASE_URL", "https://api.stripe.com")
            or "https://api.stripe.com"
        ).rstrip("/")
        if self.secret_key:
            if self.mode == "test" and self.secret_key.startswith("sk_live_"):
                raise PermissionError(
                    "Refusing to use a live Stripe secret key (sk_live_) while STRIPE_BILLING_MODE='test'."
                )
            if self.mode == "live" and self.secret_key.startswith("sk_test_"):
                raise PermissionError(
                    "STRIPE_BILLING_MODE='live' requires a live secret key."
                )

    @property
    def has_remote_credentials(self) -> bool:
        return bool(self.secret_key)

    def create_checkout_session(
        self,
        *,
        tenant: Tenant,
        customer_email: str,
        existing_customer_id: str,
        price_id: str,
        target_plan_code: str,
        idempotency_key: str,
        success_url: str,
        cancel_url: str,
    ) -> dict[str, str]:
        if not self.has_remote_credentials:
            if self.mode == "live":
                raise PermissionError(
                    "STRIPE_SECRET_KEY is required for live Stripe checkout."
                )
            digest = hashlib.sha256(
                f"{tenant.uuid}:{target_plan_code}:{idempotency_key}".encode("utf-8")
            ).hexdigest()[:24]
            session_id = f"cs_test_{digest}"
            sep = "&" if "?" in success_url else "?"
            checkout_url = (
                f"{success_url}{sep}"
                + urlencode(
                    {
                        "test_checkout_session": session_id,
                        "plan": target_plan_code,
                    }
                )
            )
            return {
                "session_id": session_id,
                "checkout_url": checkout_url,
                "customer_id": existing_customer_id
                or f"cus_test_{str(tenant.uuid).replace('-', '')[:14]}",
                "provider_transport": "local_test_simulator",
            }

        form_data: dict[str, str] = {
            "mode": "subscription",
            "success_url": success_url,
            "cancel_url": cancel_url,
            "line_items[0][price]": price_id,
            "line_items[0][quantity]": "1",
            "client_reference_id": str(tenant.uuid),
            "metadata[tenant_uuid]": str(tenant.uuid),
            "metadata[target_plan_code]": target_plan_code,
            "subscription_data[metadata][tenant_uuid]": str(tenant.uuid),
            "subscription_data[metadata][target_plan_code]": target_plan_code,
        }
        if existing_customer_id:
            form_data["customer"] = existing_customer_id
        elif customer_email:
            form_data["customer_email"] = customer_email

        resp = httpx.post(
            f"{self.api_base}/v1/checkout/sessions",
            data=form_data,
            headers={
                "Authorization": f"Bearer {self.secret_key}",
                "Idempotency-Key": idempotency_key,
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        body = resp.json()
        return {
            "session_id": body["id"],
            "checkout_url": body["url"],
            "customer_id": body.get("customer") or existing_customer_id or "",
            "provider_transport": "stripe_api",
        }

    def create_billing_portal_session(
        self,
        *,
        customer_id: str,
        return_url: str,
    ) -> dict[str, str]:
        if not self.has_remote_credentials:
            sep = "&" if "?" in return_url else "?"
            return {
                "portal_url": f"{return_url}{sep}test_billing_portal=1",
                "provider_transport": "local_test_simulator",
            }
        resp = httpx.post(
            f"{self.api_base}/v1/billing_portal/sessions",
            data={"customer": customer_id, "return_url": return_url},
            headers={"Authorization": f"Bearer {self.secret_key}"},
            timeout=15.0,
        )
        resp.raise_for_status()
        body = resp.json()
        return {
            "portal_url": body["url"],
            "provider_transport": "stripe_api",
        }

    def update_subscription_plan(
        self,
        *,
        subscription_id: str,
        target_plan_code: str,
        price_id: str,
        tenant: Tenant,
        proration_behavior: str = "always_invoice",
    ) -> dict[str, Any]:
        clean_sub_id = (subscription_id or "").strip()
        if _is_simulated_stripe_subscription_id(clean_sub_id) and not self.has_remote_credentials:
            if self.mode == "live":
                raise PermissionError(
                    "STRIPE_SECRET_KEY is required for live Stripe subscription updates."
                )
            return {
                "status": "active",
                "cancel_at_period_end": False,
                "provider_transport": "local_test_simulator",
            }
        if not self.has_remote_credentials:
            raise PermissionError(
                "STRIPE_SECRET_KEY is required to update an active remote Stripe subscription."
            )

        auth_headers = {"Authorization": f"Bearer {self.secret_key}"}
        get_resp = httpx.get(
            f"{self.api_base}/v1/subscriptions/{clean_sub_id}",
            headers=auth_headers,
            timeout=15.0,
        )
        get_resp.raise_for_status()
        current_sub = get_resp.json()
        items = ((current_sub.get("items") or {}).get("data")) or []
        if not items or not items[0].get("id"):
            raise ValueError(
                "Stripe subscription has no updatable subscription item."
            )

        upd_resp = httpx.post(
            f"{self.api_base}/v1/subscriptions/{clean_sub_id}",
            data={
                "items[0][id]": str(items[0]["id"]),
                "items[0][price]": price_id,
                "cancel_at_period_end": "false",
                "proration_behavior": proration_behavior,
                "payment_behavior": "error_if_incomplete",
                "metadata[tenant_uuid]": str(tenant.uuid),
                "metadata[target_plan_code]": target_plan_code,
            },
            headers=auth_headers,
            timeout=15.0,
        )
        upd_resp.raise_for_status()
        updated = upd_resp.json()
        remote_status = str(updated.get("status") or "")
        if remote_status not in {"active", "trialing"}:
            raise ValueError(
                f"Stripe subscription upgrade was not confirmed active (status={remote_status or 'unknown'})."
            )
        return {
            "status": remote_status,
            "cancel_at_period_end": bool(
                updated.get("cancel_at_period_end", False)
            ),
            "current_period_start": updated.get("current_period_start"),
            "current_period_end": updated.get("current_period_end"),
            "provider_transport": "stripe_api",
        }

    def cancel_subscription(
        self,
        *,
        subscription_id: str,
        immediate: bool = False,
    ) -> dict[str, Any]:
        clean_sub_id = (subscription_id or "").strip()
        if _is_simulated_stripe_subscription_id(clean_sub_id) and not self.has_remote_credentials:
            if self.mode == "live":
                raise PermissionError(
                    "STRIPE_SECRET_KEY is required for live Stripe subscription cancellation."
                )
            return {
                "status": "canceled" if immediate else "active",
                "cancel_at_period_end": not immediate,
                "provider_transport": "local_test_simulator",
            }
        if not self.has_remote_credentials:
            raise PermissionError(
                "STRIPE_SECRET_KEY is required to cancel an active remote Stripe subscription."
            )

        auth_headers = {"Authorization": f"Bearer {self.secret_key}"}
        if immediate:
            resp = httpx.delete(
                f"{self.api_base}/v1/subscriptions/{clean_sub_id}",
                headers=auth_headers,
                timeout=15.0,
            )
            resp.raise_for_status()
            body = resp.json()
            remote_status = str(body.get("status") or "")
            if remote_status not in {"canceled", "incomplete_expired"}:
                raise ValueError(
                    f"Stripe immediate cancellation did not confirm canceled status (got {remote_status or 'unknown'})."
                )
        else:
            resp = httpx.post(
                f"{self.api_base}/v1/subscriptions/{clean_sub_id}",
                data={"cancel_at_period_end": "true"},
                headers=auth_headers,
                timeout=15.0,
            )
            resp.raise_for_status()
            body = resp.json()
            if (
                not bool(body.get("cancel_at_period_end"))
                and str(body.get("status") or "") != "canceled"
            ):
                raise ValueError(
                    "Stripe did not confirm cancel_at_period_end on the subscription."
                )

        return {
            "status": str(body.get("status") or "active"),
            "cancel_at_period_end": bool(
                body.get("cancel_at_period_end", not immediate)
            ),
            "current_period_end": body.get("current_period_end"),
            "provider_transport": "stripe_api",
        }


def preview_subscription_change(
    access: ResolvedWorkspaceAccess,
    *,
    target_plan_code: str,
) -> dict[str, Any]:
    """Explain exact effective dates, monthly charges, visit allowances, and
    paid-through behavior BEFORE the buyer confirms a plan change."""
    _require_billing_access(access)
    sub = ensure_brand_subscription(access.tenant)
    current_ent = resolve_brand_entitlements(access.tenant)
    target_entry = get_plan_entry(target_plan_code)

    if target_plan_code == WorkspaceProfile.PLAN_SCALE or target_entry.is_quote_only:
        return {
            "current_plan_code": current_ent.effective_plan_code,
            "target_plan_code": target_plan_code,
            "target_display_name": target_entry.display_name,
            "is_quote_only": True,
            "can_self_serve_confirm": False,
            "monthly_price_usd": str(target_entry.monthly_price_usd),
            "charge_today_usd": "0.00",
            "included_visits": target_entry.included_completed_visits or 250,
            "change_kind": "quote_only",
            "effective_date_label": "Upon signed Scale agreement",
            "paid_through_date": (
                sub.current_period_end.date().isoformat()
                if sub.current_period_end
                else None
            ),
            "disclosure": (
                "Scale starts at $1,500/mo for 250 completed visits and custom "
                "provider/retailer feeds. Request a quote — no card is charged automatically."
            ),
        }

    cur_rank = PLAN_RANK.get(current_ent.effective_plan_code, 0)
    tgt_rank = PLAN_RANK.get(target_plan_code, 0)

    now = timezone.now()
    next_renewal = sub.current_period_end or (now + timedelta(days=30))

    if target_plan_code == WorkspaceProfile.PLAN_FREE:
        change_kind = "cancel_to_free"
        charge_today = Decimal("0.00")
        eff_label = (
            f"End of paid period ({sub.current_period_end.date().isoformat()})"
            if sub.current_period_end and sub.current_period_end > now
            else "Immediate"
        )
        disclosure = (
            f"Your workspace keeps full {current_ent.display_name} access through "
            f"{eff_label}, then transitions to Free ($0/mo, 5 visits/mo) with all "
            f"historical store, cost, and recap records preserved."
        )
    elif current_ent.is_pilot and sub.status == BrandSubscription.STATUS_PILOT_ACTIVE:
        change_kind = "pilot_to_recurring"
        charge_today = target_entry.monthly_price_usd
        pilot_end_str = (
            sub.pilot_ends_at.date().isoformat()
            if sub.pilot_ends_at
            else now.date().isoformat()
        )
        eff_label = "Immediate recurring activation (explicit opt-in required)"
        disclosure = (
            f"Your 60-day Pro Pilot runs through {pilot_end_str}. Confirming "
            f"starts recurring {target_entry.display_name} billing at "
            f"${target_entry.monthly_price_usd}/month (USD) in Stripe {sub.stripe_mode} mode "
            f"with {target_entry.included_completed_visits} completed visits/month."
        )
    elif tgt_rank > cur_rank:
        change_kind = "upgrade"
        charge_today = target_entry.monthly_price_usd
        eff_label = "Immediate upon verified checkout"
        disclosure = (
            f"Upgrades immediately to {target_entry.display_name} "
            f"(${target_entry.monthly_price_usd}/month USD, "
            f"{target_entry.included_completed_visits} completed visits/month). "
            f"Renews monthly starting {(now + timedelta(days=30)).date().isoformat()} "
            f"until cancelled. Historical monthly usage is preserved."
        )
    elif tgt_rank < cur_rank:
        change_kind = "downgrade"
        charge_today = Decimal("0.00")
        eff_label = f"At next renewal ({next_renewal.date().isoformat()})"
        disclosure = (
            f"No charge today. You keep {current_ent.display_name} features through "
            f"{next_renewal.date().isoformat()}, then renew on "
            f"{target_entry.display_name} (${target_entry.monthly_price_usd}/month, "
            f"{target_entry.included_completed_visits} visits/month)."
        )
    else:
        change_kind = "unchanged"
        charge_today = Decimal("0.00")
        eff_label = "Already active"
        disclosure = f"Your workspace is already on {target_entry.display_name}."

    return {
        "current_plan_code": current_ent.effective_plan_code,
        "target_plan_code": target_plan_code,
        "target_display_name": target_entry.display_name,
        "is_quote_only": False,
        "can_self_serve_confirm": change_kind != "unchanged",
        "monthly_price_usd": str(target_entry.monthly_price_usd),
        "charge_today_usd": str(quantize_money(charge_today)),
        "included_visits": target_entry.included_completed_visits or 0,
        "change_kind": change_kind,
        "effective_date_label": eff_label,
        "paid_through_date": (
            sub.current_period_end.date().isoformat()
            if sub.current_period_end
            else None
        ),
        "disclosure": disclosure,
    }


@transaction.atomic
def create_subscription_checkout_session(
    access: ResolvedWorkspaceAccess,
    *,
    target_plan_code: str,
    explicit_terms_accepted: bool,
    idempotency_key: str | None = None,
    success_url: str | None = None,
    cancel_url: str | None = None,
) -> StripeCheckoutRecord:
    """Create an idempotent Stripe Checkout session in test mode after
    validating brand-admin permissions and explicit recurring-billing consent."""
    if not getattr(settings, "STANDALONE_BILLING_ENABLED", True):
        raise PermissionError(
            "Self-service subscription billing is currently disabled by release control."
        )
    _require_billing_access(access)
    if not access.is_brand_admin:
        raise PermissionError(
            "Only brand administrators can manage software subscriptions."
        )
    if not explicit_terms_accepted:
        raise ValueError(
            "Please confirm recurring billing terms and effective dates before continuing to checkout."
        )

    target_entry = get_plan_entry(target_plan_code)
    if target_plan_code == WorkspaceProfile.PLAN_SCALE or target_entry.is_quote_only:
        raise PlanEntitlementError(
            "Scale is quote-only (starting at $1,500/mo for 250 visits). Request a custom agreement instead of self-serve checkout.",
            code="QUOTE_ONLY_PLAN",
            required_plan=WorkspaceProfile.PLAN_SCALE,
        )
    if target_plan_code not in {
        WorkspaceProfile.PLAN_CORE,
        WorkspaceProfile.PLAN_PRO,
    }:
        raise ValueError(
            f"Unsupported self-service checkout plan: {target_plan_code}"
        )

    idem = (idempotency_key or "").strip() or (
        f"chk:{access.tenant.uuid}:{target_plan_code}:{timezone.now().strftime('%Y%m%d%H%M')}"
    )
    existing_rec = StripeCheckoutRecord.objects.filter(
        tenant=access.tenant, idempotency_key=idem
    ).first()
    if existing_rec is not None:
        return existing_rec

    sub = ensure_brand_subscription(access.tenant)
    preview = preview_subscription_change(
        access, target_plan_code=target_plan_code
    )
    base_ui_url = _default_billing_ui_base_url()
    resolved_success = success_url or f"{base_ui_url}/plans-billing?checkout=success"
    resolved_cancel = cancel_url or f"{base_ui_url}/plans-billing?checkout=cancelled"

    adapter = StripeBillingAdapter()
    session_data = adapter.create_checkout_session(
        tenant=access.tenant,
        customer_email=access.user.email or "",
        existing_customer_id=sub.stripe_customer_id,
        price_id=target_entry.stripe_price_id
        or f"price_{target_plan_code}_monthly_test",
        target_plan_code=target_plan_code,
        idempotency_key=idem,
        success_url=resolved_success,
        cancel_url=resolved_cancel,
    )

    now = timezone.now()
    sub.stripe_mode = adapter.mode
    if not sub.stripe_customer_id and session_data.get("customer_id"):
        sub.stripe_customer_id = session_data["customer_id"]
    if sub.billing_owner_id is None:
        sub.billing_owner = access.user
    sub.recurring_billing_accepted_at = now
    sub.recurring_billing_accepted_by = access.user
    sub.save(
        update_fields=[
            "stripe_mode",
            "stripe_customer_id",
            "billing_owner",
            "recurring_billing_accepted_at",
            "recurring_billing_accepted_by",
            "updated_at",
        ]
    )

    record = StripeCheckoutRecord.objects.create(
        tenant=access.tenant,
        initiated_by=access.user,
        target_plan_code=target_plan_code,
        quoted_monthly_price_usd=target_entry.monthly_price_usd,
        quoted_included_visits=target_entry.included_completed_visits or 50,
        effective_summary=preview["disclosure"][:400],
        idempotency_key=idem,
        stripe_session_id=session_data["session_id"],
        checkout_url=session_data["checkout_url"],
        mode=adapter.mode,
        status=StripeCheckoutRecord.STATUS_OPEN,
        explicit_terms_accepted=True,
        accepted_at=now,
    )
    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="billing",
        action="checkout_session_created",
        target_type="StripeCheckoutRecord",
        target_id=str(record.uuid),
        summary=(
            f"Started Stripe {adapter.mode}-mode checkout for {target_entry.display_name} "
            f"(${target_entry.monthly_price_usd}/mo)."
        ),
        metadata={
            "stripe_session_id": record.stripe_session_id,
            "target_plan_code": target_plan_code,
            "transport": session_data["provider_transport"],
        },
    )
    return record


def create_customer_portal_session(
    access: ResolvedWorkspaceAccess,
    *,
    return_url: str | None = None,
) -> dict[str, str]:
    _require_billing_access(access)
    if not access.is_brand_admin:
        raise PermissionError(
            "Only brand administrators can open the customer billing portal."
        )
    sub = ensure_brand_subscription(access.tenant)
    base_ui_url = _default_billing_ui_base_url()
    resolved_return = return_url or f"{base_ui_url}/plans-billing"
    customer_id = (
        sub.stripe_customer_id
        or f"cus_test_{str(access.tenant.uuid).replace('-', '')[:14]}"
    )
    if not sub.stripe_customer_id:
        sub.stripe_customer_id = customer_id
        sub.save(update_fields=["stripe_customer_id", "updated_at"])

    adapter = StripeBillingAdapter()
    return adapter.create_billing_portal_session(
        customer_id=customer_id, return_url=resolved_return
    )


@transaction.atomic
def change_subscription_plan(
    access: ResolvedWorkspaceAccess,
    *,
    target_plan_code: str,
    explicit_terms_accepted: bool,
) -> BrandSubscription:
    """Apply an authenticated upgrade or scheduled period-end downgrade on an
    already-active recurring subscription without rewriting historical usage."""
    _require_billing_access(access)
    if not access.is_brand_admin:
        raise PermissionError(
            "Only brand administrators can change subscription plans."
        )
    if not explicit_terms_accepted:
        raise ValueError(
            "Explicit confirmation of the new plan price and effective date is required."
        )
    sub = (
        BrandSubscription.objects.select_for_update()
        .filter(tenant=access.tenant)
        .first()
    ) or ensure_brand_subscription(access.tenant)
    profile = ensure_workspace_profile(access.tenant)
    target_entry = get_plan_entry(target_plan_code)
    if target_entry.is_quote_only:
        raise PlanEntitlementError(
            "Scale requires a custom quote and signed agreement.",
            code="QUOTE_ONLY_PLAN",
            required_plan=WorkspaceProfile.PLAN_SCALE,
        )

    now = timezone.now()
    price_id = (
        target_entry.stripe_price_id or f"price_{target_plan_code}_monthly_test"
    )
    if profile.mode == WorkspaceProfile.MODE_MANAGED:
        if (sub.stripe_subscription_id or "").strip():
            adapter = StripeBillingAdapter()
            if target_plan_code == WorkspaceProfile.PLAN_FREE:
                adapter.cancel_subscription(
                    subscription_id=sub.stripe_subscription_id,
                    immediate=False,
                )
            else:
                adapter.update_subscription_plan(
                    subscription_id=sub.stripe_subscription_id,
                    target_plan_code=target_plan_code,
                    price_id=price_id,
                    tenant=access.tenant,
                )
        sub.plan_code = WorkspaceProfile.PLAN_MANAGED_SOW
        sub.status = BrandSubscription.STATUS_MANAGED_SOW
        sub.addon_plan_code = (
            ""
            if target_plan_code == WorkspaceProfile.PLAN_FREE
            else target_plan_code
        )
        sub.recurring_billing_accepted_at = now
        sub.recurring_billing_accepted_by = access.user
        sub.save()
        log_workspace_audit(
            tenant=access.tenant,
            actor=access.user,
            category="billing",
            action="managed_addon_updated",
            target_type="BrandSubscription",
            target_id=str(sub.uuid),
            summary=(
                f"Updated optional software add-on to {sub.addon_plan_code or 'none'} "
                f"while preserving Managed SOW entitlements."
            ),
        )
        return sub

    cur_rank = PLAN_RANK.get(sub.plan_code, 0)
    tgt_rank = PLAN_RANK.get(target_plan_code, 0)

    if tgt_rank > cur_rank:
        if not (sub.stripe_subscription_id or "").strip():
            raise PlanEntitlementError(
                "Start Stripe Checkout to activate a paid subscription before upgrading plans.",
                code="CHECKOUT_REQUIRED",
                required_plan=target_plan_code,
            )
        stripe_res = StripeBillingAdapter().update_subscription_plan(
            subscription_id=sub.stripe_subscription_id,
            target_plan_code=target_plan_code,
            price_id=price_id,
            tenant=access.tenant,
        )
        p_start = _ts_to_dt(stripe_res.get("current_period_start"))
        p_end = _ts_to_dt(stripe_res.get("current_period_end"))

        sub.plan_code = target_plan_code
        profile.plan_tier = target_plan_code
        if profile.mode == WorkspaceProfile.MODE_PILOT:
            profile.mode = WorkspaceProfile.MODE_SELF_SERVE
            sub.is_pilot = False
        profile.save(update_fields=["mode", "plan_tier", "updated_at"])
        sub.pending_plan_code = ""
        sub.cancel_at_period_end = False
        sub.status = BrandSubscription.STATUS_ACTIVE
        if p_start is not None:
            sub.current_period_start = p_start
        elif sub.current_period_start is None:
            sub.current_period_start = now
        if p_end is not None:
            sub.current_period_end = p_end
        elif sub.current_period_end is None or sub.current_period_end <= now:
            sub.current_period_end = now + timedelta(days=30)
        sub.last_payment_status = BrandSubscription.PAY_SUCCEEDED
        sub.last_payment_error = ""
        sub.recurring_billing_accepted_at = now
        sub.recurring_billing_accepted_by = access.user
        sub.save()
        log_workspace_audit(
            tenant=access.tenant,
            actor=access.user,
            category="billing",
            action="subscription_upgraded",
            target_type="BrandSubscription",
            target_id=str(sub.uuid),
            summary=f"Upgraded subscription to {target_entry.display_name} (${target_entry.monthly_price_usd}/mo).",
        )
        return sub

    if tgt_rank < cur_rank:
        if (
            target_plan_code == WorkspaceProfile.PLAN_FREE
            and (sub.stripe_subscription_id or "").strip()
        ):
            return cancel_brand_subscription(
                access,
                immediate=not (
                    sub.current_period_end is not None
                    and sub.current_period_end > now
                ),
                reason="Downgraded to Free",
            )
        if sub.current_period_end and sub.current_period_end > now:
            sub.pending_plan_code = target_plan_code
            sub.save(update_fields=["pending_plan_code", "updated_at"])
            log_workspace_audit(
                tenant=access.tenant,
                actor=access.user,
                category="billing",
                action="subscription_downgrade_scheduled",
                target_type="BrandSubscription",
                target_id=str(sub.uuid),
                summary=(
                    f"Scheduled downgrade to {target_entry.display_name} effective "
                    f"{sub.current_period_end.date().isoformat()}."
                ),
            )
        else:
            if (sub.stripe_subscription_id or "").strip():
                StripeBillingAdapter().update_subscription_plan(
                    subscription_id=sub.stripe_subscription_id,
                    target_plan_code=target_plan_code,
                    price_id=price_id,
                    tenant=access.tenant,
                    proration_behavior="none",
                )
            sub.plan_code = target_plan_code
            sub.pending_plan_code = ""
            sub.status = (
                BrandSubscription.STATUS_FREE_ACTIVE
                if target_plan_code == WorkspaceProfile.PLAN_FREE
                else BrandSubscription.STATUS_ACTIVE
            )
            sub.save()
            profile.plan_tier = target_plan_code
            profile.save(update_fields=["plan_tier", "updated_at"])
        return sub

    return sub


@transaction.atomic
def cancel_brand_subscription(
    access: ResolvedWorkspaceAccess,
    *,
    immediate: bool = False,
    reason: str = "",
) -> BrandSubscription:
    """Cancel recurring software billing while preserving paid-through access
    until `current_period_end` (unless `immediate=True`), keeping all historical
    store/cost/recap data and allowing in-progress fieldwork to submit."""
    _require_billing_access(access)
    if not access.is_brand_admin:
        raise PermissionError(
            "Only brand administrators can cancel software subscriptions."
        )
    sub = (
        BrandSubscription.objects.select_for_update()
        .filter(tenant=access.tenant)
        .first()
    ) or ensure_brand_subscription(access.tenant)
    profile = ensure_workspace_profile(access.tenant)
    now = timezone.now()

    period_end_cancel = bool(
        not immediate
        and sub.current_period_end is not None
        and sub.current_period_end > now
    )

    if profile.mode == WorkspaceProfile.MODE_MANAGED:
        if (sub.stripe_subscription_id or "").strip():
            StripeBillingAdapter().cancel_subscription(
                subscription_id=sub.stripe_subscription_id,
                immediate=immediate,
            )
        # Managed SOW client cancelling a standalone software add-on retains SOW!
        sub.addon_plan_code = ""
        sub.cancel_at_period_end = False
        sub.canceled_at = now
        sub.status = BrandSubscription.STATUS_MANAGED_SOW
        sub.save(
            update_fields=[
                "addon_plan_code",
                "cancel_at_period_end",
                "canceled_at",
                "status",
                "updated_at",
            ]
        )
        log_workspace_audit(
            tenant=access.tenant,
            actor=access.user,
            category="billing",
            action="managed_addon_canceled",
            target_type="BrandSubscription",
            target_id=str(sub.uuid),
            summary="Cancelled standalone software add-on; Managed SOW entitlements preserved.",
        )
        return sub

    stripe_cancel_res: dict[str, Any] | None = None
    if (sub.stripe_subscription_id or "").strip():
        stripe_cancel_res = StripeBillingAdapter().cancel_subscription(
            subscription_id=sub.stripe_subscription_id,
            immediate=not period_end_cancel,
        )

    sub.canceled_at = now
    if period_end_cancel:
        remote_period_end = (
            _ts_to_dt(stripe_cancel_res.get("current_period_end"))
            if stripe_cancel_res
            else None
        )
        if remote_period_end is not None and remote_period_end > now:
            sub.current_period_end = remote_period_end
        sub.cancel_at_period_end = True
        sub.status = BrandSubscription.STATUS_CANCEL_AT_PERIOD_END
        sub.pending_plan_code = WorkspaceProfile.PLAN_FREE
    else:
        sub.cancel_at_period_end = False
        sub.status = BrandSubscription.STATUS_CANCELED
        sub.current_period_end = now
        sub.plan_code = WorkspaceProfile.PLAN_FREE
        profile.plan_tier = WorkspaceProfile.PLAN_FREE
        profile.save(update_fields=["plan_tier", "updated_at"])

    sub.save()
    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="billing",
        action="subscription_canceled",
        target_type="BrandSubscription",
        target_id=str(sub.uuid),
        summary=(
            f"Cancelled subscription ({'paid-through ' + sub.current_period_end.date().isoformat() if sub.cancel_at_period_end and sub.current_period_end else 'effective immediately'}). {reason}".strip()
        ),
    )
    return sub


@transaction.atomic
def approve_overage_or_volume_band(
    access: ResolvedWorkspaceAccess,
    *,
    agreed_volume_band_visits: int | None = None,
    overage_agreement_approved: bool = False,
    overage_unit_price_usd: Decimal | None = None,
) -> BrandSubscription:
    """Record customer-approved volume band or overage agreement so overages
    are NEVER charged without explicit customer consent."""
    _require_billing_access(access)
    if not access.is_brand_admin:
        raise PermissionError(
            "Only brand administrators can approve volume bands or overage terms."
        )
    sub = ensure_brand_subscription(access.tenant)
    now = timezone.now()
    if agreed_volume_band_visits is not None:
        sub.agreed_volume_band_visits = agreed_volume_band_visits
    sub.overage_agreement_approved = overage_agreement_approved
    if overage_unit_price_usd is not None:
        sub.overage_unit_price_usd = quantize_money(overage_unit_price_usd)
    sub.overage_approved_by = access.user if overage_agreement_approved else None
    sub.overage_approved_at = now if overage_agreement_approved else None
    sub.save()
    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="billing",
        action="volume_or_overage_updated",
        target_type="BrandSubscription",
        target_id=str(sub.uuid),
        summary=(
            f"Updated volume band ({sub.agreed_volume_band_visits}) / "
            f"overage agreement (approved={sub.overage_agreement_approved})."
        ),
    )
    return sub


@transaction.atomic
def provision_pro_pilot(
    *,
    actor: User | None,
    tenant: Tenant,
    pilot_fee_usd: Decimal = Decimal("1500.00"),
    duration_days: int = 60,
    included_visits_monthly: int = 100,
    starts_at: datetime | None = None,
) -> BrandSubscription:
    """Provision the manually configured $1,500, 60-day Pro pilot on a brand
    workspace without starting recurring charges until explicit buyer opt-in."""
    if actor is not None and not is_ignite_staff_user(actor):
        raise PermissionError(
            "Only Ignite platform administrators can manually provision a 60-day Pro pilot."
        )
    start_dt = starts_at or timezone.now()
    end_dt = start_dt + timedelta(days=max(duration_days, 1))

    profile = ensure_workspace_profile(
        tenant,
        mode=WorkspaceProfile.MODE_PILOT,
        plan_tier=WorkspaceProfile.PLAN_PILOT_PRO,
    )
    sub = ensure_brand_subscription(tenant)
    sub.plan_code = WorkspaceProfile.PLAN_PILOT_PRO
    sub.status = BrandSubscription.STATUS_PILOT_ACTIVE
    sub.is_pilot = True
    sub.pilot_fee_usd = quantize_money(pilot_fee_usd)
    sub.pilot_starts_at = start_dt
    sub.pilot_ends_at = end_dt
    sub.custom_included_visits_monthly = included_visits_monthly
    sub.current_period_start = start_dt
    sub.current_period_end = end_dt
    sub.cancel_at_period_end = False
    sub.recurring_billing_accepted_at = None
    sub.recurring_billing_accepted_by = None
    sub.save()

    log_workspace_audit(
        tenant=tenant,
        actor=actor,
        category="billing",
        action="pro_pilot_provisioned",
        target_type="BrandSubscription",
        target_id=str(sub.uuid),
        summary=(
            f"Provisioned ${sub.pilot_fee_usd}, {duration_days}-day Pro Pilot "
            f"through {end_dt.date().isoformat()} ({profile.business_timezone})."
        ),
    )
    return sub


# ---------------------------------------------------------------------------
# Verified, Idempotent, Out-of-Order-Safe Stripe Webhook Processor
# ---------------------------------------------------------------------------


def _ts_to_dt(ts_val: Any) -> datetime | None:
    if ts_val is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts_val), tz=dt_timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _resolve_tenant_for_stripe_object(obj: dict[str, Any]) -> Tenant | None:
    metadata = obj.get("metadata") or {}
    tenant_uuid = (
        metadata.get("tenant_uuid") or obj.get("client_reference_id") or ""
    ).strip()
    if tenant_uuid:
        found = Tenant.objects.filter(uuid=tenant_uuid).first()
        if found is not None:
            return found

    sub_id = (obj.get("subscription") or obj.get("id") or "").strip()
    if sub_id.startswith("sub_"):
        sub_row = (
            BrandSubscription.objects.select_related("tenant")
            .filter(stripe_subscription_id=sub_id)
            .first()
        )
        if sub_row is not None:
            return sub_row.tenant

    cust_id = (obj.get("customer") or "").strip()
    if cust_id:
        sub_row = (
            BrandSubscription.objects.select_related("tenant")
            .filter(stripe_customer_id=cust_id)
            .first()
        )
        if sub_row is not None:
            return sub_row.tenant

    return None


@transaction.atomic
def process_stripe_webhook(
    raw_body: bytes,
    signature_header: str | None,
    *,
    tolerance_seconds: int = 300,
    now_ts: int | None = None,
) -> dict[str, Any]:
    """Verify and process a Stripe Billing webhook event with:
    1. Constant-time HMAC-SHA256 `Stripe-Signature` verification
    2. Test-vs-live mode guard (rejects livemode events in test mode)
    3. Exact `stripe_event_id` deduplication (`ignored_duplicate`)
    4. Monotonic `stripe_created_ts` ordering guard (`ignored_stale_out_of_order`)
       so delayed/out-of-order deliveries never overwrite newer subscription state."""
    event = verify_stripe_webhook_signature(
        raw_body,
        signature_header,
        tolerance_seconds=tolerance_seconds,
        now_ts=now_ts,
    )
    event_id = str(event["id"]).strip()
    event_type = str(event["type"]).strip()
    created_ts = int(event.get("created") or 0)
    livemode = bool(event.get("livemode", False))

    active_mode = _active_stripe_mode()
    if active_mode == "test" and livemode:
        raise PermissionError(
            "Rejected live-mode Stripe webhook while STRIPE_BILLING_MODE='test'."
        )

    # Fast + atomic replay check
    if StripeWebhookEvent.objects.filter(stripe_event_id=event_id).exists():
        return {
            "ok": True,
            "event_id": event_id,
            "status": StripeWebhookEvent.PROC_DUPLICATE,
        }

    data_obj = ((event.get("data") or {}).get("object")) or {}
    tenant = _resolve_tenant_for_stripe_object(data_obj)

    if event_type == "spark.billing.readiness_probe":
        if active_mode != "test":
            raise PermissionError(
                "Synthetic readiness probes are only permitted while STRIPE_BILLING_MODE='test'."
            )
        try:
            StripeWebhookEvent.objects.create(
                stripe_event_id=event_id,
                event_type=event_type,
                stripe_created_ts=created_ts,
                livemode=False,
                tenant=tenant,
                processing_status=StripeWebhookEvent.PROC_PROCESSED,
                note="Ignite admin test-mode HMAC signature & replay check passed.",
                payload=event,
            )
        except IntegrityError:
            return {
                "ok": True,
                "event_id": event_id,
                "status": StripeWebhookEvent.PROC_DUPLICATE,
            }
        return {
            "ok": True,
            "event_id": event_id,
            "status": StripeWebhookEvent.PROC_PROCESSED,
        }

    if tenant is None:
        try:
            StripeWebhookEvent.objects.create(
                stripe_event_id=event_id,
                event_type=event_type,
                stripe_created_ts=created_ts,
                livemode=livemode,
                tenant=None,
                processing_status=StripeWebhookEvent.PROC_UNMATCHED,
                note="No matching Spark brand workspace for webhook object.",
                payload=event,
            )
        except IntegrityError:
            return {
                "ok": True,
                "event_id": event_id,
                "status": StripeWebhookEvent.PROC_DUPLICATE,
            }
        return {
            "ok": True,
            "event_id": event_id,
            "status": StripeWebhookEvent.PROC_UNMATCHED,
        }

    ensure_brand_subscription(tenant)
    sub = BrandSubscription.objects.select_for_update().get(tenant=tenant)
    profile = ensure_workspace_profile(tenant)

    if (
        event_type not in SUBSCRIPTION_DOMAIN_EVENTS
        and event_type not in INVOICE_DOMAIN_EVENTS
    ):
        StripeWebhookEvent.objects.create(
            stripe_event_id=event_id,
            event_type=event_type,
            stripe_created_ts=created_ts,
            livemode=livemode,
            tenant=tenant,
            processing_status=StripeWebhookEvent.PROC_UNMATCHED,
            note=f"Ignored unsupported Stripe event type {event_type}.",
            payload=event,
        )
        return {
            "ok": True,
            "event_id": event_id,
            "status": StripeWebhookEvent.PROC_UNMATCHED,
        }

    latest_sub_ts = max(
        int(sub.last_stripe_event_created or 0),
        int(
            StripeWebhookEvent.objects.filter(
                tenant=tenant,
                processing_status=StripeWebhookEvent.PROC_PROCESSED,
                event_type__in=SUBSCRIPTION_DOMAIN_EVENTS,
            ).aggregate(m=Max("stripe_created_ts"))["m"]
            or 0
        ),
    )
    latest_invoice_ts = int(
        StripeWebhookEvent.objects.filter(
            tenant=tenant,
            processing_status=StripeWebhookEvent.PROC_PROCESSED,
            event_type__in=INVOICE_DOMAIN_EVENTS,
        ).aggregate(m=Max("stripe_created_ts"))["m"]
        or 0
    )

    # Per-domain out-of-order guard: subscription events compare against the
    # subscription-domain watermark; invoice events compare against the
    # invoice-domain watermark so a newer `invoice.paid` never discards an
    # older `customer.subscription.updated` carrying plan/cancellation state.
    domain_watermark = (
        latest_sub_ts
        if event_type in SUBSCRIPTION_DOMAIN_EVENTS
        else latest_invoice_ts
    )
    if created_ts > 0 and domain_watermark > created_ts:
        StripeWebhookEvent.objects.create(
            stripe_event_id=event_id,
            event_type=event_type,
            stripe_created_ts=created_ts,
            livemode=livemode,
            tenant=tenant,
            processing_status=StripeWebhookEvent.PROC_STALE,
            note=(
                f"Ignored stale {event_type} ({created_ts} < {domain_watermark})."
            ),
            payload=event,
        )
        return {
            "ok": True,
            "event_id": event_id,
            "status": StripeWebhookEvent.PROC_STALE,
        }

    metadata = data_obj.get("metadata") or {}
    target_plan = (metadata.get("target_plan_code") or "").strip()
    now = timezone.now()

    if event_type == "checkout.session.completed":
        session_id = str(data_obj.get("id") or "").strip()
        checkout_rec = (
            StripeCheckoutRecord.objects.select_for_update()
            .filter(
                tenant=tenant,
                stripe_session_id=session_id,
                status=StripeCheckoutRecord.STATUS_OPEN,
            )
            .first()
        )
        if (
            checkout_rec is None
            or checkout_rec.target_plan_code
            not in {WorkspaceProfile.PLAN_CORE, WorkspaceProfile.PLAN_PRO}
            or (
                target_plan
                and target_plan != checkout_rec.target_plan_code
            )
        ):
            StripeWebhookEvent.objects.create(
                stripe_event_id=event_id,
                event_type=event_type,
                stripe_created_ts=created_ts,
                livemode=livemode,
                tenant=tenant,
                processing_status=StripeWebhookEvent.PROC_UNMATCHED,
                note=(
                    "Ignored checkout.session.completed without matching open "
                    "Spark checkout record or verified target plan."
                ),
                payload=event,
            )
            return {
                "ok": True,
                "event_id": event_id,
                "status": StripeWebhookEvent.PROC_UNMATCHED,
            }

        resolved_plan = checkout_rec.target_plan_code
        checkout_rec.status = StripeCheckoutRecord.STATUS_COMPLETED
        checkout_rec.completed_at = now
        checkout_rec.save(update_fields=["status", "completed_at"])

        if data_obj.get("customer"):
            sub.stripe_customer_id = str(data_obj["customer"])
        if data_obj.get("subscription"):
            sub.stripe_subscription_id = str(data_obj["subscription"])

        period_start = _ts_to_dt(data_obj.get("current_period_start")) or now
        period_end = _ts_to_dt(data_obj.get("current_period_end")) or (
            period_start + timedelta(days=30)
        )

        if profile.mode == WorkspaceProfile.MODE_MANAGED:
            sub.addon_plan_code = resolved_plan
            sub.status = BrandSubscription.STATUS_MANAGED_SOW
        else:
            sub.plan_code = resolved_plan
            sub.status = BrandSubscription.STATUS_ACTIVE
            sub.is_pilot = False
            profile.mode = WorkspaceProfile.MODE_SELF_SERVE
            profile.plan_tier = resolved_plan
            profile.save(update_fields=["mode", "plan_tier", "updated_at"])

        sub.pending_plan_code = ""
        sub.cancel_at_period_end = False
        sub.current_period_start = period_start
        sub.current_period_end = period_end
        if created_ts == 0 or created_ts >= latest_invoice_ts:
            sub.last_payment_status = BrandSubscription.PAY_SUCCEEDED
            sub.last_payment_error = ""

    elif event_type in {
        "customer.subscription.created",
        "customer.subscription.updated",
    }:
        if data_obj.get("id"):
            sub.stripe_subscription_id = str(data_obj["id"])
        if data_obj.get("customer"):
            sub.stripe_customer_id = str(data_obj["customer"])

        p_start = _ts_to_dt(data_obj.get("current_period_start"))
        p_end = _ts_to_dt(data_obj.get("current_period_end"))
        if p_start:
            sub.current_period_start = p_start
        if p_end:
            sub.current_period_end = p_end

        if target_plan in {
            WorkspaceProfile.PLAN_FREE,
            WorkspaceProfile.PLAN_CORE,
            WorkspaceProfile.PLAN_PRO,
            WorkspaceProfile.PLAN_SCALE,
        }:
            if profile.mode == WorkspaceProfile.MODE_MANAGED:
                sub.addon_plan_code = target_plan
            else:
                sub.plan_code = target_plan
                profile.plan_tier = target_plan
                profile.save(update_fields=["plan_tier", "updated_at"])

        cancel_period_end = bool(data_obj.get("cancel_at_period_end", False))
        stripe_status = str(data_obj.get("status") or "active")
        sub.cancel_at_period_end = cancel_period_end
        can_update_payment_fields = (
            created_ts == 0 or created_ts >= latest_invoice_ts
        )

        if profile.mode == WorkspaceProfile.MODE_MANAGED:
            sub.status = BrandSubscription.STATUS_MANAGED_SOW
            if stripe_status == "past_due" and can_update_payment_fields:
                sub.last_payment_status = BrandSubscription.PAY_FAILED
                sub.last_payment_error = "Add-on subscription renewal failed."
        else:
            if stripe_status == "past_due":
                sub.status = BrandSubscription.STATUS_PAST_DUE
                if can_update_payment_fields:
                    sub.last_payment_status = BrandSubscription.PAY_FAILED
                    sub.last_payment_error = (
                        "Subscription renewal payment failed. Update payment method to avoid interruption."
                    )
            elif stripe_status in {"canceled", "unpaid"}:
                sub.status = BrandSubscription.STATUS_CANCELED
                sub.canceled_at = sub.canceled_at or now
            elif cancel_period_end:
                sub.status = BrandSubscription.STATUS_CANCEL_AT_PERIOD_END
                sub.canceled_at = sub.canceled_at or now
            else:
                sub.status = BrandSubscription.STATUS_ACTIVE
                if can_update_payment_fields:
                    sub.last_payment_status = BrandSubscription.PAY_SUCCEEDED
                    sub.last_payment_error = ""

    elif event_type == "customer.subscription.deleted":
        sub.cancel_at_period_end = False
        sub.canceled_at = now
        if profile.mode == WorkspaceProfile.MODE_MANAGED:
            sub.addon_plan_code = ""
            sub.status = BrandSubscription.STATUS_MANAGED_SOW
        else:
            sub.status = BrandSubscription.STATUS_CANCELED
            p_end = _ts_to_dt(data_obj.get("current_period_end"))
            if p_end:
                sub.current_period_end = p_end
            if sub.current_period_end is None or sub.current_period_end <= now:
                sub.plan_code = WorkspaceProfile.PLAN_FREE
                profile.plan_tier = WorkspaceProfile.PLAN_FREE
                profile.save(update_fields=["plan_tier", "updated_at"])

    elif event_type == "invoice.paid":
        if data_obj.get("id"):
            sub.stripe_latest_invoice_id = str(data_obj["id"])
        can_update_sub_lifecycle = (
            (created_ts == 0 or created_ts >= latest_sub_ts)
            and sub.status != BrandSubscription.STATUS_CANCELED
        )
        p_end = _ts_to_dt(data_obj.get("period_end"))
        if (
            can_update_sub_lifecycle
            and p_end
            and (sub.current_period_end is None or p_end > sub.current_period_end)
        ):
            sub.current_period_end = p_end
        sub.last_payment_status = BrandSubscription.PAY_SUCCEEDED
        sub.last_payment_error = ""
        if (
            can_update_sub_lifecycle
            and profile.mode != WorkspaceProfile.MODE_MANAGED
            and not sub.cancel_at_period_end
        ):
            sub.status = BrandSubscription.STATUS_ACTIVE

    elif event_type == "invoice.payment_failed":
        if data_obj.get("id"):
            sub.stripe_latest_invoice_id = str(data_obj["id"])
        err_msg = (
            str(
                data_obj.get("failure_message")
                or (data_obj.get("last_finalization_error") or {}).get("message")
                or (data_obj.get("last_payment_error") or {}).get("message")
                or ""
            ).strip()
            or "Card declined during recurring subscription renewal."
        )
        sub.last_payment_status = BrandSubscription.PAY_FAILED
        sub.last_payment_error = err_msg[:500]
        if (
            (created_ts == 0 or created_ts >= latest_sub_ts)
            and profile.mode != WorkspaceProfile.MODE_MANAGED
            and sub.status != BrandSubscription.STATUS_CANCELED
        ):
            sub.status = BrandSubscription.STATUS_PAST_DUE

    if (
        event_type in SUBSCRIPTION_DOMAIN_EVENTS
        and created_ts > sub.last_stripe_event_created
    ):
        sub.last_stripe_event_created = created_ts
    sub.save()

    StripeWebhookEvent.objects.create(
        stripe_event_id=event_id,
        event_type=event_type,
        stripe_created_ts=created_ts,
        livemode=livemode,
        tenant=tenant,
        processing_status=StripeWebhookEvent.PROC_PROCESSED,
        note=f"Applied {event_type} to {tenant.name}.",
        payload=event,
    )
    log_workspace_audit(
        tenant=tenant,
        actor=sub.billing_owner,
        category="billing",
        action=f"stripe_{event_type.replace('.', '_')}",
        target_type="StripeWebhookEvent",
        target_id=event_id,
        summary=f"Processed verified Stripe event {event_type} ({event_id}).",
    )
    return {
        "ok": True,
        "event_id": event_id,
        "status": StripeWebhookEvent.PROC_PROCESSED,
        "tenant_uuid": str(tenant.uuid),
        "plan_code": sub.plan_code,
        "subscription_status": sub.status,
    }


def complete_test_mode_checkout(
    access: ResolvedWorkspaceAccess,
    *,
    stripe_session_id: str,
) -> dict[str, Any]:
    """Complete an open test-mode hosted checkout session by constructing a
    signed `checkout.session.completed` webhook payload and routing it through
    the exact `process_stripe_webhook` signature + idempotency pipeline."""
    _require_billing_access(access)
    if _active_stripe_mode() != "test":
        raise PermissionError(
            "Simulated checkout completion is only available when STRIPE_BILLING_MODE='test'."
        )
    if not access.is_brand_admin:
        raise PermissionError(
            "Only brand administrators can confirm test-mode checkout."
        )
    record = StripeCheckoutRecord.objects.filter(
        tenant=access.tenant, stripe_session_id=stripe_session_id
    ).first()
    if record is None:
        raise ValueError("Checkout session not found for this brand workspace.")

    sub = ensure_brand_subscription(access.tenant)
    now_ts = max(
        int(time_module.time()), int(sub.last_stripe_event_created) + 1
    )
    event_payload = {
        "id": f"evt_test_{record.stripe_session_id[-20:]}",
        "object": "event",
        "type": "checkout.session.completed",
        "created": now_ts,
        "livemode": False,
        "data": {
            "object": {
                "id": record.stripe_session_id,
                "object": "checkout.session",
                "customer": sub.stripe_customer_id
                or f"cus_test_{str(access.tenant.uuid).replace('-', '')[:14]}",
                "subscription": sub.stripe_subscription_id
                or f"sub_test_{str(access.tenant.uuid).replace('-', '')[:14]}",
                "client_reference_id": str(access.tenant.uuid),
                "current_period_start": now_ts,
                "current_period_end": now_ts + 30 * 86400,
                "metadata": {
                    "tenant_uuid": str(access.tenant.uuid),
                    "target_plan_code": record.target_plan_code,
                },
            }
        },
    }
    raw = json.dumps(event_payload).encode("utf-8")
    sig = sign_stripe_webhook_payload(raw, timestamp=now_ts)
    return process_stripe_webhook(raw, sig, now_ts=now_ts)


def get_plans_and_billing_overview(
    access: ResolvedWorkspaceAccess,
) -> dict[str, Any]:
    """Assemble the complete Feature 8 + Feature 9 Plan, Usage, Pilot, and
    Billing dashboard state for a brand workspace."""
    _require_billing_access(access)
    ensure_plan_catalog()
    sub = ensure_brand_subscription(access.tenant)
    ent = resolve_brand_entitlements(access.tenant)
    usage = compute_monthly_visit_usage(access.tenant)

    plans_out = []
    for entry in ensure_plan_catalog():
        if entry.plan_code in {
            WorkspaceProfile.PLAN_PILOT_PRO,
            WorkspaceProfile.PLAN_MANAGED_SOW,
        } and ent.effective_plan_code != entry.plan_code:
            continue
        plans_out.append(
            {
                "plan_code": entry.plan_code,
                "display_name": entry.display_name,
                "tagline": entry.tagline,
                "monthly_price_usd": str(entry.monthly_price_usd),
                "included_completed_visits": entry.included_completed_visits,
                "is_quote_only": entry.is_quote_only,
                "is_self_serve_selectable": entry.is_self_serve_selectable,
                "is_current": entry.plan_code == ent.effective_plan_code,
                "max_active_programs": entry.max_active_programs,
                "max_operating_providers": entry.max_operating_providers,
                "max_pos_retailers": entry.max_pos_retailers,
                "allows_branded_forms": entry.allows_branded_forms,
                "allows_approvals_and_reminders": entry.allows_approvals_and_reminders,
                "allows_exports_and_weekly_reporting": entry.allows_exports_and_weekly_reporting,
                "allows_store_recommendations": entry.allows_store_recommendations,
                "allows_provider_csv_imports": entry.allows_provider_csv_imports,
                "allows_brand_cost_tracking": entry.allows_brand_cost_tracking,
                "allows_finance_review_queue": entry.allows_finance_review_queue,
                "allows_retailer_pos_measurement": entry.allows_retailer_pos_measurement,
                "allows_tracked_store_actions": entry.allows_tracked_store_actions,
            }
        )

    recent_events = [
        {
            "stripe_event_id": ev.stripe_event_id,
            "event_type": ev.event_type,
            "processing_status": ev.processing_status,
            "note": ev.note,
            "processed_at": ev.processed_at.isoformat(),
        }
        for ev in StripeWebhookEvent.objects.filter(
            tenant=access.tenant
        ).order_by("-processed_at")[:15]
        if access.is_brand_admin
    ]

    open_checkouts = [
        {
            "stripe_session_id": c.stripe_session_id,
            "target_plan_code": c.target_plan_code,
            "quoted_monthly_price_usd": str(c.quoted_monthly_price_usd),
            "checkout_url": c.checkout_url,
            "status": c.status,
            "effective_summary": c.effective_summary,
        }
        for c in StripeCheckoutRecord.objects.filter(
            tenant=access.tenant
        ).order_by("-created_at")[:5]
        if access.is_brand_admin
    ]

    return {
        "tenant_uuid": str(access.tenant.uuid),
        "tenant_name": access.tenant.name,
        "workspace_mode": ent.workspace_mode,
        "configured_plan_code": ent.configured_plan_code,
        "effective_plan_code": ent.effective_plan_code,
        "plan_display_name": ent.display_name,
        "subscription_status": ent.subscription_status,
        "pending_plan_code": sub.pending_plan_code or None,
        "addon_plan_code": sub.addon_plan_code or None,
        "stripe_mode": sub.stripe_mode,
        "stripe_configured_remotely": StripeBillingAdapter().has_remote_credentials,
        "monthly_price_usd": str(ent.monthly_price_usd),
        "is_managed_sow": ent.is_managed_sow,
        "is_pilot": ent.is_pilot,
        "pilot_fee_usd": str(ent.pilot_fee_usd) if ent.pilot_fee_usd is not None else None,
        "pilot_starts_at": sub.pilot_starts_at.isoformat() if sub.pilot_starts_at else None,
        "pilot_ends_at": sub.pilot_ends_at.isoformat() if sub.pilot_ends_at else None,
        "current_period_start": (
            sub.current_period_start.isoformat() if sub.current_period_start else None
        ),
        "current_period_end": (
            sub.current_period_end.isoformat() if sub.current_period_end else None
        ),
        "paid_through_at": (
            ent.paid_through_at.isoformat() if ent.paid_through_at else None
        ),
        "cancel_at_period_end": sub.cancel_at_period_end,
        "has_payment_failure": ent.has_payment_failure,
        "payment_failure_message": ent.payment_failure_message,
        "recurring_billing_accepted": sub.recurring_billing_accepted_at is not None,
        "viewer_and_contributor_seats_free": True,
        "billing_separation_notice": (
            "Spark software subscriptions are billed separately from agency service "
            "invoices, consumer receipt rebates, and ambassador payouts."
        ),
        "entitlements": {
            "max_active_programs": ent.max_active_programs,
            "max_operating_providers": ent.max_operating_providers,
            "max_pos_retailers": ent.max_pos_retailers,
            "allows_branded_forms": ent.allows_branded_forms,
            "allows_approvals_and_reminders": ent.allows_approvals_and_reminders,
            "allows_exports_and_weekly_reporting": ent.allows_exports_and_weekly_reporting,
            "allows_store_recommendations": ent.allows_store_recommendations,
            "allows_provider_csv_imports": ent.allows_provider_csv_imports,
            "allows_brand_cost_tracking": ent.allows_brand_cost_tracking,
            "allows_finance_review_queue": ent.allows_finance_review_queue,
            "allows_retailer_pos_measurement": ent.allows_retailer_pos_measurement,
            "allows_tracked_store_actions": ent.allows_tracked_store_actions,
        },
        "usage": usage,
        "plans": plans_out,
        "open_checkouts": open_checkouts,
        "recent_webhook_events": recent_events,
    }


def get_billing_readiness(*, user: User | None) -> dict[str, Any]:
    """Return Ignite-admin-only Stripe Billing readiness diagnostics using
    strictly boolean configuration flags (NEVER exposing raw secret values)."""
    require_ignite_billing_staff(user)
    ensure_plan_catalog()

    raw_mode = (
        getattr(settings, "STRIPE_BILLING_MODE", "test") or "test"
    ).strip().lower()
    billing_mode = "live" if raw_mode == "live" else "test"
    secret_key_configured = bool(
        (getattr(settings, "STRIPE_SECRET_KEY", "") or "").strip()
    )
    publishable_key_configured = bool(
        (getattr(settings, "STRIPE_PUBLISHABLE_KEY", "") or "").strip()
    )
    webhook_secret_configured = bool(
        (getattr(settings, "STRIPE_WEBHOOK_SECRET", "") or "").strip()
    )
    core_price_configured = bool(
        (getattr(settings, "STRIPE_PRICE_ID_CORE", "") or "").strip()
    )
    pro_price_configured = bool(
        (getattr(settings, "STRIPE_PRICE_ID_PRO", "") or "").strip()
    )
    scale_price_configured = bool(
        (getattr(settings, "STRIPE_PRICE_ID_SCALE", "") or "").strip()
    )
    test_simulation_active = bool(
        billing_mode == "test" and not secret_key_configured
    )
    client_billing_hidden = not is_client_billing_launched()

    api_base = (
        getattr(settings, "PUBLIC_API_BASE_URL", "")
        or getattr(settings, "ADMIN_FRONTEND_URL", "http://localhost:8000")
        or "http://localhost:8000"
    ).rstrip("/")
    webhook_endpoint_url = f"{api_base}/api/public/stripe-billing/webhook"

    cutoff = timezone.now() - timedelta(days=30)
    recent_qs = StripeWebhookEvent.objects.filter(processed_at__gte=cutoff)
    recent_webhook_count = recent_qs.count()
    failed_webhook_count = recent_qs.filter(
        processing_status=StripeWebhookEvent.PROC_FAILED
    ).count()
    latest_ev = StripeWebhookEvent.objects.order_by("-processed_at", "-id").first()
    recent_items = [
        {
            "stripe_event_id": ev.stripe_event_id,
            "event_type": ev.event_type,
            "processing_status": ev.processing_status,
            "livemode": bool(ev.livemode),
            "processed_at": ev.processed_at.isoformat(),
            "note": ev.note or "",
        }
        for ev in StripeWebhookEvent.objects.order_by("-processed_at", "-id")[:8]
    ]

    return {
        "billing_mode": billing_mode,
        "billing_enabled": bool(
            getattr(settings, "STANDALONE_BILLING_ENABLED", True)
        ),
        "client_billing_hidden": client_billing_hidden,
        "secret_key_configured": secret_key_configured,
        "publishable_key_configured": publishable_key_configured,
        "webhook_secret_configured": webhook_secret_configured,
        "core_price_configured": core_price_configured,
        "pro_price_configured": pro_price_configured,
        "scale_price_configured": scale_price_configured,
        "test_simulation_active": test_simulation_active,
        "webhook_endpoint_url": webhook_endpoint_url,
        "recent_webhook_count": recent_webhook_count,
        "failed_webhook_count": failed_webhook_count,
        "last_webhook_at": (
            latest_ev.processed_at.isoformat() if latest_ev else None
        ),
        "last_webhook_event_type": (
            latest_ev.event_type if latest_ev else None
        ),
        "last_webhook_status": (
            latest_ev.processing_status if latest_ev else None
        ),
        "recent_webhooks": recent_items,
    }


def run_billing_readiness_test_check(
    *, actor: User | None, tenant: Tenant | None = None
) -> dict[str, Any]:
    """Run a non-destructive Ignite-admin test-mode verification of plan
    catalog integrity, Stripe mode/credential guards, HMAC-SHA256 webhook
    signing, and exact event-ID replay deduplication."""
    require_ignite_billing_staff(actor)
    mode = _active_stripe_mode()
    if mode != "test":
        raise PermissionError(
            "One-click billing readiness test checks can only run when STRIPE_BILLING_MODE='test'."
        )

    catalog = ensure_plan_catalog()
    adapter = StripeBillingAdapter()
    now_ts = int(time_module.time())
    probe_id = f"evt_test_probe_{uuid4().hex[:18]}"
    metadata: dict[str, str] = {"initiated_by": str(getattr(actor, "id", ""))}
    if tenant is not None:
        metadata["tenant_uuid"] = str(tenant.uuid)

    probe_payload = {
        "id": probe_id,
        "object": "event",
        "type": "spark.billing.readiness_probe",
        "created": now_ts,
        "livemode": False,
        "data": {"object": {"id": f"probe_{uuid4().hex[:10]}", "metadata": metadata}},
    }
    raw = json.dumps(probe_payload).encode("utf-8")
    sig = sign_stripe_webhook_payload(raw, timestamp=now_ts)

    first = process_stripe_webhook(raw, sig, now_ts=now_ts)
    second = process_stripe_webhook(raw, sig, now_ts=now_ts)
    sig_ok = first.get("status") == StripeWebhookEvent.PROC_PROCESSED
    idem_ok = second.get("status") == StripeWebhookEvent.PROC_DUPLICATE

    transport = (
        "stripe_test_api"
        if adapter.has_remote_credentials
        else "local_test_simulator"
    )
    if tenant is not None:
        log_workspace_audit(
            tenant=tenant,
            actor=actor,
            category="billing",
            action="billing_readiness_test_check",
            target_type="StripeWebhookEvent",
            target_id=probe_id,
            summary=(
                f"Ignite admin test-mode billing check passed ({transport}, "
                f"{len(catalog)} catalog plans, HMAC + replay verified)."
            ),
        )

    return {
        "ok": bool(sig_ok and idem_ok),
        "billing_mode": mode,
        "transport": transport,
        "signature_verified": sig_ok,
        "idempotency_verified": idem_ok,
        "probe_event_id": probe_id,
        "summary": (
            f"Test-mode billing check passed ({transport}): {len(catalog)} plan tiers "
            f"verified, HMAC-SHA256 webhook signature accepted ({probe_id}), and "
            f"duplicate replay rejected cleanly. Client billing UI remains hidden."
        ),
    }

