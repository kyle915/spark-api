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

# Addresses to strip from EVERY composed CC list, no matter where they
# came from (static IGNITE_REVIEW_CC, settings REQUEST_REVIEW_COPY_EMAILS,
# or the dynamic active-Spark-admin roll-up). nevina@ (note the "i") is a
# stray/typo account that was getting CC'd via the active-Spark-admin
# roll-up; Kyle asked to drop it. nevena@ (the real ops account) stays
# CC'd on every Spark email. Lower-cased for case-insensitive matching.
CC_SUPPRESS_EMAILS: set[str] = {
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

# Full US state (+ DC) names → 2-letter code. Lets the parser resolve forms
# the bare 2-letter regex misses, e.g. "…Columbia, Missouri" or "…Cabot,
# Arkansas, United States".
_US_STATE_NAME_TO_CODE: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN",
    "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}
_US_STATE_CODES: set[str] = set(_US_STATE_NAME_TO_CODE.values())
# Longest names first so "west virginia" wins over "virginia", etc.
_US_STATE_NAMES_BY_LEN = sorted(_US_STATE_NAME_TO_CODE, key=len, reverse=True)

# Trailing country suffix to strip before looking for the state.
_COUNTRY_SUFFIX_RE = re.compile(
    r"[\s,]*(?:united states of america|united states|u\.?\s*s\.?\s*a\.?|"
    r"u\.?\s*s\.?)\s*$",
    re.IGNORECASE,
)
# A 2-letter code at the END (optionally before a zip) — the most authoritative
# signal. Case-insensitive; validated against the real US-state set below.
_END_CODE_RE = re.compile(r"\b([A-Za-z]{2})\b[,\s]*(?:\d{5}(?:-\d{4})?)?\s*$")


def extract_state_code(address: str | None) -> str | None:
    """Pull the 2-letter US state code out of an address string.

    Robust to the messy real-world forms we actually receive:
      * Google-Places ("1885 Halite Dr, Sparks, NV 89436, USA")
      * manual comma form ("EDMOND, OK, 73034")
      * lowercase codes ("11650 s 73rd st papillion, ne 68046")
      * full state names ("405 East Nifong Blvd, Columbia, Missouri")
      * a "United States" country suffix
      * tab / irregular whitespace ("OREGON CITY\\tOR\\t97045")

    Resolution order (most authoritative first):
      1. a trailing 2-letter code (optionally before a zip), validated
         against the real US-state set — beats a state-named city like
         "Indiana, PA";
      2. a full state name anywhere in the string (longest match wins).

    Returns None for international addresses or genuine misses — callers
    treat None as "route manually" (see `_state_code_from_request`).
    """
    if not address:
        return None
    # Normalise: collapse tabs/spaces, then drop the trailing country.
    norm = re.sub(r"[\t ]+", " ", address.strip())
    norm = _COUNTRY_SUFFIX_RE.sub("", norm).strip()

    m = _END_CODE_RE.search(norm)
    if m and m.group(1).upper() in _US_STATE_CODES:
        return m.group(1).upper()

    low = norm.lower()
    for name in _US_STATE_NAMES_BY_LEN:
        if re.search(r"\b" + re.escape(name) + r"\b", low):
            return _US_STATE_NAME_TO_CODE[name]

    return None


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


def _state_for_code(code: str | None):
    """Look up the State row for a 2-letter code (case-insensitive).

    Returns None when the code is empty or unknown to the State table."""
    if not code:
        return None
    from events import models as event_models

    return (
        event_models.State.objects.filter(code__iexact=code).order_by("id").first()
    )


def compute_request_routing(request):
    """READ-ONLY: determine the state + territory RMM this request *should*
    have, without persisting anything.

    This is the shared brain behind both the create-time hook and the
    backfill's dry-run. It mirrors `assign_rmm_for_request`'s logic — the
    tenant-level ``default_external_rmm`` override wins, otherwise the
    per-state territory map (LD only today) — but writes nothing and sends
    no email.

    Returns ``(assigned_user_or_None, state_code_or_None, state_obj_or_None)``.
    """
    from tenants.models import Tenant, User

    state_code = _state_code_from_request(request)
    state_obj = _state_for_code(state_code) if state_code else None

    assigned = None
    if request.tenant_id:
        tenant = (
            Tenant.objects.filter(id=request.tenant_id)
            .select_related("default_external_rmm")
            .first()
        )
        if tenant:
            if tenant.default_external_rmm_id and tenant.default_external_rmm:
                # Tenant-wide override — every unrouted request goes here.
                assigned = tenant.default_external_rmm
            else:
                slug = tenant.request_url_name or tenant.slug
                emails = territory_emails_for_state(slug, state_code)
                if emails:
                    assigned = (
                        User.objects.filter(
                            email__iexact=emails[0], is_active=True
                        ).first()
                        or User.objects.filter(email__iexact=emails[0]).first()
                    )
    return assigned, state_code, state_obj


def route_request_sync(request) -> tuple[object | None, str | None]:
    """Synchronous, signal-free RMM routing + state stamping for ONE request.

    Parity with the public-form `assign_rmm_for_request`, but for
    INTERNALLY-created requests and the backfill: it stamps ``request.state``
    from the address (so the Tracker "Market" column and the linked-sheet
    "State" column populate) and assigns the territory RMM (so the row lands
    in that RMM's filtered sheet view) — WITHOUT sending any territory email.

    Only fills BLANKS — an already-set ``state``/``rmm_asigned`` is left
    untouched, so it's idempotent and safe to re-run. Persists via a queryset
    ``.update()`` so it does NOT fire the Request post_save signal; the caller
    must re-sync the sheet exactly once (``upsert_request_row``) afterward.

    Returns ``(assigned_user_or_None, resolved_state_code_or_None, changed)``
    where ``changed`` is True iff a field was actually written (lets callers
    skip a redundant sheet re-sync when nothing changed).
    """
    from django.utils import timezone
    from events import models as event_models

    assigned, state_code, state_obj = compute_request_routing(request)

    updates: dict = {}
    if not request.state_id and state_obj is not None:
        # Set the relation (not just the id) so an in-memory caller — e.g. the
        # post_save sheet mirror — reads request.state.code without a re-fetch.
        request.state = state_obj
        updates["state_id"] = state_obj.id
    if not request.rmm_asigned_id and assigned is not None:
        request.rmm_asigned = assigned
        updates["rmm_asigned_id"] = assigned.id

    if updates:
        updates["updated_at"] = timezone.now()
        event_models.Request.objects.filter(pk=request.pk).update(**updates)

    return assigned, state_code, bool(updates)
