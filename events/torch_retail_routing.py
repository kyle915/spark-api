"""Torch retail recap recipients by state.

Retail-approved recaps go to the sales org keyed by the store's state.
Ryan Heuser is weekly-rollup only — never on a per-recap blast.
The Monday weekly digest goes to the full list, including Ryan.
"""

from __future__ import annotations

from events.routing import extract_state_code

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
        from events.routing import _state_code_from_request

        req = getattr(event, "request", None)
        if req is not None:
            return _state_code_from_request(req)
    except Exception:
        pass
    return None
