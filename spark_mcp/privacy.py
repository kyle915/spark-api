"""Brand ambassador privacy for everything the MCP returns.

Rule (Kyle): brands must not be able to pull BA information out of Spark
through an AI app. No tool returns an ambassador's name, phone, email,
address, payment or document data, or GPS; there is no ambassador listing or
search tool; recaps and events say "Brand ambassador" or give a count.

The tools are written so they never select those columns in the first place.
This module is the second line of defence, run over every string a tool
returns before it leaves the server:

* any e-mail address or phone number is replaced;
* the full name (and stand-in name) of every ambassador who has worked for
  the brand is replaced with "a brand ambassador" — this catches a name
  that slipped into free text such as a store label or a reviewed weekly
  summary someone edited by hand.
"""

from __future__ import annotations

import re

from django.core.cache import cache

BA_LABEL = "a brand ambassador"
REDACTED_EMAIL = "[email removed]"
REDACTED_PHONE = "[phone removed]"

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# North American numbers in the usual written forms: (415) 555-0134,
# 415-555-0134, 415.555.0134, +1 415 555 0134, 4155550134. Store numbers
# ("#725") and dates do not match: 10+ digits in phone grouping are required.
_PHONE_RE = re.compile(
    r"(?<!\w)(?:\+?1[\s.\-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.\-]?)\d{3}[\s.\-]?\d{4}(?!\w)"
)

_CACHE_SECONDS = 300


def _name_pattern(names: list[str]) -> re.Pattern | None:
    names = sorted({n for n in names if len(n) >= 5}, key=len, reverse=True)
    if not names:
        return None
    return re.compile(
        r"(?<!\w)(?:" + "|".join(re.escape(n) for n in names) + r")(?!\w)",
        re.IGNORECASE,
    )


def ambassador_names(tenant_id: int) -> list[str]:
    """Full names of everyone who has worked a shift or filed a recap for the
    brand, plus stand-in names typed on recaps. Cached briefly per tenant."""
    key = f"spark_mcp:ba_names:{tenant_id}"
    cached = cache.get(key)
    if cached is not None:
        return cached

    from ambassadors.models import AmbassadorEvent
    from recaps.models import CustomRecap, Recap

    pairs: set[tuple[str, str]] = set()
    for qs, prefix in (
        (AmbassadorEvent.objects.filter(tenant_id=tenant_id), "ambassador__user__"),
        (CustomRecap.objects.filter(tenant_id=tenant_id), "ambassador__user__"),
        (Recap.objects.filter(event__tenant_id=tenant_id), "ambassador__user__"),
    ):
        pairs.update(
            qs.exclude(**{f"{prefix}first_name": ""})
            .values_list(f"{prefix}first_name", f"{prefix}last_name")
            .distinct()
        )
    names: set[str] = set()
    for first, last in pairs:
        full = " ".join(f"{first or ''} {last or ''}".split())
        if " " in full:
            names.add(full)
    for qs in (
        CustomRecap.objects.filter(tenant_id=tenant_id),
        Recap.objects.filter(event__tenant_id=tenant_id),
    ):
        for raw in qs.exclude(external_ba_name__isnull=True).exclude(
            external_ba_name=""
        ).values_list("external_ba_name", flat=True).distinct():
            cleaned = " ".join(str(raw).split())
            if cleaned:
                names.add(cleaned)
    result = sorted(names)
    cache.set(key, result, _CACHE_SECONDS)
    return result


class Scrubber:
    def __init__(self, tenant_id: int):
        self._names = _name_pattern(ambassador_names(tenant_id))

    def text(self, value: str) -> str:
        value = _EMAIL_RE.sub(REDACTED_EMAIL, value)
        value = _PHONE_RE.sub(REDACTED_PHONE, value)
        if self._names is not None:
            value = self._names.sub(BA_LABEL, value)
        return value

    def deep(self, payload):
        """Scrub every string in a JSON-shaped payload (keys left alone)."""
        if isinstance(payload, str):
            return self.text(payload)
        if isinstance(payload, list):
            return [self.deep(v) for v in payload]
        if isinstance(payload, dict):
            # Spark permalinks are built here from uuids, never from data.
            return {
                k: (v if k == "url" else self.deep(v)) for k, v in payload.items()
            }
        return payload
