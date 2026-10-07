"""READ-ONLY: which recent Torch / Liquid Death routings change under the
hardened address→state parser.

The territory parser used to take a full state name *anywhere* in the address
("13657 Washington St" → WA, "100 Georgia Ave, Columbus, OH" was fine only
because the trailing code won) and read "Peachtree Rd NE" as Nebraska. This
recomputes the old vs current routed state for:

  * Torch approved recaps (per-recap mail goes to reps keyed by state);
  * Liquid Death requests (territory RMM assignment);
  * Liquid Death approved recaps (event state).

Writes nothing and sends nothing.

    python manage.py audit_routing_state [--days 60] [--limit 25]
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from events.event_confirmations import is_liquid_death_tenant
from events.models import Request
from events.routing import (
    _state_code_from_request,
    territory_emails_for_state,
)
from events.torch_portal import is_torch_tenant
from events.torch_retail_routing import (
    state_code_from_event,
    torch_retail_recap_emails,
)
from events.us_states import US_STATE_CODES, US_STATE_NAME_TO_CODE
from recaps.models import CustomRecap, Recap
from tenants.models import Tenant

_LEGACY_NAMES = sorted(US_STATE_NAME_TO_CODE, key=len, reverse=True)
_LEGACY_COUNTRY_RE = re.compile(
    r"[\s,]*(?:united states of america|united states|u\.?\s*s\.?\s*a\.?|"
    r"u\.?\s*s\.?)\s*$",
    re.IGNORECASE,
)
_LEGACY_END_CODE_RE = re.compile(r"\b([A-Za-z]{2})\b[,\s]*(?:\d{2,5}(?:-\d{4})?)?\s*$")


def legacy_extract_state_code(address: str | None) -> str | None:
    """The pre-fix parser, verbatim, for the comparison only."""
    if not address:
        return None
    norm = re.sub(r"[\t ]+", " ", address.strip())
    norm = _LEGACY_COUNTRY_RE.sub("", norm).strip()
    m = _LEGACY_END_CODE_RE.search(norm)
    if m and m.group(1).upper() in US_STATE_CODES:
        return m.group(1).upper()
    low = norm.lower()
    for name in _LEGACY_NAMES:
        if re.search(r"\b" + re.escape(name) + r"\b", low):
            return US_STATE_NAME_TO_CODE[name]
    return None


def _code(getter) -> str | None:
    try:
        value = getter()
    except Exception:
        return None
    return str(value).strip().upper() if value else None


def _legacy_request_state(req) -> str | None:
    return (
        legacy_extract_state_code(getattr(req, "address", None))
        or _code(lambda: req.state.code)
        or _code(lambda: req.location.state.code)
        or _code(lambda: req.retailer.location.state.code)
    )


def _legacy_event_state(event) -> str | None:
    code = legacy_extract_state_code(getattr(event, "address", None)) or _code(
        lambda: event.state.code
    )
    if code:
        return code
    req = getattr(event, "request", None)
    return _legacy_request_state(req) if req is not None else None


class Command(BaseCommand):
    help = "Read-only: old vs new routed state for recent Torch / LD routings."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=60)
        parser.add_argument("--limit", type=int, default=25, help="Examples listed per section")

    def handle(self, *args, **opts):
        since = timezone.now() - timedelta(days=opts["days"])
        limit: int = opts["limit"]
        w = self.stdout.write
        tenants = list(Tenant.objects.all())
        torch_ids = [t.id for t in tenants if is_torch_tenant(t)]
        ld_ids = [t.id for t in tenants if is_liquid_death_tenant(t)]
        w(f"audit_routing_state days={opts['days']} torch={torch_ids} ld={ld_ids}")

        self._torch(torch_ids, since, limit)
        self._ld_requests(ld_ids, since, limit)
        self._ld_recaps(ld_ids, since, limit)

    def _approved_recaps(self, tenant_ids, since):
        rel = (
            "event",
            "event__state",
            "event__rmm_asigned",
            "event__request",
            "event__request__state",
            "event__request__location__state",
            "event__request__retailer__location__state",
        )
        out = []
        for model in (Recap, CustomRecap):
            out.extend(
                model.objects.filter(event__tenant_id__in=tenant_ids, approved=True)
                .filter(Q(approved_at__gte=since) | Q(client_notified_at__gte=since))
                .select_related(*rel)
                .order_by("id")
            )
        return out

    def _torch(self, tenant_ids, since, limit):
        w = self.stdout.write
        recaps = self._approved_recaps(tenant_ids, since)
        state_diff = []
        rcpt_diff = []
        for recap in recaps:
            old = _legacy_event_state(recap.event)
            new = state_code_from_event(recap.event)
            if old == new:
                continue
            state_diff.append((old, new))
            old_to = set(torch_retail_recap_emails(old))
            new_to = set(torch_retail_recap_emails(new))
            if old_to != new_to:
                rcpt_diff.append((recap, old, new, sorted(old_to - new_to), sorted(new_to - old_to)))
        notified = sum(1 for r, *_ in rcpt_diff if r.client_notified_at)
        w(f"\nTORCH approved recaps: {len(recaps)}")
        w(f"  routed state changes: {len(state_diff)} {Counter(state_diff).most_common(15)}")
        w(f"  recipient set changes: {len(rcpt_diff)} (mail already sent on {notified})")
        for recap, old, new, extra, missed in rcpt_diff[:limit]:
            w(
                f"   - {type(recap).__name__}#{recap.id} {recap.event.name!r} "
                f"address={recap.event.address!r} old={old} new={new} "
                f"sent={'yes' if recap.client_notified_at else 'no'} "
                f"wrongly_sent_to={extra} missed={missed}"
            )

    def _ld_requests(self, tenant_ids, since, limit):
        w = self.stdout.write
        reqs = list(
            Request.objects.filter(tenant_id__in=tenant_ids, created_at__gte=since)
            .select_related(
                "state",
                "location__state",
                "retailer__location__state",
                "request_type",
                "rmm_asigned",
            )
            .order_by("id")
        )
        state_diff = []
        owner_diff = []
        for req in reqs:
            old = _legacy_request_state(req)
            new = _state_code_from_request(req)
            if old == new:
                continue
            state_diff.append((req, old, new))
            old_to = territory_emails_for_state("ighn-liquid-death", old)
            new_to = territory_emails_for_state("ighn-liquid-death", new)
            if old_to != new_to:
                owner_diff.append((req, old, new, old_to, new_to))
        w(f"\nLD requests: {len(reqs)}")
        w(
            f"  routed state changes: {len(state_diff)} "
            f"{Counter((o, n) for _, o, n in state_diff).most_common(15)}"
        )
        w(f"  territory RMM changes (current map): {len(owner_diff)}")
        for req, old, new, old_to, new_to in owner_diff[:limit]:
            w(
                f"   - Request#{req.id} {req.name!r} "
                f"type={getattr(req.request_type, 'name', '')!r} address={req.address!r} "
                f"old={old} new={new} old_to={old_to} new_to={new_to} "
                f"assigned={getattr(req.rmm_asigned, 'email', None)} "
                f"stored_state={getattr(req.state, 'code', None)}"
            )
        for req, old, new in state_diff[:limit]:
            w(f"   (state) Request#{req.id} {req.name!r} address={req.address!r} old={old} new={new}")

    def _ld_recaps(self, tenant_ids, since, limit):
        w = self.stdout.write
        recaps = self._approved_recaps(tenant_ids, since)
        diff = []
        for recap in recaps:
            old = _legacy_event_state(recap.event)
            new = state_code_from_event(recap.event)
            if old != new:
                diff.append((recap, old, new))
        w(f"\nLD approved recaps: {len(recaps)}; event state changes: {len(diff)}")
        for recap, old, new in diff[:limit]:
            w(
                f"   - {type(recap).__name__}#{recap.id} {recap.event.name!r} "
                f"address={recap.event.address!r} old={old} new={new} "
                f"rmm={getattr(recap.event.rmm_asigned, 'email', None)}"
            )
