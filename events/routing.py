"""
Per-tenant state-based RMM routing.

When an external request comes in via /spark-form/:slug we look at the
activation address, pick the state, and route the request to the RMM
responsible for that territory. Falls back to "every RMM in the table"
when the state is outside the configured coverage so nothing slips
through.

Kept here (and not in the DB) deliberately — the routing logic is the
kind of thing the client wants reviewed alongside other ops choices.
Move to a per-tenant `RmmTerritory` table when there's a second client
that needs different rules.
"""
from __future__ import annotations
import re
import logging
from asgiref.sync import sync_to_async
from django.db.models import Q

logger = logging.getLogger(__name__)

# Lower-cased email → list of state abbreviations the RMM owns.
LIQUID_DEATH_TERRITORY: dict[str, list[str]] = {
    "l.giaccio@liquiddeath.com": [
        "NY", "VT", "MA", "ME", "NH", "RI", "CT", "NJ",
    ],
    "k.williams@liquiddeath.com": [
        "CA", "NV", "AZ", "UT", "CO", "ID", "WY", "OR", "MT", "NM", "HI",
    ],
    "m.cristancho@liquiddeath.com": [
        "FL", "GA", "NC", "VA", "DE", "TN", "KY", "SC", "WV",
    ],
    "ross@liquiddeath.com": [
        "TX", "OK", "AR", "LA", "AL", "MS",
    ],
    "pat@liquiddeath.com": [
        "PA", "MD", "DE", "WA", "AK",
    ],
    "t.reed@liquiddeath.com": [
        "WI", "IL", "IN", "OH", "IA", "MN", "KS", "NE", "MO", "MI", "ND", "SD",
    ],
}

# Ignite admins that always get CC'd on the routing email.
IGNITE_REVIEW_CC: list[str] = [
    "events@igniteproductions.co",
    "myriant@igniteproductions.co",
    "kyle@igniteproductions.co",
    "nevena@igniteproductions.co",
    "madison@igniteproductions.co",
]

# Tenants where we apply the territory map. The slug is what the BA
# types in the public form URL: /spark-form/ighn-liquid-death.
ROUTED_TENANT_SLUGS = {"ighn-liquid-death"}

_STATE_AFTER_ZIP_RE = re.compile(
    r"\b([A-Z]{2})\b\s*(?:\d{5}(?:-\d{4})?)?\s*(?:,\s*USA)?\s*$"
)


def extract_state_code(address: str | None) -> str | None:
    """Pull the 2-letter state code out of a Google-formatted address.

        '1885 Halite Dr, Sparks, NV 89436, USA' → 'NV'

    Falls back to None if we can't find one (international address,
    parser miss, etc.) — callers should treat that as "route to
    everyone."""
    if not address:
        return None
    m = _STATE_AFTER_ZIP_RE.search(address.strip())
    return m.group(1).upper() if m else None


def territory_emails_for_state(tenant_slug: str, state_code: str | None) -> list[str]:
    """Return the list of LD-side reviewer emails for a given state.

    If `state_code` is None or not covered by any reviewer, returns
    every reviewer in the table so the request can't fall through the
    cracks."""
    if tenant_slug not in ROUTED_TENANT_SLUGS:
        return []
    state_code = (state_code or "").upper()
    matched = [
        email
        for email, states in LIQUID_DEATH_TERRITORY.items()
        if state_code in states
    ]
    if matched:
        return matched
    # Fallback: every reviewer
    logger.info(
        "No RMM territory match for tenant=%s state=%s — fanning out to all reviewers",
        tenant_slug, state_code,
    )
    return list(LIQUID_DEATH_TERRITORY.keys())


@sync_to_async
def assign_rmm_for_request(request, tenant_slug: str) -> tuple[object | None, list[str]]:
    """Pick the RMM for this request and assign it. Returns (user, all_to_emails).

    1. Extract state from request.address.
    2. Map to one or more reviewer emails per the territory table.
    3. Look up the user row for the first match, set rmm_asigned, save.
    4. Return that user plus the full list of TO addresses so the
       mailer can address everyone in the territory (when fallback
       fanout fires) without re-doing the lookup.
    """
    from tenants.models import User
    if tenant_slug not in ROUTED_TENANT_SLUGS:
        return None, []
    state = extract_state_code(getattr(request, "address", None))
    emails = territory_emails_for_state(tenant_slug, state)
    if not emails:
        return None, []
    # The first reviewer (alphabetical-ish by territory) becomes the
    # canonical assigned RMM on the request row; the rest get the email
    # too. If the territory falls through to fanout, we just take the
    # first as nominal owner.
    primary_email = emails[0]
    user = (
        User.objects.filter(email__iexact=primary_email, is_active=True).first()
        or User.objects.filter(email__iexact=primary_email).first()
    )
    if user:
        request.rmm_asigned_id = user.id
        request.save(update_fields=["rmm_asigned_id", "updated_at"])
    return user, emails
