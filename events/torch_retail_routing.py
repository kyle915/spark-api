"""Torch recap recipients.

Retail-approved recaps go to the sales org keyed by the store's state.
Ryan Heuser and Collin Kerrigan (CEO) are off every per-retail-recap blast.
Event, Guerilla and Seeding recaps go only to the field marketing list
(Ryan + the four market managers), never the state reps. The Monday weekly
digest goes to the full retail list, including the weekly-only people.
"""

from __future__ import annotations

import re

from events.routing import _state_code_from_request, extract_state_code

# Always on every retail recap (and on the weekly rollup).
TORCH_RETAIL_ALL_STATES: tuple[tuple[str, str], ...] = (
    ("John Giarrante", "john@torchdrinks.com"),
    ("Doug Wiegard", "doug@torchdrinks.com"),
    ("Liberty Flynn", "liberty@torchdrinks.com"),
)

# State → additional people for that market (managers + their reps).
TORCH_RETAIL_BY_STATE: dict[str, tuple[tuple[str, str], ...]] = {
    "OH": (("Jason Merkle", "jason@torchdrinks.com"),),
    "MO": (
        ("Emma Jones", "emma@torchdrinks.com"),
        ("Brian Watkins", "brian@torchdrinks.com"),
    ),
    "IL": (("Emma Jones", "emma@torchdrinks.com"),),
    "KS": (("Brian Watkins", "brian@torchdrinks.com"),),
    "GA": (("Cesar Vazquez", "cesar@torchdrinks.com"),),
    "NC": (
        ("Cesar Vazquez", "cesar@torchdrinks.com"),
        ("Lucas Dyer", "lucas@torchdrinks.com"),
    ),
    "SC": (
        ("Cesar Vazquez", "cesar@torchdrinks.com"),
        ("Skylar King", "skylar@torchdrinks.com"),
    ),
    "TN": (("Bobby Houk", "bobby@torchdrinks.com"),),
    "FL": (
        ("James Gilbert", "james@torchdrinks.com"),
        ("LeslyAnn Altet", "leslyann@torchdrinks.com"),
        ("Emily McGuire", "emily@torchdrinks.com"),
    ),
    "TX": (
        ("Brad Cooper", "brad@torchdrinks.com"),
        ("Morgan Moore", "morgan@torchdrinks.com"),
    ),
}

# Weekly rollup only — never per-recap.
TORCH_WEEKLY_ONLY: tuple[tuple[str, str], ...] = (
    ("Ryan Heuser", "ryanheuser@torchdrinks.com"),
    ("Collin Kerrigan", "collin@torchenterprise.com"),
)

# Every Event / Guerilla / Seeding recap — instead of the state reps.
TORCH_FIELD_MARKETING_RECAP: tuple[tuple[str, str], ...] = (
    ("Ryan Heuser", "ryanheuser@torchdrinks.com"),
    ("Alec Aparicio", "alec@torchdrinks.com"),
    ("Brittany Senglin", "brittany@torchdrinks.com"),
    ("Victoria Quintana", "victoria@torchdrinks.com"),
    ("Octavius Jefferson", "octavius@torchdrinks.com"),
)

EXECUTION_RETAIL = "retail"
EXECUTION_EVENT = "event"
EXECUTION_GUERILLA = "guerilla"
EXECUTION_SEEDING = "seeding"

# First match wins, so "Guerilla Activation" is guerilla, not event.
_EXECUTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (EXECUTION_SEEDING, re.compile(r"product\s*seeding|\bseeding\b", re.I)),
    (EXECUTION_GUERILLA, re.compile(r"guerr?ill?a", re.I)),
    (EXECUTION_RETAIL, re.compile(r"retail|on[-\s]?prem", re.I)),
    (EXECUTION_EVENT, re.compile(r"event|activation|festival|pop[-\s]?up", re.I)),
)

# Ignite ops still CC'd on portal (request-linked) Torch recap mail.
TORCH_RETAIL_IGNITE_OPS: tuple[str, ...] = (
    "events@igniteproductions.co",
    "nevena@igniteproductions.co",
)


def _dedupe_emails(emails: list[str] | tuple[str, ...]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for email in emails:
        normalized = (email or "").strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(normalized)
    return out


def torch_retail_recap_emails(state_code: str | None) -> list[str]:
    """Per-recap Torch retail recipients for a US state. Excludes Ryan."""
    emails = [email for _name, email in TORCH_RETAIL_ALL_STATES]
    code = (state_code or "").strip().upper()
    if code in TORCH_RETAIL_BY_STATE:
        emails.extend(email for _name, email in TORCH_RETAIL_BY_STATE[code])
    return _dedupe_emails(emails)


def torch_field_marketing_recap_emails() -> list[str]:
    """Per-recap recipients for Event / Guerilla / Seeding. No state reps."""
    return _dedupe_emails([email for _name, email in TORCH_FIELD_MARKETING_RECAP])


def _execution_type_for_name(name: str | None) -> str | None:
    text = name or ""
    for kind, pattern in _EXECUTION_PATTERNS:
        if pattern.search(text):
            return kind
    return None


def _related_name(obj, *path: str) -> str | None:
    try:
        for attr in path:
            obj = getattr(obj, attr, None)
            if obj is None:
                return None
    except Exception:
        return None
    return obj if isinstance(obj, str) else None


def torch_recap_execution_type(recap) -> str:
    """Event / Guerilla / Seeding / Retail for a Recap or CustomRecap.

    The template the BA filed on wins, then the event's program, then the
    linked request's type. Anything unrecognised stays retail.
    """
    candidates = (
        ("custom_recap_template", "name"),
        ("custom_recap_template", "event_type", "name"),
        ("event", "event_type", "name"),
        ("event", "custom_recap_template", "name"),
        ("event", "request", "request_type", "name"),
    )
    for path in candidates:
        kind = _execution_type_for_name(_related_name(recap, *path))
        if kind is not None:
            return kind
    return EXECUTION_RETAIL


def torch_weekly_digest_emails() -> list[str]:
    """Full Torch weekly rollup list — all states, every market, and Ryan."""
    emails = [email for _name, email in TORCH_RETAIL_ALL_STATES]
    for people in TORCH_RETAIL_BY_STATE.values():
        emails.extend(email for _name, email in people)
    emails.extend(email for _name, email in TORCH_WEEKLY_ONLY)
    return _dedupe_emails(emails)


def state_code_from_event(event) -> str | None:
    """Best-effort 2-letter state from the recap's event / linked request."""
    if event is None:
        return None
    code = extract_state_code(getattr(event, "address", None))
    if code:
        return code
    try:
        state = getattr(event, "state", None)
        if state is not None and getattr(state, "code", None):
            return str(state.code).strip().upper()
    except Exception:
        pass
    try:
        req = getattr(event, "request", None)
        if req is not None:
            return _state_code_from_request(req)
    except Exception:
        pass
    return None
