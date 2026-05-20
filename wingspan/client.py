"""
Async Wingspan API client.

Wraps the Wingspan REST API for two surfaces the admin UI needs:
  - Payroll periods (pay cycles, status, total)
  - Payments / payouts (disbursements to BAs in a period)

Configuration (env vars, read via django.conf.settings):
  - WINGSPAN_API_KEY (required for non-mock mode)
  - WINGSPAN_API_BASE (default: https://api.wingspan.app)
  - WINGSPAN_MOCK    (default: False — when True, returns canned
                     "no data" responses so the front-end can render
                     its empty states without a live key)

The client is intentionally read-only for v1. No payouts run via
Spark — payroll is still kicked off from the Wingspan dashboard.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import httpx
from django.conf import settings


logger = logging.getLogger(__name__)


class WingspanNotConfigured(Exception):
    """Raised when callers ask for live data but no API key is set.

    Resolver layer should catch this and return a structured "not
    connected" payload so the admin UI can render a friendly state
    instead of a GraphQL error.
    """


class WingspanAPIError(Exception):
    """Network / auth / 5xx error from Wingspan."""


@dataclass
class PayrollPeriod:
    """A Wingspan pay cycle. Names mirror common Wingspan terminology
    so the resolver can pass them through unchanged. Optional fields
    are None when Wingspan's response doesn't include them.
    """

    id: str
    label: str  # e.g. "May 13 – May 19"
    starts_at: Optional[str] = None  # ISO date
    ends_at: Optional[str] = None
    pay_date: Optional[str] = None
    status: Optional[str] = None  # open | processing | paid
    total_amount: Optional[float] = None  # USD
    contractor_count: Optional[int] = None


@dataclass
class Payment:
    """A single disbursement to a contractor (BA) within a period."""

    id: str
    contractor_name: Optional[str] = None
    contractor_email: Optional[str] = None
    amount: Optional[float] = None
    status: Optional[str] = None  # pending | sent | failed
    pay_date: Optional[str] = None
    period_id: Optional[str] = None
    memo: Optional[str] = None


@dataclass
class WingspanConfig:
    api_key: str
    base_url: str = "https://api.wingspan.app"
    mock: bool = False

    @classmethod
    def from_settings(cls) -> "WingspanConfig":
        api_key = getattr(settings, "WINGSPAN_API_KEY", "") or ""
        base_url = (
            getattr(settings, "WINGSPAN_API_BASE", "") or "https://api.wingspan.app"
        )
        mock = bool(getattr(settings, "WINGSPAN_MOCK", False))
        return cls(api_key=api_key, base_url=base_url, mock=mock)


def _config() -> WingspanConfig:
    return WingspanConfig.from_settings()


def is_connected() -> bool:
    """Cheap configuration check — does the backend have a key?"""
    cfg = _config()
    if cfg.mock:
        return False
    return bool(cfg.api_key)


async def _request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = _config()
    if not cfg.api_key and not cfg.mock:
        raise WingspanNotConfigured(
            "WINGSPAN_API_KEY is not set. Set it on Cloud Run to enable "
            "the integration, or set WINGSPAN_MOCK=true for an empty-state demo."
        )
    if cfg.mock:
        # Mock mode: deliberately return empty so the UI renders its
        # "no data" state instead of fake numbers.
        return {"results": [], "data": [], "items": []}

    url = f"{cfg.base_url.rstrip('/')}{path}"
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Accept": "application/json",
        "User-Agent": "spark-api/wingspan-client",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.request(
                method, url, headers=headers, params=params
            )
    except httpx.HTTPError as exc:
        logger.warning("Wingspan transport error %s %s: %s", method, path, exc)
        raise WingspanAPIError(f"Wingspan transport error: {exc}") from exc

    if resp.status_code == 401 or resp.status_code == 403:
        raise WingspanAPIError(
            f"Wingspan auth failed ({resp.status_code}) — check WINGSPAN_API_KEY."
        )
    if resp.status_code >= 400:
        snippet = (resp.text or "")[:200]
        raise WingspanAPIError(
            f"Wingspan {method} {path} returned {resp.status_code}: {snippet}"
        )

    try:
        return resp.json()
    except ValueError:
        return {}


def _coerce_amount(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _coerce_int(raw: Any) -> Optional[int]:
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _normalize_period(row: dict[str, Any]) -> PayrollPeriod:
    """Best-effort field aliasing. Different Wingspan response shapes
    use different key names; we look in the obvious places without
    making assumptions hard-fail.
    """
    period_id = (
        row.get("id")
        or row.get("payrollId")
        or row.get("uuid")
        or ""
    )
    label = (
        row.get("label")
        or row.get("name")
        or row.get("period")
        or row.get("payDate")
        or str(period_id)
    )
    return PayrollPeriod(
        id=str(period_id),
        label=str(label),
        starts_at=row.get("startsAt") or row.get("startDate"),
        ends_at=row.get("endsAt") or row.get("endDate"),
        pay_date=row.get("payDate") or row.get("disburseAt"),
        status=row.get("status"),
        total_amount=_coerce_amount(
            row.get("totalAmount") or row.get("total")
        ),
        contractor_count=_coerce_int(
            row.get("contractorCount") or row.get("count")
        ),
    )


def _normalize_payment(row: dict[str, Any]) -> Payment:
    payee = row.get("contractor") or row.get("payee") or row.get("recipient") or {}
    name = (
        row.get("contractorName")
        or payee.get("name")
        or " ".join(
            [
                payee.get("firstName", "") or "",
                payee.get("lastName", "") or "",
            ]
        ).strip()
        or None
    )
    return Payment(
        id=str(
            row.get("id") or row.get("paymentId") or row.get("uuid") or ""
        ),
        contractor_name=name,
        contractor_email=row.get("contractorEmail") or payee.get("email"),
        amount=_coerce_amount(row.get("amount") or row.get("total")),
        status=row.get("status"),
        pay_date=row.get("payDate") or row.get("createdAt"),
        period_id=row.get("periodId") or row.get("payrollId"),
        memo=row.get("memo") or row.get("description"),
    )


def _extract_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Pick whichever rows-shaped field the response uses."""
    for key in ("results", "data", "items", "rows"):
        v = payload.get(key)
        if isinstance(v, list):
            return v
    return []


async def list_payroll_periods(
    *, limit: int = 12
) -> list[PayrollPeriod]:
    """Recent payroll periods, newest first.

    Returns [] (not raises) when the API isn't configured — the
    resolver layer surfaces a "not connected" flag separately so the
    UI can show a setup nudge.
    """
    if not is_connected():
        return []
    try:
        payload = await _request(
            "GET",
            "/v1/payroll",
            params={"limit": limit},
        )
    except WingspanNotConfigured:
        return []
    return [_normalize_period(r) for r in _extract_list(payload)]


async def list_payments(
    *,
    period_id: str | None = None,
    limit: int = 50,
) -> list[Payment]:
    """Recent payments / disbursements.

    Filter to a single period via `period_id`. Same not-configured
    posture as `list_payroll_periods`.
    """
    if not is_connected():
        return []
    params: dict[str, Any] = {"limit": limit}
    if period_id:
        params["periodId"] = period_id
    try:
        payload = await _request("GET", "/v1/payments", params=params)
    except WingspanNotConfigured:
        return []
    return [_normalize_payment(r) for r in _extract_list(payload)]
