"""Brand chat limits, read from django.conf.settings with safe defaults so
the feature works without any new env vars."""

from __future__ import annotations

from django.conf import settings


def _get(name: str, default):
    value = getattr(settings, name, None)
    return default if value in (None, "") else value


def enabled() -> bool:
    """Kill switch (BRAND_CHAT_ENABLED). Off → every question is refused."""
    return bool(_get("BRAND_CHAT_ENABLED", True))


def questions_per_hour_per_user() -> int:
    return int(_get("BRAND_CHAT_QUESTIONS_PER_HOUR", 20))


def questions_per_day_per_tenant() -> int:
    return int(_get("BRAND_CHAT_QUESTIONS_PER_DAY_PER_TENANT", 150))


def daily_cost_cap_usd() -> float:
    """Estimated spend across every brand per ops day (Pacific)."""
    return float(_get("BRAND_CHAT_DAILY_COST_CAP_USD", 10.0))


def tenant_daily_cost_cap_usd() -> float:
    """Estimated spend for one brand per ops day (Pacific)."""
    return float(_get("BRAND_CHAT_TENANT_DAILY_COST_CAP_USD", 2.0))


def max_tool_calls() -> int:
    return int(_get("BRAND_CHAT_MAX_TOOL_CALLS", 6))


def max_rounds() -> int:
    return int(_get("BRAND_CHAT_MAX_ROUNDS", 4))


def request_timeout_seconds() -> float:
    return float(_get("BRAND_CHAT_REQUEST_TIMEOUT_SECONDS", 30.0))


def turn_deadline_seconds() -> float:
    return float(_get("BRAND_CHAT_TURN_DEADLINE_SECONDS", 60.0))


def history_turns() -> int:
    """Earlier turns of the same conversation sent with a new question."""
    return int(_get("BRAND_CHAT_HISTORY_TURNS", 6))


def input_price_per_million_usd() -> float:
    """Estimate only: set to the configured model's input price."""
    return float(_get("BRAND_CHAT_INPUT_PRICE_PER_M_USD", 1.25))


def output_price_per_million_usd() -> float:
    return float(_get("BRAND_CHAT_OUTPUT_PRICE_PER_M_USD", 10.0))
