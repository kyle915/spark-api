"""Retitle already-filed recaps to "Store Name #1234" (Torch client ask).

New walk-up filings get this title from the recap form's Store boxes; this
command brings the recaps filed before that up to the same standard.

Store name: the store part of the event title (walk-in titles are
"M/D/YYYY - <address> (<store>)"), else the recap's Retailer, else a store
name another request/event of the brand uses at the same address.

Store directory (``--directory`` data files and/or ``--numbers-json``): the
client's own store list. A directory hit replaces the name with the list's
name and supplies the number. Walk-in addresses are geocoder guesses (3310 vs
3308 Glenstone, "Ballwin;Town & Country", a neighboring zip), so a store of
the same chain is matched by, in order: exact name, address, zip, house
number + street, city, street name, then a "#1234" the BA typed in the
recap title — each only when exactly one store fits. A walk-in with no store
name at all matches on house number + street, or zip + street name.

Otherwise the number comes from the event's Request.store_number or any live
request of the brand at the same address (chain+zip placeholders ignored).
Recaps with no number found keep their title unless ``--include-unnumbered``:
a bare "lol liquors" says less than the walk-in title's address did.

Only system titles are rewritten (the event title, the event title + a
" · <shift>" suffix, or an earlier run's "Store Name" form). A title an admin
typed by hand is reported as ``custom`` and left alone — unless the directory
knows the store and the title names that chain without its number ("Total
wine #Sunset valley"); a title carrying another listed store's # is
reported as ``conflict``.

SAFE: dry-run by default; ``--apply`` writes. Idempotent. The report ends
with the stores still missing a number so they can be collected.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from ambassadors.checkin_web import (
    addresses_fuzzy_match,
    compose_shift_recap_name,
    compose_store_recap_name,
    known_store_name,
    known_store_number,
    normalize_place,
    real_store_number,
    store_identity_prefill,
)

DATA_DIR = Path(__file__).resolve().parent / "data"
_NUMBER_SUFFIX = re.compile(r"\s#\s*[\w-]+$")


@dataclass(frozen=True)
class DirectoryStore:
    name: str
    address: str
    number: str


def _key(value: str) -> str:
    """Lowercase, curly/straight apostrophes dropped, punctuation → spaces."""
    value = (value or "").lower().replace("’", "").replace("'", "")
    return " ".join(re.sub(r"[^a-z0-9#]+", " ", value).split())


def _zip(text: str) -> str:
    found = re.findall(r"\b(\d{5})(?:-\d{4})?\b", text or "")
    return found[-1] if found else ""


_DIRECTIONS = {"n", "s", "e", "w", "ne", "nw", "se", "sw", "north", "south", "east", "west"}
_STREET_TYPES = {
    "ave", "avenue", "st", "street", "rd", "road", "dr", "drive", "blvd", "boulevard",
    "ln", "lane", "ct", "court", "pkwy", "parkway", "hwy", "highway", "way", "trl",
    "trail", "cir", "circle", "pl", "place", "expy", "expressway", "fwy", "freeway",
}


def _street_parts(text: str) -> tuple[str, str]:
    """("7330", "north") from "7330 W. North Ave"; ("3954a", "peachtree") from "3954 A Peachtree".

    The name is "" unless the text reads like a street: a house number, or a
    street type before the first comma ("Brentwood Promenade Court", not
    "lake zurich Illinois").
    """
    words = _key(text).split()
    first_part = set(_key((text or "").split(",")[0]).split())
    number = ""
    if words and re.fullmatch(r"\d+[a-z]?", words[0]):
        number, words = words[0], words[1:]
        if number.isdigit() and len(words) > 1 and len(words[0]) == 1 and words[0] not in _DIRECTIONS:
            number, words = number + words[0], words[1:]
    while len(words) > 1 and words[0] in _DIRECTIONS and words[1] not in _STREET_TYPES:
        words = words[1:]
    if not words or words[0].isdigit():
        return number, ""
    if not number and (len(words) < 2 or not (first_part - {words[0]}) & _STREET_TYPES):
        return "", ""
    return number, words[0]


def _street(text: str) -> str:
    """"11221 legacy" from "11221 Legacy Ave. West Palm Beach"; "3954 A Peachtree" → "3954a peachtree"."""
    number, name = _street_parts(text)
    return f"{number} {name}" if number and name else ""


def _street_name(text: str) -> str:
    return _street_parts(text)[1]


def _place(title: str) -> str:
    """The address part of a walk-in title "M/D/YYYY - <address> (<store>)"."""
    return re.sub(r"\s*\([^()]*\)\s*$", "", re.sub(r"^\d{1,2}/\d{1,2}/\d{4}\s*-\s*", "", title or ""))


def _city(address: str) -> str:
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    return _key(parts[-2]) if len(parts) >= 3 else ""


def _chain(name: str) -> str:
    words = _key(name).split()
    return words[0] if words and len(words[0]) >= 4 else ""


def _only(stores: list[DirectoryStore]) -> DirectoryStore | None:
    return stores[0] if len({s.number for s in stores}) == 1 else None


def _typed_numbers(text: str) -> set[str]:
    return set(re.findall(r"#\s*(\d+)\b", text or ""))


def _first_fit(stores: list[DirectoryStore], tests) -> DirectoryStore | None:
    for fits in tests:
        hit = _only([s for s in stores if fits(s)])
        if hit:
            return hit
    return None


def match_directory(
    directory: list[DirectoryStore], store_name: str, address: str, title: str, typed: str = ""
) -> DirectoryStore | None:
    """``typed``: what the BA titled the recap ("Binnys Niles", "Total Wine #909")."""
    if not directory:
        return None
    name_key = _key(store_name)
    if name_key:
        hit = _only([s for s in directory if _key(s.name) == name_key])
        if hit:
            return hit
    if address:
        hit = _only([s for s in directory if addresses_fuzzy_match(s.address, address)])
        if hit:
            return hit
    haystack = f" {_key(store_name)} {_key(title)} {_key(address)} {_key(typed)} "
    place = address or _place(title)
    zip_code = _zip(address) or _zip(title)
    street = _street(place)
    street_name = _street_name(place)
    same_chain = [s for s in directory if _chain(s.name) and f" {_chain(s.name)} " in haystack]
    if same_chain:
        by_place = _first_fit(
            same_chain,
            (
                lambda s: bool(zip_code) and _zip(s.address) == zip_code,
                lambda s: bool(street) and _street(s.address) == street,
                lambda s: bool(_city(s.address)) and f" {_city(s.address)} " in haystack,
                lambda s: bool(street_name) and _street_name(s.address) == street_name,
            ),
        )
        return by_place or _only([s for s in same_chain if s.number in _typed_numbers(typed)])
    if name_key:
        return None
    # No store name at all: a walk-in that only typed the address.
    return _first_fit(
        directory,
        (
            lambda s: bool(_street(s.address))
            and (_street(s.address) == street or f" {_street(s.address)} " in haystack),
            lambda s: bool(zip_code and street_name)
            and _zip(s.address) == zip_code
            and _street_name(s.address) == street_name,
        ),
    )


def load_directory(keys: str, numbers_json: str) -> list[DirectoryStore]:
    stores: list[DirectoryStore] = []
    for key in [k.strip() for k in (keys or "").split(",") if k.strip()]:
        path = DATA_DIR / f"{key}.json"
        if not re.fullmatch(r"[a-z0-9_]+", key) or not path.exists():
            raise CommandError(f"unknown store directory: {key}")
        for row in json.loads(path.read_text())["stores"]:
            stores.append(DirectoryStore(row["name"], row.get("address", ""), str(row["number"])))
    if numbers_json:
        try:
            supplied = json.loads(numbers_json)
        except json.JSONDecodeError as exc:
            raise CommandError(f"--numbers-json is not valid JSON: {exc}") from exc
        if not isinstance(supplied, dict):
            raise CommandError("--numbers-json must be a JSON object")
        for key, number in supplied.items():
            number = real_store_number(str(number))
            looks_like_address = bool(re.match(r"^\s*\d", key))
            stores.append(
                DirectoryStore("" if looks_like_address else key, key if looks_like_address else "", number)
            )
    return stores


def _custom_reason(
    directory: list[DirectoryStore], listed: DirectoryStore | None, current: str
) -> str:
    """Why a hand-typed title stays: "custom", or "conflict" when it names another listed store's #.

    "" lets it be retitled: the client's list knows the store and the title
    names that chain but not its number ("Total wine #Sunset valley").
    """
    chain = _chain(listed.name) if listed is not None else ""
    if not chain or f" {chain} " not in f" {_key(current)} ":
        return "custom"
    typed = _typed_numbers(current)
    if listed.number in typed:
        return "custom"
    if typed & {s.number for s in directory if _chain(s.name) == chain}:
        return "conflict"
    return ""


def _split_shift(current: str, event_name: str, store_name: str) -> tuple[str, str]:
    """(title without " · <shift>", shift) when the suffix sits on a system title."""
    base, sep, shift = current.rpartition(" · ")
    if not sep:
        return current, ""
    unnumbered = _NUMBER_SUFFIX.sub("", base).strip()
    if base in (event_name, store_name) or unnumbered == store_name:
        return base, shift.strip()
    return current, ""


class Command(BaseCommand):
    help = 'Retitle filed recaps to "Store Name #1234". Dry-run unless --apply.'

    def add_arguments(self, parser):
        parser.add_argument("--tenant-slug", default="keee-torch-thc")
        parser.add_argument(
            "--directory",
            default="",
            help="Comma-separated store lists in commands/data/ (e.g. torch_total_wine_stores,torch_binnys_stores).",
        )
        parser.add_argument(
            "--numbers-json",
            default="",
            help='JSON object {"<store name or address>": "<store #>"}.',
        )
        parser.add_argument(
            "--include-unnumbered",
            action="store_true",
            help="Also retitle recaps with no store # found (drops the address from walk-in titles).",
        )
        parser.add_argument("--apply", action="store_true", help="Write changes (default: dry-run).")

    def handle(self, *args, **opts):
        from recaps.models import CustomRecap
        from tenants.models import Tenant

        slug = opts["tenant_slug"]
        apply = opts["apply"]
        directory = load_directory(opts["directory"], opts["numbers_json"])

        tenant = (
            Tenant.objects.filter(slug=slug).order_by("id").first()
            or Tenant.objects.filter(request_url_name=slug).order_by("id").first()
        )
        if tenant is None:
            raise CommandError(f"tenant-not-found: {slug}")

        recaps = (
            CustomRecap.objects.filter(tenant=tenant, event__isnull=False)
            .select_related("event", "event__request", "retailer")
            .order_by("id")
        )
        counts: Counter[str] = Counter()
        missing_number: Counter[tuple[str, str]] = Counter()
        address_numbers: dict[str, str] = {}
        address_names: dict[str, str] = {}

        for recap in recaps.iterator():
            event = recap.event
            current = (recap.name or "").strip()
            event_name = (event.name or "").strip()
            address = (event.address or "").strip()
            store_name = store_identity_prefill(event)["name"] or (
                (recap.retailer.name or "").strip() if recap.retailer_id else ""
            )
            if not store_name and address:
                addr_key = normalize_place(address)
                if addr_key not in address_names:
                    address_names[addr_key] = known_store_name(tenant.id, address)
                store_name = address_names[addr_key]

            number = ""
            listed = match_directory(directory, store_name, address, event_name, current)
            if listed is not None:
                store_name = listed.name or store_name
                number = listed.number
            if not store_name:
                counts["no-store-name"] += 1
                self.stdout.write(f"no-store-name  #{recap.id}  {current!r}")
                continue

            base, shift = _split_shift(current, event_name, store_name)
            unnumbered = _NUMBER_SUFFIX.sub("", base).strip()
            if base != event_name and unnumbered != store_name and _key(base) != _key(event_name):
                reason = _custom_reason(directory, listed, current)
                if reason:
                    counts[reason] += 1
                    seen = f"  (list: {listed.name} #{listed.number})" if reason == "conflict" else ""
                    self.stdout.write(f"{reason:<15}#{recap.id}  {current!r}{seen}")
                    continue

            if not number:
                req = getattr(event, "request", None)
                number = real_store_number(req.store_number) if req is not None else ""
            if not number and address:
                addr_key = normalize_place(address)
                if addr_key not in address_numbers:
                    address_numbers[addr_key] = known_store_number(tenant.id, address)
                number = address_numbers[addr_key]
            if not number and base != event_name and base.startswith(store_name):
                number = real_store_number(base[len(store_name):])

            proposed = compose_store_recap_name(store_name, number)
            if shift:
                proposed = compose_shift_recap_name(proposed, shift)
            if not number:
                missing_number[(store_name, address)] += 1
                if not opts["include_unnumbered"]:
                    counts["kept-no-number"] += 1
                    continue

            if proposed == current:
                counts["already"] += 1
                continue
            counts["renamed" if number else "renamed-no-number"] += 1
            self.stdout.write(f"rename         #{recap.id}  {current!r} -> {proposed!r}")
            if apply:
                CustomRecap.objects.filter(id=recap.id).update(name=proposed[:255])

        mode = "APPLIED" if apply else "DRY-RUN"
        self.stdout.write(f"\n{mode} {slug}: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        if missing_number:
            self.stdout.write(f"\nStores with no store # on file ({len(missing_number)}):")
            for (store, address), n in sorted(missing_number.items()):
                self.stdout.write(f"  {store} | {address} | {n} recap(s)")
