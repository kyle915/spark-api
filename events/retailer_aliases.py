"""Store names written several ways, shown as one (a non-destructive layer).

The same store or chain is typed "HEB", "H-E-B", "Heb 725", "Total Wine",
"Total Wine & More". Charts that group by store name counted each spelling as
its own store. This module proposes which written names are the same, staff
confirm, and reports then show the confirmed name.

What changes, and what does not
-------------------------------
* The only thing written is :class:`events.models.RetailerAlias`. No Retailer
  row is merged, renamed or deleted, and no request or event has its stored
  store name rewritten. Dismissing an accepted alias puts every chart back.
* Reports read ACCEPTED aliases only (:func:`accepted_alias_map` +
  :func:`resolve_store_name`), so nothing moves until staff accept.

Chain alias versus store identity (the decision)
------------------------------------------------
These are different things and are kept apart (``RetailerAlias.kind``):

* a **chain** alias is one banner spelled two ways: "HEB" = "H-E-B";
* a **store** alias is one numbered store written two ways:
  "Heb 725" = "HEB #725".

A numbered store is NEVER folded into its banner, and two different store
numbers are never the same store. So in the "by store" charts
(``recaps.consumer_breakdown``), which group by store NAME:

* a name that carries a store number stays its own row, i.e. a physical store;
* a name that is only a banner ("HEB") is one row for that banner, exactly as
  before this module existed. The recap has nothing more specific to say.

The Account Map does not group by name at all: its stores are places
(``events.account_stores``: one address or one spot on the ground). Aliases
only tidy the label on a pin there; pin and visit counts are untouched.

Guards against a mega-merge
---------------------------
A past dedupe in this business folded hundreds of unrelated records into one.
Here:

* a group holds at most :data:`MAX_GROUP_NAMES` written names. Names that are
  identical once case and punctuation are ignored are split into several
  groups of that size; a larger group suggested by the model is refused
  outright, and :func:`decide_group` refuses to accept one however it got
  there;
* names whose known addresses are in different cities are not grouped unless
  it is clearly a chain alias (identical once case and punctuation are
  ignored, or differing only by generic words such as "& More");
* the model sees names and counts only (no addresses, no people), may only
  group names it was sent, cannot join two existing groups, and cannot mix
  store numbers;
* every group reports how many records an accept would re-label, before the
  click.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher

import strawberry
from asgiref.sync import sync_to_async
from django.db import transaction
from django.utils import timezone
from graphql import GraphQLError

from events.staffing_board import _accessible_tenants
from utils.graphql.mixins import resolve_id_to_int
from utils.graphql.permissions import IsClientOrSparkAdmin

logger = logging.getLogger(__name__)

# Written names in one group, the name they resolve to included.
MAX_GROUP_NAMES = 8
# Names sent to the model in one call.
MAX_AI_NEW_NAMES = 120
MAX_AI_CONTEXT_NAMES = 80
# A resolved name is followed through at most this many aliases.
_MAX_HOPS = 4
_MIN_NAME = 2
_MAX_NAME = 80

# Not store names: what people type when they do not know the store.
_NON_ANSWERS = {
    "null", "none", "unknown", "n a", "na", "not specified", "unspecified",
    "not provided", "not available", "no retailer", "undefined", "tbd", "tba",
    "test", "other", "various", "multiple", "store", "retailer",
}

# Words that do not tell two chains apart: "Total Wine" / "Total Wine & More".
_GENERIC_WORDS = {
    "and", "more", "the", "inc", "llc", "co", "company", "corp", "store",
    "stores", "market", "markets", "marketplace", "supermarket", "supermarkets",
    "grocery", "grocers", "food", "foods", "liquor", "liquors", "wine", "wines",
    "spirits", "beverage", "beverages", "pharmacy", "supercenter", "superstore",
    "wholesale", "fine", "shop", "shops",
}

_MARKED_NUMBER_RE = re.compile(
    r"^(?P<base>.*?[A-Za-z].*?)[\s,\-]*(?:#|\bno\.?|\bstore\b|\bstr\.?|\bunit\b)\s*#?\s*0*(?P<num>\d{1,6})\s*$",
    re.I,
)
_BARE_NUMBER_RE = re.compile(r"^(?P<base>.*?[A-Za-z].*?)[\s,\-]+0*(?P<num>\d{2,6})\s*$")


# ─── Names ──────────────────────────────────────────────────────────


def alias_key(name: str | None) -> str:
    """Lower-cased and single-spaced: the key the store charts group by."""
    return " ".join((name or "").split()).casefold()


def _words(name: str | None) -> str:
    """Case, punctuation and "&" / "and" removed: "H-E-B" -> "h e b"."""
    text = (name or "").casefold().replace("&", " and ")
    text = re.sub(r"[’'`.]", "", text)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def chain_key(name: str | None) -> str:
    """:func:`_words` with the spaces out: "H-E-B", "HEB" and "h e b" agree."""
    return _words(name).replace(" ", "")


def split_store_number(name: str) -> tuple[str, str, bool]:
    """``(base, number, marked)``. "HEB #725" -> ("HEB", "725", True);
    "Heb 725" -> ("Heb", "725", False); "Total Wine" -> ("Total Wine", "",
    False). A bare trailing number is only a *candidate* store number
    ("Forever 21" has one too); the caller confirms it."""
    cleaned = " ".join((name or "").split())
    m = _MARKED_NUMBER_RE.match(cleaned)
    if m and m.group("base").strip(" ,-#"):
        return m.group("base").strip(" ,-#"), m.group("num"), True
    m = _BARE_NUMBER_RE.match(cleaned)
    if m and m.group("base").strip(" ,-#"):
        return m.group("base").strip(" ,-#"), m.group("num"), False
    return cleaned, "", False


def is_junk_name(name: str | None) -> bool:
    cleaned = " ".join((name or "").split())
    if not (_MIN_NAME <= len(cleaned) <= _MAX_NAME):
        return True
    if not re.search(r"[A-Za-z]", cleaned):
        return True
    return _words(cleaned) in _NON_ANSWERS


def _city_of(address: str | None) -> str:
    """ "austin|TX" for an address that names its city and state, else ""."""
    from events.market import parse_address_geo

    if not address:
        return ""
    try:
        geo = parse_address_geo(address)
    except Exception:  # noqa: BLE001 — an odd address is just an unknown city
        return ""
    city = (getattr(geo, "city", None) or "").strip().casefold()
    state = (getattr(geo, "state_code", None) or "").strip().upper()
    return f"{city}|{state}" if city and state else ""


@dataclass
class NameInfo:
    """One written store name of a tenant."""

    key: str
    spellings: Counter = field(default_factory=Counter)
    records: int = 0
    cities: set[str] = field(default_factory=set)

    @property
    def display(self) -> str:
        """The spelling used most (the earliest alphabetically on a tie)."""
        if not self.spellings:
            return self.key
        best = max(self.spellings.values())
        return sorted(s for s, n in self.spellings.items() if n == best)[0]


def collect_store_names(tenant_id: int) -> dict[str, NameInfo]:
    """Every store name a tenant's records carry, with how many records.

    The name of a record is the one the charts read: the event's retailer,
    then its request's retailer, then the request's typed ``retailer_name``.
    A request with no event yet counts once, by its own retailer or typed
    name. Soft-deleted requests (and their events) are left out. Retailers
    created from accepted store suggestions are on events, so they are here.
    """
    from events.models import Event, Request

    names: dict[str, NameInfo] = {}

    def add(raw: str | None, address: str | None) -> None:
        cleaned = " ".join((raw or "").split())
        if not cleaned:
            return
        info = names.setdefault(alias_key(cleaned), NameInfo(key=alias_key(cleaned)))
        info.spellings[cleaned] += 1
        info.records += 1
        city = _city_of(address)
        if city:
            info.cities.add(city)

    seen_requests: set[int] = set()
    events = (
        Event.objects.filter(tenant_id=tenant_id)
        .exclude(request__deleted_at__isnull=False)
        .values_list(
            "retailer__name",
            "request__retailer__name",
            "request__retailer_name",
            "address",
            "request_id",
        )
    )
    for retailer, req_retailer, req_name, address, request_id in events.iterator():
        if request_id:
            seen_requests.add(request_id)
        add(retailer or req_retailer or req_name, address)

    requests = Request.objects.filter(
        tenant_id=tenant_id, deleted_at__isnull=True
    ).values_list("pk", "retailer__name", "retailer_name", "address")
    for pk, retailer, typed, address in requests.iterator():
        if pk in seen_requests:
            continue
        add(retailer or typed, address)
    return names


# ─── Reading accepted aliases (what reports call) ───────────────────


def accepted_alias_map(tenant_id: int | None) -> dict[str, str]:
    """``{alias_key: canonical_name}`` of one tenant's ACCEPTED aliases."""
    from events.models import RetailerAlias

    if not tenant_id:
        return {}
    return dict(
        RetailerAlias.objects.filter(
            tenant_id=tenant_id, status=RetailerAlias.STATUS_ACCEPTED
        )
        .exclude(canonical_name="")
        .values_list("alias_key", "canonical_name")
    )


def accepted_alias_maps() -> dict[int, dict[str, str]]:
    """Every tenant's accepted aliases, for a query that spans tenants."""
    from events.models import RetailerAlias

    out: dict[int, dict[str, str]] = {}
    rows = (
        RetailerAlias.objects.filter(status=RetailerAlias.STATUS_ACCEPTED)
        .exclude(canonical_name="")
        .values_list("tenant_id", "alias_key", "canonical_name")
    )
    for tenant_id, key, canonical in rows:
        out.setdefault(tenant_id, {})[key] = canonical
    return out


def resolve_store_name(name: str | None, aliases: dict[str, str] | None) -> str:
    """The name to show for a written store name. A name with no accepted
    alias (and every name when ``aliases`` is empty) comes back exactly as it
    was given, so a caller's output is unchanged until staff accept one."""
    original = name or ""
    current = original.strip()
    if not current or not aliases:
        return original
    seen: set[str] = set()
    changed = False
    for _ in range(_MAX_HOPS):
        key = alias_key(current)
        target = aliases.get(key)
        if not target or key in seen or alias_key(target) == key:
            break
        seen.add(key)
        current = target.strip()
        changed = True
    return current if changed else original


# ─── Proposing ──────────────────────────────────────────────────────


@dataclass
class _Parsed:
    info: NameInfo
    base: str
    number: str
    ident: tuple[str, str]  # (chain key, confirmed store number or "")


@dataclass
class Group:
    """Written names that look like one store, before anything is stored."""

    kind: str
    canonical: str
    members: list[NameInfo]
    source: str
    reason: str = ""
    store_number: str = ""
    ident: tuple[str, str] = ("", "")


def _parse_names(names: dict[str, NameInfo]) -> list[_Parsed]:
    """Decide, for each name, whether its trailing number is a store number.

    A marked number ("#725", "Store 412") always is. A bare one ("Heb 725")
    is when the same base appears on its own or with another number, so
    "Forever 21" alone keeps its number as part of the name.
    """
    raw = []
    for info in names.values():
        if is_junk_name(info.display):
            continue
        base, number, marked = split_store_number(info.display)
        raw.append((info, base, number, marked))
    whole_keys = {chain_key(info.display) for info, _b, number, _m in raw if not number}
    numbers_by_base: dict[str, set[str]] = {}
    for _info, base, number, _marked in raw:
        if number:
            numbers_by_base.setdefault(chain_key(base), set()).add(number)
    parsed: list[_Parsed] = []
    for info, base, number, marked in raw:
        base_key = chain_key(base)
        confirmed = bool(number) and (
            marked or base_key in whole_keys or len(numbers_by_base.get(base_key, ())) > 1
        )
        if confirmed and base_key:
            parsed.append(_Parsed(info, base, number, (base_key, number)))
        else:
            parsed.append(_Parsed(info, info.display, "", (chain_key(info.display), "")))
    return parsed


def _cities_disagree(members: list[NameInfo]) -> bool:
    """True when two members both have known cities and share none."""
    known = [m.cities for m in members if m.cities]
    for i, a in enumerate(known):
        for b in known[i + 1 :]:
            if not (a & b):
                return True
    return False


def _clearly_same_chain(a: str, b: str) -> bool:
    """Two names that are one banner: identical once case and punctuation are
    ignored, differing only by generic words ("& More", "Liquors"), or one
    typo apart."""
    ka, kb = chain_key(a), chain_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    wa, wb = set(_words(a).split()), set(_words(b).split())
    if (wa ^ wb) and (wa ^ wb) <= _GENERIC_WORDS and (wa & wb) - _GENERIC_WORDS:
        return True
    return min(len(ka), len(kb)) >= 5 and SequenceMatcher(None, ka, kb).ratio() >= 0.88


def _most_used(members: list[NameInfo], retailer_names: set[str]) -> NameInfo:
    """The spelling to keep: the one used most; on a tie a Retailer row's own
    name, then the one with the least punctuation."""
    return sorted(
        members,
        key=lambda m: (
            -m.records,
            m.display not in retailer_names,
            len(re.sub(r"[A-Za-z0-9 ]", "", m.display)),
            m.display.casefold(),
        ),
    )[0]


def deterministic_groups(
    names: dict[str, NameInfo],
    accepted: dict[str, str] | None = None,
    retailer_names: set[str] | None = None,
) -> tuple[list[Group], list[_Parsed]]:
    """Names that agree once case, punctuation and "&"/"and" are ignored.

    Returns the groups and every parsed name (the caller sends the leftovers
    to the model). Store-numbered names group only with the same number of
    the same chain, and only when their known cities do not disagree.
    """
    accepted = accepted or {}
    retailer_names = retailer_names or set()
    parsed = _parse_names(names)
    by_ident: dict[tuple[str, str], list[_Parsed]] = {}
    for p in parsed:
        if p.ident[0]:
            by_ident.setdefault(p.ident, []).append(p)

    # The spelling each chain resolves to, so "HEB #725" follows its banner.
    chain_display: dict[str, str] = {}
    for (base_key, number), members in by_ident.items():
        if number:
            continue
        infos = [m.info for m in members]
        chain_display[base_key] = resolve_store_name(
            _most_used(infos, retailer_names).display, accepted
        )

    groups: list[Group] = []
    for (base_key, number), members in sorted(by_ident.items()):
        infos = [m.info for m in members]
        if len(infos) < 2:
            continue
        if number:
            if _cities_disagree(infos):
                # Same chain and number on paper, different towns on the
                # ground: not the same store.
                continue
            bases = Counter()
            for m in members:
                bases[m.base] += m.info.records
            chain = chain_display.get(base_key) or resolve_store_name(
                sorted(bases, key=lambda b: (-bases[b], b.casefold()))[0], accepted
            )
            canonical = f"{chain} #{number}"
            kind = "store"
            reason = f"Same chain and store number ({number})."
        else:
            canonical = chain_display[base_key]
            kind = "chain"
            reason = "The same name once capitals and punctuation are ignored."
        groups.append(
            Group(
                kind=kind,
                canonical=canonical,
                members=infos,
                source="rule",
                reason=reason,
                store_number=number,
                ident=(base_key, number),
            )
        )
    return groups, parsed


_AI_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["groups"],
    "properties": {
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["canonical", "kind", "names", "reason"],
                "properties": {
                    "canonical": {"type": "string"},
                    "kind": {"type": "string", "enum": ["chain", "store"]},
                    "names": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                },
            },
        }
    },
}

_AI_SYSTEM = (
    "You tidy a list of retail store names typed by different people for one brand's "
    "field-marketing records. Some names are the same store or chain written "
    "differently. Return groups of names from the list that are the same, with the "
    "spelling to keep. Rules: use only names exactly as they appear in the list; a name "
    "belongs to at most one group; a group has at least two names and at most "
    f"{MAX_GROUP_NAMES}; kind is \"chain\" when the names are one banner spelled "
    "differently and \"store\" when they are one single location; never put two "
    "different chains together because they sell similar things or sound alike; never "
    "group names that carry different store numbers, and never group a numbered store "
    "with its chain; names that differ by a town or neighbourhood are different stores. "
    "When you are not sure, leave the name out. Most names belong in no group. Keep each "
    "reason to one short sentence."
)


def ai_groups(
    new_names: list[NameInfo], context_names: list[str]
) -> list[dict] | None:
    """Ask the model which of ``new_names`` match each other or a context
    name. Sends names and record counts only. ``None`` when AI is off or the
    call fails (the caller then leaves the names for a later run)."""
    from utils.ai_text import AiUnavailable, generate_json

    if not new_names:
        return []
    lines = [f"- {n.display} ({n.records})" for n in new_names]
    lines += [f"- {name} (already in use)" for name in context_names]
    user = (
        "Store names, with the number of records using each:\n" + "\n".join(lines)
    )
    try:
        # A sorting job run for every brand: the smaller model, like the
        # store suggestions in recaps.retailer_proposals. What it returns is
        # checked by vet_ai_group and then by a person.
        out = generate_json(
            _AI_SYSTEM, user, schema=_AI_SCHEMA, max_tokens=3000, small=True
        )
    except AiUnavailable:
        return None
    except Exception:  # noqa: BLE001
        logger.info("retailer aliases: AI call failed", exc_info=True)
        return None
    if not isinstance(out, dict) or not isinstance(out.get("groups"), list):
        return None
    return [g for g in out["groups"] if isinstance(g, dict)]


def vet_ai_group(
    raw: dict,
    *,
    sent: dict[str, NameInfo],
    new_keys: set[str],
    numbers: dict[str, str],
    owner_of: dict[str, str],
    accepted: dict[str, str],
    retailer_names: set[str],
) -> Group | None:
    """Turn one model group into a :class:`Group`, or refuse it (None).

    ``sent`` maps alias keys to the names that were sent; ``numbers`` holds
    each name's confirmed store number; ``owner_of`` maps a context name's
    key to the existing group / canonical it already belongs to.
    """
    members: list[NameInfo] = []
    seen: set[str] = set()
    for name in raw.get("names") or []:
        key = alias_key(str(name))
        info = sent.get(key)
        if info is None or key in seen:
            continue  # not a name we sent: the model may not invent one
        seen.add(key)
        members.append(info)
    if len(members) < 2:
        return None
    if len(members) > MAX_GROUP_NAMES:
        logger.info("retailer aliases: refused an AI group of %s names", len(members))
        return None
    if not any(m.key in new_keys for m in members):
        return None
    if len({numbers.get(m.key, "") for m in members}) > 1:
        return None  # mixes store numbers, or a numbered store with its chain
    owners = {owner_of[m.key] for m in members if m.key in owner_of}
    if len(owners) > 1:
        return None  # would join two groups that already exist

    kind = "store" if str(raw.get("kind")) == "store" else "chain"
    anchor = _most_used(members, retailer_names)
    if _cities_disagree(members):
        if kind != "chain" or not all(
            _clearly_same_chain(anchor.display, m.display) for m in members
        ):
            return None

    suggested = " ".join(str(raw.get("canonical") or "").split())
    canonical = anchor.display
    member_chain_keys = {chain_key(m.display) for m in members}
    if suggested and len(suggested) <= _MAX_NAME and chain_key(suggested) in member_chain_keys:
        canonical = suggested
    for m in members:
        # A name that is already filed under something keeps that name.
        if m.key in owner_of:
            canonical = owner_of[m.key]
    canonical = resolve_store_name(canonical, accepted)
    number = numbers.get(members[0].key, "")
    return Group(
        kind="store" if number else kind,
        canonical=canonical,
        members=members,
        source="ai",
        reason=" ".join(str(raw.get("reason") or "").split())[:300],
        store_number=number,
        ident=("ai", alias_key(canonical)),
    )


def _group_key(tenant_id: int, ident: tuple[str, str], part: int) -> str:
    raw = f"{tenant_id}|{ident[0]}|{ident[1]}|{part}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]


@dataclass
class ProposeResult:
    names: int = 0
    groups: int = 0
    proposed: int = 0
    ai_used: bool = False
    ai_available: bool = True
    refused: int = 0


def _store_groups(tenant_id: int, groups: list[Group], existing: dict[str, object]) -> tuple[int, int]:
    """Write ``proposed`` rows for the groups. Returns (groups, rows) written.

    A name that already has a decision (accepted or dismissed) or is already
    proposed is left exactly as it is. A group of more than
    :data:`MAX_GROUP_NAMES` names is split into several groups.
    """
    from events.models import RetailerAlias

    groups_written = rows_written = 0
    size = MAX_GROUP_NAMES - 1  # the canonical name is the last seat
    # Names already waiting in each group, so a new spelling joins a group
    # with room instead of growing one past the limit.
    filled: Counter = Counter(
        row.group_key
        for row in existing.values()
        if row.status == RetailerAlias.STATUS_PROPOSED and row.group_key
    )
    for group in groups:
        canonical_key = alias_key(group.canonical)
        todo = [
            m
            for m in sorted(group.members, key=lambda m: (-m.records, m.key))
            if m.key != canonical_key
            and getattr(existing.get(m.key), "status", RetailerAlias.STATUS_NONE)
            == RetailerAlias.STATUS_NONE
        ]
        touched: set[str] = set()
        part = 0
        for member in todo:
            key = _group_key(tenant_id, group.ident, part)
            while filled[key] >= size:
                part += 1
                key = _group_key(tenant_id, group.ident, part)
            filled[key] += 1
            touched.add(key)
            RetailerAlias.objects.update_or_create(
                tenant_id=tenant_id,
                alias_key=member.key,
                defaults={
                    "alias_name": member.display[:255],
                    "canonical_name": group.canonical[:255],
                    "kind": group.kind,
                    "store_number": group.store_number[:16],
                    "group_key": key,
                    "source": group.source,
                    "reason": group.reason[:300],
                    "status": RetailerAlias.STATUS_PROPOSED,
                    "decided_by": None,
                    "decided_at": None,
                },
            )
            rows_written += 1
        groups_written += len(touched)
    return groups_written, rows_written


def propose_for_tenant(
    tenant_id: int, *, use_ai: bool = True, dry_run: bool = False
) -> ProposeResult:
    """Look at one tenant's store names and store what looks like the same
    store as ``proposed`` rows. Changes no chart: only accepted rows are read.
    """
    from events.models import Retailer, RetailerAlias

    names = collect_store_names(tenant_id)
    accepted = accepted_alias_map(tenant_id)
    retailer_names = set(
        Retailer.objects.filter(tenant_id=tenant_id).values_list("name", flat=True)
    )
    existing = {
        row.alias_key: row for row in RetailerAlias.objects.filter(tenant_id=tenant_id)
    }
    result = ProposeResult(names=len(names))

    groups, parsed = deterministic_groups(names, accepted, retailer_names)
    grouped_keys = {m.key for g in groups for m in g.members}

    ai_found: list[Group] = []
    asked: list[NameInfo] = []
    if use_ai:
        new = sorted(
            (
                p.info
                for p in parsed
                if p.info.key not in grouped_keys and p.info.key not in existing
            ),
            key=lambda n: (-n.records, n.key),
        )[:MAX_AI_NEW_NAMES]
        if new:
            new_keys = {n.key for n in new}
            # Names the new ones may belong with: what groups resolve to, and
            # names that already have a decision.
            owner_of: dict[str, str] = {}
            context: dict[str, NameInfo] = {}
            for g in groups:
                key = alias_key(g.canonical)
                owner_of[key] = g.canonical
                context.setdefault(key, names.get(key) or NameInfo(key=key, spellings=Counter({g.canonical: 1})))
            for key, row in existing.items():
                if row.status == RetailerAlias.STATUS_ACCEPTED and row.canonical_name:
                    ckey = alias_key(row.canonical_name)
                    owner_of[ckey] = row.canonical_name
                    context.setdefault(
                        ckey,
                        names.get(ckey) or NameInfo(key=ckey, spellings=Counter({row.canonical_name: 1})),
                    )
            for p in parsed:
                if p.info.key in new_keys or p.info.key in grouped_keys:
                    continue
                if p.info.key in existing and existing[p.info.key].status != RetailerAlias.STATUS_NONE:
                    continue
                context.setdefault(p.info.key, p.info)
            context_list = sorted(
                (c for k, c in context.items() if k not in new_keys),
                key=lambda n: (-n.records, n.key),
            )[:MAX_AI_CONTEXT_NAMES]
            raw_groups = ai_groups(new, [c.display for c in context_list])
            if raw_groups is None:
                result.ai_available = False
            else:
                result.ai_used = True
                asked = new
                sent = {n.key: n for n in new}
                sent.update({c.key: c for c in context_list})
                numbers = {p.info.key: p.ident[1] for p in parsed}
                taken: set[str] = set()
                for raw in raw_groups:
                    group = vet_ai_group(
                        raw,
                        sent=sent,
                        new_keys=new_keys,
                        numbers=numbers,
                        owner_of=owner_of,
                        accepted=accepted,
                        retailer_names=retailer_names,
                    )
                    if group is None or any(m.key in taken for m in group.members):
                        result.refused += 1
                        continue
                    taken.update(m.key for m in group.members)
                    ai_found.append(group)

    if dry_run:
        result.groups = len(groups) + len(ai_found)
        return result

    with transaction.atomic():
        written_groups, written_rows = _store_groups(tenant_id, groups + ai_found, existing)
        result.groups, result.proposed = written_groups, written_rows
        # Remember the names the model was asked about and did not group, so
        # the next run only asks about names it has not seen.
        waiting = set(
            RetailerAlias.objects.filter(
                tenant_id=tenant_id, status=RetailerAlias.STATUS_PROPOSED
            ).values_list("alias_key", flat=True)
        )
        to_remember = [
            RetailerAlias(
                tenant_id=tenant_id,
                alias_key=n.key,
                alias_name=n.display[:255],
                canonical_name="",
                status=RetailerAlias.STATUS_NONE,
                source=RetailerAlias.SOURCE_AI,
            )
            for n in asked
            if n.key not in waiting and n.key not in existing
        ]
        if to_remember:
            RetailerAlias.objects.bulk_create(to_remember, ignore_conflicts=True)
    return result


# ─── Deciding (staff) ───────────────────────────────────────────────


class AliasError(ValueError):
    """A decision that cannot be applied; the message is shown to staff."""


def decide_group(
    tenant_id: int,
    group_key: str,
    action: str,
    *,
    actor,
    canonical_name: str | None = None,
    alias_uuids: list[str] | None = None,
) -> tuple[int, int]:
    """Accept or dismiss a proposed group. Returns (rows changed, records an
    accept re-labels). Only :class:`RetailerAlias` rows are written."""
    from events.models import RetailerAlias

    if action not in ("accept", "dismiss"):
        raise AliasError("Choose accept or dismiss.")
    with transaction.atomic():
        rows = list(
            RetailerAlias.objects.select_for_update().filter(
                tenant_id=tenant_id,
                group_key=group_key,
                status=RetailerAlias.STATUS_PROPOSED,
            )
        )
        if alias_uuids:
            wanted = {str(u) for u in alias_uuids}
            rows = [r for r in rows if str(r.uuid) in wanted]
        if not rows:
            raise AliasError("That suggestion is no longer open.")
        now = timezone.now()
        if action == "dismiss":
            for row in rows:
                row.status = RetailerAlias.STATUS_DISMISSED
                row.decided_by, row.decided_at = actor, now
                row.save(update_fields=["status", "decided_by", "decided_at", "updated_at"])
            return len(rows), 0

        if len(rows) + 1 > MAX_GROUP_NAMES:
            raise AliasError(
                f"That is {len(rows) + 1} names in one group. Accept at most "
                f"{MAX_GROUP_NAMES} at a time so each one can be checked."
            )
        proposed_canonical = rows[0].canonical_name
        canonical = " ".join((canonical_name or "").split()) or proposed_canonical
        if not canonical or len(canonical) > 255 or is_junk_name(canonical):
            raise AliasError("Enter the store name to keep.")
        canonical_key = alias_key(canonical)
        clash = RetailerAlias.objects.filter(
            tenant_id=tenant_id,
            alias_key=canonical_key,
            status=RetailerAlias.STATUS_ACCEPTED,
        ).first()
        if clash is not None and alias_key(clash.canonical_name) != canonical_key:
            raise AliasError(
                f"“{canonical}” is already shown as “{clash.canonical_name}”. "
                "Use that name, or undo that one first."
            )

        changed = 0
        for row in rows:
            if row.alias_key == canonical_key:
                # Staff chose this spelling as the one to keep.
                row.status = RetailerAlias.STATUS_NONE
                row.canonical_name = ""
            else:
                row.status = RetailerAlias.STATUS_ACCEPTED
                row.canonical_name = canonical
            row.decided_by, row.decided_at = actor, now
            row.save()
            changed += 1

        # A different spelling was chosen: the one first proposed becomes an
        # alias too, so both spellings end up on the same row of a chart.
        old_key = alias_key(proposed_canonical)
        if old_key != canonical_key and proposed_canonical:
            held = RetailerAlias.objects.filter(tenant_id=tenant_id, alias_key=old_key).first()
            if held is None or held.status in (
                RetailerAlias.STATUS_NONE,
                RetailerAlias.STATUS_PROPOSED,
            ):
                RetailerAlias.objects.update_or_create(
                    tenant_id=tenant_id,
                    alias_key=old_key,
                    defaults={
                        "alias_name": proposed_canonical,
                        "canonical_name": canonical,
                        "kind": rows[0].kind,
                        "store_number": rows[0].store_number,
                        "group_key": group_key,
                        "source": rows[0].source,
                        "reason": rows[0].reason,
                        "status": RetailerAlias.STATUS_ACCEPTED,
                        "decided_by": actor,
                        "decided_at": now,
                    },
                )
                changed += 1
            # Numbered stores still waiting follow the banner's new spelling.
            if rows[0].kind == RetailerAlias.KIND_CHAIN:
                prefix = f"{proposed_canonical} #"
                for waiting in RetailerAlias.objects.filter(
                    tenant_id=tenant_id,
                    status=RetailerAlias.STATUS_PROPOSED,
                    kind=RetailerAlias.KIND_STORE,
                    canonical_name__startswith=prefix,
                ):
                    waiting.canonical_name = f"{canonical} #{waiting.canonical_name[len(prefix):]}"
                    waiting.save(update_fields=["canonical_name", "updated_at"])

    names = collect_store_names(tenant_id)
    affected = sum(
        names[r.alias_key].records
        for r in RetailerAlias.objects.filter(
            tenant_id=tenant_id, group_key=group_key, status=RetailerAlias.STATUS_ACCEPTED
        )
        if r.alias_key in names
    )
    return changed, affected


def undo_alias(tenant_id: int, alias_uuid: str, *, actor) -> int:
    """Stop showing one accepted name under another. The row is kept as
    ``dismissed`` so it is not proposed again."""
    from events.models import RetailerAlias

    return RetailerAlias.objects.filter(
        tenant_id=tenant_id, uuid=alias_uuid, status=RetailerAlias.STATUS_ACCEPTED
    ).update(
        status=RetailerAlias.STATUS_DISMISSED,
        decided_by=actor,
        decided_at=timezone.now(),
        updated_at=timezone.now(),
    )


# ─── GraphQL ────────────────────────────────────────────────────────


@strawberry.type
class StoreNameAlias:
    uuid: strawberry.ID
    name: str
    # Records (events, and requests with no event yet) that carry this name.
    records: int
    # Up to three "City, ST" the name is used in, for staff to judge by.
    places: str


@strawberry.type
class StoreNameGroup:
    group_key: str
    canonical_name: str
    # "chain" = one banner spelled differently; "store" = one numbered store.
    kind: str
    # "rule" = same once case and punctuation are ignored; "ai" = suggested.
    source: str
    reason: str
    canonical_records: int
    # Records whose shown store name would change if this is accepted.
    affected_records: int
    names: list[StoreNameAlias]


@strawberry.type
class StoreNameAccepted:
    uuid: strawberry.ID
    name: str
    canonical_name: str
    records: int


@strawberry.type
class StoreNameSuggestions:
    groups: list[StoreNameGroup]
    accepted: list[StoreNameAccepted]
    max_group_size: int


@strawberry.type
class StoreNameDecision:
    ok: bool
    message: str
    changed: int = 0
    affected_records: int = 0


@strawberry.type
class StoreNameCheck:
    ok: bool
    message: str
    names_checked: int = 0
    groups_found: int = 0
    ai_used: bool = False


def _places(info: NameInfo | None) -> str:
    if info is None or not info.cities:
        return ""
    shown = []
    for city in sorted(info.cities)[:3]:
        name, _, state = city.partition("|")
        shown.append(f"{name.title()}, {state}")
    more = len(info.cities) - len(shown)
    return " · ".join(shown) + (f" · +{more} more" if more > 0 else "")


def list_suggestions(tenant_id: int) -> StoreNameSuggestions:
    from events.models import RetailerAlias

    names = collect_store_names(tenant_id)

    def records(key: str) -> int:
        info = names.get(key)
        return info.records if info else 0

    grouped: dict[str, list] = {}
    for row in RetailerAlias.objects.filter(
        tenant_id=tenant_id, status=RetailerAlias.STATUS_PROPOSED
    ).order_by("id"):
        grouped.setdefault(row.group_key, []).append(row)
    groups: list[StoreNameGroup] = []
    for key, rows in grouped.items():
        aliases = [
            StoreNameAlias(
                uuid=str(r.uuid),
                name=r.alias_name,
                records=records(r.alias_key),
                places=_places(names.get(r.alias_key)),
            )
            for r in rows
        ]
        aliases.sort(key=lambda a: (-a.records, a.name.casefold()))
        groups.append(
            StoreNameGroup(
                group_key=key,
                canonical_name=rows[0].canonical_name,
                kind=rows[0].kind,
                source=rows[0].source,
                reason=rows[0].reason,
                canonical_records=records(alias_key(rows[0].canonical_name)),
                affected_records=sum(a.records for a in aliases),
                names=aliases,
            )
        )
    groups.sort(key=lambda g: (-g.affected_records, g.canonical_name.casefold()))
    accepted = [
        StoreNameAccepted(
            uuid=str(r.uuid),
            name=r.alias_name,
            canonical_name=r.canonical_name,
            records=records(r.alias_key),
        )
        for r in RetailerAlias.objects.filter(
            tenant_id=tenant_id, status=RetailerAlias.STATUS_ACCEPTED
        ).order_by("canonical_name", "alias_name")
    ]
    return StoreNameSuggestions(
        groups=groups, accepted=accepted, max_group_size=MAX_GROUP_NAMES
    )


async def _staff_tenant(info: strawberry.Info, tenant_id) -> int:
    """The tenant id, for Ignite staff only. A brand user (their own brand
    included) and an ambassador are refused: store names are tidied by staff."""
    is_admin, _allowed = await _accessible_tenants(info.context.request.user)
    if not is_admin:
        raise GraphQLError("Staff only.")
    try:
        return resolve_id_to_int(tenant_id)
    except Exception as exc:  # noqa: BLE001
        raise GraphQLError("Invalid tenant id.") from exc


@strawberry.type
class RetailerAliasQueries:
    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def store_name_suggestions(
        self, info: strawberry.Info, tenant_id: strawberry.ID
    ) -> StoreNameSuggestions:
        """Store names that look like the same store, waiting for staff."""
        resolved = await _staff_tenant(info, tenant_id)
        return await sync_to_async(list_suggestions)(resolved)


@strawberry.type
class RetailerAliasMutations:
    @strawberry.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def decide_store_name_group(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        group_key: str,
        action: str,
        canonical_name: str | None = None,
        alias_uuids: list[strawberry.ID] | None = None,
    ) -> StoreNameDecision:
        """Accept (optionally under a corrected name) or dismiss one group."""
        resolved = await _staff_tenant(info, tenant_id)
        user = info.context.request.user
        try:
            changed, affected = await sync_to_async(decide_group)(
                resolved,
                group_key,
                action,
                actor=user,
                canonical_name=canonical_name,
                alias_uuids=[str(u) for u in alias_uuids] if alias_uuids else None,
            )
        except AliasError as exc:
            return StoreNameDecision(ok=False, message=str(exc))
        return StoreNameDecision(
            ok=True, message="", changed=changed, affected_records=affected
        )

    @strawberry.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def undo_store_name_alias(
        self, info: strawberry.Info, tenant_id: strawberry.ID, alias_uuid: strawberry.ID
    ) -> StoreNameDecision:
        """Show an accepted name on its own again."""
        resolved = await _staff_tenant(info, tenant_id)
        user = info.context.request.user
        try:
            changed = await sync_to_async(undo_alias)(resolved, str(alias_uuid), actor=user)
        except Exception:  # noqa: BLE001 — a malformed id is "nothing to undo"
            changed = 0
        if not changed:
            return StoreNameDecision(ok=False, message="That name is not combined any more.")
        return StoreNameDecision(ok=True, message="", changed=changed)

    @strawberry.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def check_store_names(
        self, info: strawberry.Info, tenant_id: strawberry.ID
    ) -> StoreNameCheck:
        """Look through one brand's store names now. Stores suggestions only."""
        resolved = await _staff_tenant(info, tenant_id)
        started = time.monotonic()
        try:
            result = await sync_to_async(propose_for_tenant)(resolved)
        except Exception:  # noqa: BLE001
            logger.exception("check_store_names failed tenant=%s", resolved)
            return StoreNameCheck(ok=False, message="Could not check store names. Try again.")
        logger.info(
            "check_store_names tenant=%s names=%s groups=%s in %.1fs",
            resolved, result.names, result.groups, time.monotonic() - started,
        )
        if result.groups:
            message = ""
        elif not result.ai_available:
            message = (
                "No names differ only by capitals or punctuation. Looser matches "
                "could not be checked right now."
            )
        else:
            message = "No new names look like the same store."
        return StoreNameCheck(
            ok=True,
            message=message,
            names_checked=result.names,
            groups_found=result.groups,
            ai_used=result.ai_used,
        )
