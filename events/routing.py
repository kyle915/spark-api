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
    "madison@igniteproductions.co",
]

# Addresses to strip from EVERY composed CC list, no matter where they
# came from (static IGNITE_REVIEW_CC, settings REQUEST_REVIEW_COPY_EMAILS,
# or the dynamic active-Spark-admin roll-up). Kyle asked to stop CC'ing
# Nevena on request/approval confirmations; she's also an active Spark
# admin, so dropping her from the static list above isn't enough — the
# approval path folds in admin_emails. Both spellings on file are listed
# defensively. Lower-cased for case-insensitive matching.
CC_SUPPRESS_EMAILS: set[str] = {
    "nevena@igniteproductions.co",
    "nevina@igniteproductions.co",
}


def suppress_cc(emails: list[str]) -> list[str]:
    """Drop any suppressed address from a composed CC list (case-insensitive),
    preserving order and the original casing of the kept entries."""
    return [
        e
        for e in emails
        if (e or "").strip().lower() not in CC_SUPPRESS_EMAILS
    ]

# Tenants where we apply the territory map. The slug is what the BA
# types in the public form URL: /spark-form/ighn-liquid-death.
ROUTED_TENANT_SLUGS = {"ighn-liquid-death"}

# Match a US state code at the end of an address, optionally followed by a
# zip and an optional ", USA" suffix.
#
# The class between state and zip is intentionally permissive — `[,\s]*` —
# because real addresses come from at least three sources, each with its
# own punctuation style:
#
#   "Chino, CA 91710"               (Google Places, state SPACE zip)
#   "EDMOND, OK, 73034"             (manual entry, state COMMA SPACE zip)
#   "Sparks, NV 89436, USA"         (Google Places with country)
#   "Brooklyn, NY"                  (no zip at all — defensible default)
#
# REQ-926 regressed because the previous regex required `\s*` (whitespace
# only) between state and zip, so the comma form silently failed to parse
# and the request fell through to Ignite-only routing.
_STATE_AFTER_ZIP_RE = re.compile(
    r"\b([A-Z]{2})\b[,\s]*(?:\d{5}(?:-\d{4})?)?\s*(?:,\s*USA)?\s*$"
)


def extract_state_code(address: str | None) -> str | None:
    """Pull the 2-letter state code out of a US address string.

    Accepts the common Google-Places format ("1885 Halite Dr, Sparks,
    NV 89436, USA"), the manual-entry comma form ("EDMOND, OK,
    73034"), and addresses without a zip ("Brooklyn, NY"). Returns
    None for international addresses or parser misses — callers
    should treat None as "route manually" (see
    `_state_code_from_request` for the full fallback chain).
    """
    if not address:
        return None
    m = _STATE_AFTER_ZIP_RE.search(address.strip())
    return m.group(1).upper() if m else None


def _state_code_from_request(request) -> str | None:
    """Try every signal we have to determine a 2-letter state code.

    REQ-925 surfaced the failure mode that motivated this: the public
    form was submitted with an incomplete address ("1608 Broadway St",
    no city/state/zip), so `extract_state_code` returned None and the
    territory fanout sent the email to every LD reviewer with Lauren
    (first key in the dict) as the nominal owner. The fix is to try
    progressively-broader sources of state info before giving up:

        1. address regex   (fastest, exact)
        2. request.state   (set explicitly by some import paths)
        3. request.location.state                 (manual venue picker)
        4. request.retailer.location.state        (chain store HQ)

    Each step is wrapped in a broad try/except — we never want a stale
    relation or a missing FK to crash request creation. Returns None
    when no signal yields a state, which `territory_emails_for_state`
    then handles by routing to Ignite-only for manual triage.
    """
    code = extract_state_code(getattr(request, "address", None))
    if code:
        return code
    try:
        if request.state and request.state.code:
            return request.state.code.upper()
    except Exception:
        pass
    try:
        if request.location and request.location.state and request.location.state.code:
            return request.location.state.code.upper()
    except Exception:
        pass
    try:
        if (
            request.retailer
            and request.retailer.location
            and request.retailer.location.state
            and request.retailer.location.state.code
        ):
            return request.retailer.location.state.code.upper()
    except Exception:
        pass
    return None


def territory_emails_for_state(tenant_slug: str, state_code: str | None) -> list[str]:
    """Return the list of LD-side reviewer emails for a given state.

    If `state_code` is None or not covered by any reviewer, returns an
    empty list — `assign_rmm_for_request` then sets rmm_asigned to None
    and the mailer routes to Ignite-only for manual re-routing. We used
    to fan out to every reviewer here, which produced false positives
    like "Lauren got REQ-925 even though the address didn't parse a
    state" (she's first in dict-insertion order so she became the
    nominal owner). Routing to Ignite-only instead surfaces the
    problem to the team that can actually fix it, rather than spamming
    every LD RMM with a request they don't own."""
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
    logger.info(
        "No RMM territory match for tenant=%s state=%s — routing to Ignite only",
        tenant_slug,
        state_code or "<unknown>",
    )
    return []


@sync_to_async
def assign_rmm_for_request(request, tenant_slug: str) -> tuple[object | None, list[str]]:
    """Pick the RMM for this request and assign it. Returns (user, all_to_emails).

    1. Try every state signal on the request (address, request.state,
       location.state, retailer.location.state).
    2. Map to one or more reviewer emails per the territory table.
    3. Look up the user row for the first match, set rmm_asigned, save.
    4. Return that user plus the full list of TO addresses so the
       mailer can address everyone in the territory.

    When no state can be determined, returns (None, []) — the caller is
    responsible for falling back to Ignite-only with a note in the
    email subject so the team knows to re-route manually.
    """
    from tenants.models import User, Tenant

    # Tenant-level override: when an admin has set a "default recipient for
    # external requests" on the Team page, route EVERY public-form request
    # to that user regardless of territory. `tenant_slug` is the
    # request_url_name from the public form URL (fall back to slug).
    tenant = (
        Tenant.objects.filter(
            Q(request_url_name=tenant_slug) | Q(slug=tenant_slug)
        )
        .select_related("default_external_rmm")
        .first()
    )
    if tenant and tenant.default_external_rmm_id and tenant.default_external_rmm:
        rmm = tenant.default_external_rmm
        request.rmm_asigned_id = rmm.id
        request.save(update_fields=["rmm_asigned_id", "updated_at"])
        return rmm, ([rmm.email] if rmm.email else [])

    if tenant_slug not in ROUTED_TENANT_SLUGS:
        return None, []
    state = _state_code_from_request(request)
    emails = territory_emails_for_state(tenant_slug, state)
    if not emails:
        return None, []
    primary_email = emails[0]
    user = (
        User.objects.filter(email__iexact=primary_email, is_active=True).first()
        or User.objects.filter(email__iexact=primary_email).first()
    )
    if user:
        request.rmm_asigned_id = user.id
        request.save(update_fields=["rmm_asigned_id", "updated_at"])
    return user, emails
