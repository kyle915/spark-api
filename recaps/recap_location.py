"""Give each recap a real retailer, store number, city and state.

Recaps often say where they happened only as free text, or not at all: City
blank on every Torch THC recap, State on a handful, Girl Beer stores typed as
"heb". Per-retailer and per-state reports then show blanks or "heb". This
module works out, per :class:`recaps.models.CustomRecap`::

    {retailer (the chain's canonical name), store_number, city, state_code}

with a ``source`` and a ``confidence`` for every value, and (only when asked)
writes them onto the recap itself. The shared Event is never touched, so other
recaps of the same event keep what they have.

Order of evidence
-----------------
1. What the recap and its event already carry (recap / event / request
   retailer, the State and Location FKs). A value there is kept.
2. Deterministic readings of location text: the event, request and typed
   store addresses (``events.market.parse_address_geo``), the recap's own
   store / city / state / address answers, store-number patterns such as
   "HEB #614" or "H-E-B 540", accepted store-name aliases
   (``events.retailer_aliases``) and a fixed list of chain spellings.
2b. The state from where the recap's BA clocked in to its event
   (``Attendance.coordinates`` of the clock-in, and the mobile app's clock-in
   ``LocationPing``), looked up offline in shipped state shapes
   (``recaps.us_state_geo``). Only when the recap shows no state. A fix with
   poor stated accuracy, far from the event's own position, or too close to
   another state's line is not used; one that disagrees with an address or
   answer blanks the state, like any other disagreement. The city is never
   read from GPS, but a city the recap names gets placed in the GPS state
   (medium) unless the Location catalog puts that city only elsewhere.
3. The AI text model, only for what is still blank, given only location text
   (event name and address, the store / city / state / address answers). No
   ambassador names, phone numbers, emails or other people data are sent;
   names of the recap's ambassador are blanked out of the text first.

Fail closed: two sources that disagree leave the value blank, an AI answer is
kept only when the text it was given supports it, and a chain the system does
not know is reported, never invented. A wrong state is worse than a blank one.

What is written (``apply=True`` only)
-------------------------------------
* ``CustomRecap.retailer`` when the recap has no retailer, or when the one it
  shows is the same chain written another way ("heb" -> "H-E-B"). A retailer
  set on the recap by someone else (not listed in ``location_fill``) is never
  replaced.
* ``CustomRecap.store_number`` when blank.
* ``CustomRecap.state`` when neither the recap, its event nor the event's
  Location has a state.
* ``CustomRecap.location`` (the city) when neither the recap nor its event has
  one, and only together with a known state.
* ``CustomRecap.location_fill``: what was written, from where, and what was
  there before, so a re-run is a no-op and a person can see why.

Writes use ``QuerySet.update`` so a backfill does not fire the per-save sheet
mirror once per recap; the nightly sheet sync picks the values up.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date

from django.db import transaction
from django.utils import timezone

from events.market import parse_address_geo
from events.retailer_aliases import (
    _GENERIC_WORDS,
    _words,
    accepted_alias_map,
    chain_key,
    is_junk_name,
    resolve_store_name,
    split_store_number,
)
from events.us_states import US_STATE_CODES, US_STATE_NAME_TO_CODE

logger = logging.getLogger(__name__)

FIELDS = ("retailer", "store_number", "city", "state_code")

CONF_HIGH = "high"
CONF_MEDIUM = "medium"

GPS_SOURCE = "clockin_gps"
# A clock-in fix whose device said it was this unsure is not used.
GPS_MAX_ACCURACY_M = 1000.0
# A fix this far from the event's own coordinates is the wrong place (a BA
# clocking in from home or the car park of another town), not the event.
GPS_MAX_FROM_EVENT_MILES = 25.0
# Closer than this (plus the fix's stated accuracy) to another state's line:
# too close to call with simplified shapes, left blank. Closer than
# GPS_NEAR_BORDER_KM: used, at medium confidence.
GPS_BORDER_UNSURE_KM = 1.0
GPS_NEAR_BORDER_KM = 5.0

# Recaps sent to the model in one call, and the default cap on calls per run.
AI_BATCH = 15
DEFAULT_MAX_AI_CALLS = 10

# Canonical chain name -> spellings, compared with
# ``events.retailer_aliases.chain_key`` (case, spaces and punctuation out).
CHAINS: dict[str, tuple[str, ...]] = {
    "H-E-B": ("heb", "hebplus", "hebgrocery", "hebfoodstore"),
    "Whole Foods Market": ("wholefoods", "wholefoodsmarket"),
    "Kroger": ("kroger", "krogers", "krogermarketplace"),
    "Albertsons": ("albertsons", "albertson"),
    "Vons": ("vons",),
    "Safeway": ("safeway",),
    "Ralphs": ("ralphs", "ralph"),
    "Pavilions": ("pavilions",),
    "Sprouts Farmers Market": ("sprouts", "sproutsfarmersmarket", "sproutsfarmers"),
    "Trader Joe's": ("traderjoes", "traderjoe"),
    "Publix": ("publix",),
    "Wegmans": ("wegmans",),
    "Harris Teeter": ("harristeeter",),
    "Meijer": ("meijer",),
    "Target": ("target",),
    "Walmart": ("walmart", "walmartsupercenter", "walmartneighborhoodmarket"),
    "Costco": ("costco", "costcowholesale"),
    "Sam's Club": ("samsclub",),
    "BJ's Wholesale Club": ("bjs", "bjswholesale", "bjswholesaleclub"),
    "Total Wine & More": ("totalwine", "totalwineandmore", "totalwinemore"),
    "BevMo!": ("bevmo",),
    "Spec's": ("specs", "specswinesspirits", "specsliquor"),
    "Binny's Beverage Depot": ("binnys", "binnysbeveragedepot"),
    "ABC Fine Wine & Spirits": ("abcfinewine", "abcfinewineandspirits", "abcfinewinespirits"),
    "Twin Liquors": ("twinliquors",),
    "Central Market": ("centralmarket",),
    "CVS": ("cvs", "cvspharmacy"),
    "Walgreens": ("walgreens",),
    "7-Eleven": ("7eleven", "seveneleven"),
    "Circle K": ("circlek",),
    "Food Lion": ("foodlion",),
    "Giant Eagle": ("gianteagle",),
    "Hy-Vee": ("hyvee",),
    "Stater Bros.": ("staterbros", "staterbrothers"),
    "Smart & Final": ("smartandfinal", "smartfinal"),
    "The Fresh Market": ("thefreshmarket", "freshmarket"),
    "Gelson's": ("gelsons",),
    "Bristol Farms": ("bristolfarms",),
    "Erewhon": ("erewhon",),
    "Jewel-Osco": ("jewelosco", "jewel"),
    "King Soopers": ("kingsoopers",),
    "Fry's Food Stores": ("frysfood", "frysfoodstores", "frysmarketplace"),
    "Fred Meyer": ("fredmeyer",),
    "Winn-Dixie": ("winndixie",),
    "ShopRite": ("shoprite",),
    "Stop & Shop": ("stopandshop", "stopshop"),
    "Hannaford": ("hannaford",),
    "Raley's": ("raleys",),
    "WinCo Foods": ("winco", "wincofoods"),
    "Lowes Foods": ("lowesfoods",),
    "Ingles": ("ingles", "inglesmarkets"),
    "Giant": ("giantfood",),
    "Food 4 Less": ("food4less", "foodforless"),
    "Cardenas Markets": ("cardenas", "cardenasmarkets"),
    "Northgate Market": ("northgate", "northgatemarket", "northgategonzalez"),
}
_CHAIN_BY_KEY: dict[str, str] = {}
for _canonical, _spellings in CHAINS.items():
    _CHAIN_BY_KEY[chain_key(_canonical)] = _canonical
    for _s in _spellings:
        _CHAIN_BY_KEY[_s] = _canonical
_MAX_CHAIN_TOKENS = 6

# Recap answers, by the field's name. People data is never read.
_PII_FIELD_RE = re.compile(
    r"\b(phone|e-?mail|contact|manager|ambassador|ba|signature|consumers?|"
    r"customers?|comments?|feedback|notes?|employees?|staff|buyer|rep|"
    r"photo|picture|selfie|feedback)\b",
    re.I,
)
_STORE_NUMBER_FIELD_RE = re.compile(
    r"\bstore\s*(?:#|no\b\.?|num(?:ber)?\b|id\b)", re.I
)
_STORE_FIELD_RE = re.compile(
    r"\b(store|retailer|account|venue|chain|banner|location)\b", re.I
)
_CITY_FIELD_RE = re.compile(r"\b(city|town)\b", re.I)
_STATE_FIELD_RE = re.compile(r"\bstate\b", re.I)
_ADDRESS_FIELD_RE = re.compile(r"\baddress\b", re.I)
# A store field that is about the store, not naming it.
_NOT_STORE_FIELD_RE = re.compile(
    r"\b(traffic|hours|sold|stock|inventory|price|display|type|size|"
    r"layout|condition|rating|placement|shelf|photos?|time)\b",
    re.I,
)

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_PHONE_RE = re.compile(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b")
_MARKED_NUMBER_RE = re.compile(
    r"(?:#|\bstore\s*#?|\bno\.?|\bstr\.?|\bunit)\s*#?\s*0*(\d{1,6})\b", re.I
)
_SEGMENT_SPLIT_RE = re.compile(r"\s+[-–—|·•]\s+|[|·•(),]")


# ─── Text helpers ───────────────────────────────────────────────────


def _clean(text) -> str:
    return " ".join(str(text or "").split())


def scrub(text: str, names: list[str] | None = None) -> str:
    """Text with emails, phone numbers and the given people's names blanked."""
    out = _EMAIL_RE.sub("[email]", text or "")
    out = _PHONE_RE.sub("[phone]", out)
    for name in names or ():
        for part in (name or "").split():
            if len(part) >= 3:
                out = re.sub(rf"(?<![A-Za-z]){re.escape(part)}(?![A-Za-z])", "[name]", out, flags=re.I)
    return out


def state_code_of(text: str | None) -> str:
    """A 2-letter code for "TX", "tx", "Texas"; "" for anything else."""
    cleaned = _clean(text).strip(" .,").casefold()
    if not cleaned:
        return ""
    if len(cleaned) == 2 and cleaned.upper() in US_STATE_CODES:
        return cleaned.upper()
    return US_STATE_NAME_TO_CODE.get(cleaned, "")


def _city_value(text: str | None) -> str:
    """A plausible city name typed into a City answer, else ""."""
    cleaned = _clean(text).strip(" .,")
    if not cleaned or len(cleaned) > 40 or is_junk_name(cleaned):
        return ""
    if re.search(r"\d", cleaned) or not re.fullmatch(r"[A-Za-z .'\-]+", cleaned):
        return ""
    if state_code_of(cleaned) or chain_in_text(cleaned)[0]:
        return ""
    return cleaned.title() if cleaned.islower() or cleaned.isupper() else cleaned


# ─── Chains and store numbers ───────────────────────────────────────


@dataclass
class ChainIndex:
    """Chain spellings a tenant's text is read against (the fixed list plus
    the tenant's own retailers and accepted aliases), and the tenant's
    retailer rows a chain resolves to."""

    aliases: dict[str, str] = field(default_factory=dict)
    # chain key -> canonical display name: the fixed list only, and with the
    # tenant's own retailer names added.
    builtin: dict[str, str] = field(default_factory=lambda: dict(_CHAIN_BY_KEY))
    keys: dict[str, str] = field(default_factory=lambda: dict(_CHAIN_BY_KEY))
    retailers: list[tuple[int, str]] = field(default_factory=list)

    @classmethod
    def for_tenant(cls, tenant_id: int | None, retailers: list[tuple[int, str]] | None = None):
        idx = cls(aliases=accepted_alias_map(tenant_id) if tenant_id else {})
        idx.retailers = list(retailers or ())
        for _rid, name in idx.retailers:
            base, _num, _marked = split_store_number(_clean(name))
            key = chain_key(base)
            words = set(_words(base).split())
            if (
                len(key) < 3
                or is_junk_name(base)
                or not (words - _GENERIC_WORDS)
                or state_code_of(base)
                or key in idx.keys
            ):
                continue
            idx.keys[key] = base
        return idx

    def canonical(self, name: str | None) -> str:
        """The canonical chain for a written store name, or "" when the
        name is not a chain this tenant or the fixed list knows."""
        cleaned = _clean(name)
        if not cleaned:
            return ""
        resolved = resolve_store_name(cleaned, self.aliases)
        base, _num, _marked = split_store_number(resolved)
        for candidate in (resolved, base):
            hit = self.keys.get(chain_key(candidate))
            if hit:
                return _CHAIN_BY_KEY.get(chain_key(hit), hit)
        return ""

    def target(self, chain: str, number: str = "") -> tuple[int | None, str]:
        """The tenant retailer row a chain resolves to: the row for that
        numbered store when one exists, else the chain's row (its canonical
        spelling first, then a properly cased one, then the oldest), else
        ``(None, chain)``: a row to create with the canonical name."""
        numbered, plain = [], []
        for rid, name in self.retailers:
            base, num, _marked = split_store_number(_clean(name))
            if self.canonical(base) == chain and num:
                numbered.append((rid, name, num.lstrip("0")))
            elif self.canonical(name) == chain and not num:
                plain.append((rid, name))
        for rid, name, num in numbered:
            if number and num == number:
                return rid, name
        if plain:
            plain.sort(key=lambda r: (r[1] != chain, r[1] == r[1].lower(), r[0]))
            return plain[0]
        return None, chain


def chain_in_text(
    text: str | None, index: ChainIndex | None = None, *, builtin_only: bool = False
) -> tuple[str, str]:
    """``(canonical chain, store number)`` for the first chain named in
    ``text`` ("HEB #614 - Austin" -> ("H-E-B", "614")), else ("", "").
    ``builtin_only`` reads only the fixed chain list (for event names, where
    a tenant retailer called after a town would match the town)."""
    if index is None:
        keys = _CHAIN_BY_KEY
    else:
        keys = index.builtin if builtin_only else index.keys
    for segment in _SEGMENT_SPLIT_RE.split(text or ""):
        tokens = _words(segment).split()
        for i in range(len(tokens)):
            for n in range(min(_MAX_CHAIN_TOKENS, len(tokens) - i), 0, -1):
                hit = keys.get("".join(tokens[i : i + n]))
                if not hit:
                    continue
                canonical = _CHAIN_BY_KEY.get(chain_key(hit), hit)
                return canonical, _number_after(tokens, i + n, segment, text or "")
    return "", ""


def _number_after(tokens: list[str], end: int, raw: str, full: str = "") -> str:
    """A store number right after a chain name ("H-E-B 540", "Kroger Store
    412"), or one marked in the same segment ("#614"). Dates, times and
    street numbers ("Whole Foods 06/21", "HEB 10 - 2pm", "HEB 1234 Main St")
    are not store numbers."""
    from events.market import _STREET_SUFFIXES

    rest = tokens[end : end + 4]
    num = ""
    if rest and rest[0].isdigit():
        num = rest[0]
        if any(t in _STREET_SUFFIXES for t in rest[1:4]):
            num = ""
    elif len(rest) >= 2 and rest[0] in {"store", "no", "num", "number", "str", "unit"} and rest[1].isdigit():
        num = rest[1]
    if num and (
        len(num) > 5
        or re.search(rf"\b{re.escape(num)}\s*(?:[-/:]|to\b)\s*\d", full or raw, re.I)
        or re.search(rf"\b{re.escape(num)}\s*(?:am|pm)\b", full or raw, re.I)
    ):
        num = ""
    if not num:
        marked = {m.group(1) for m in _MARKED_NUMBER_RE.finditer(raw or "")}
        if len(marked) == 1:
            num = marked.pop()
    return num.lstrip("0") or ("0" if num else "")


# ─── What a recap carries ───────────────────────────────────────────


@dataclass
class Snippet:
    source: str
    text: str


@dataclass
class Current:
    retailer: str = ""
    retailer_source: str = ""  # recap | event | request | request_name
    retailer_id: int | None = None
    retailer_ours: bool = False  # recap.retailer set by this module
    store_number: str = ""
    city: str = ""
    state_code: str = ""

    def as_dict(self) -> dict:
        return {
            "retailer": self.retailer,
            "store_number": self.store_number,
            "city": self.city,
            "state_code": self.state_code,
        }


def current_values(recap) -> Current:
    """The location values reports read today, with where the retailer comes
    from (recap first, then event, request and the request's typed name: the
    ``eventRetailer`` resolver's order)."""
    event = recap.event
    request = getattr(event, "request", None) if event else None
    cur = Current(store_number=(recap.store_number or "").strip())
    fill = (recap.location_fill or {}).get("fields") or {}
    if recap.retailer_id:
        cur.retailer, cur.retailer_source = recap.retailer.name or "", "recap"
        cur.retailer_id = recap.retailer_id
        cur.retailer_ours = (fill.get("retailer") or {}).get("retailer_id") == recap.retailer_id
    elif event is not None and event.retailer_id:
        cur.retailer, cur.retailer_source = event.retailer.name or "", "event"
        cur.retailer_id = event.retailer_id
    elif request is not None and request.retailer_id:
        cur.retailer, cur.retailer_source = request.retailer.name or "", "request"
        cur.retailer_id = request.retailer_id
    elif request is not None and _clean(request.retailer_name):
        cur.retailer, cur.retailer_source = _clean(request.retailer_name), "request_name"

    location = recap.location or (event.location if event is not None else None)
    cur.city = _clean(getattr(location, "name", ""))
    for state in (
        recap.state,
        getattr(event, "state", None),
        getattr(getattr(event, "location", None), "state", None),
    ):
        code = (getattr(state, "code", "") or "").strip().upper()
        if code:
            cur.state_code = code
            break
    return cur


def _ambassador_names(recap) -> list[str]:
    names = [recap.external_ba_name or ""]
    amb = recap.ambassador
    user = getattr(amb, "user", None) if amb is not None else None
    if user is not None:
        names += [user.first_name or "", user.last_name or "", user.username or ""]
    if amb is not None:
        names.append(getattr(amb, "name", "") or "")
    return [n for n in names if n]


def gather_snippets(recap) -> list[Snippet]:
    """Location text about one recap, most trustworthy first. People data
    (ambassador, manager, contact, consumer answers) is never collected."""
    out: list[Snippet] = []

    def add(source, text):
        text = _clean(text)
        if text and len(text) <= 200 and not any(s.source == source and s.text == text for s in out):
            out.append(Snippet(source, text))

    for cfv in recap.custom_field_value.all():
        label = _clean(getattr(cfv.custom_field, "name", ""))
        value = _clean(cfv.value)
        if not label or not value or value[0] in "[{" or len(value) > 160:
            continue
        if _PII_FIELD_RE.search(label):
            continue
        if _STORE_NUMBER_FIELD_RE.search(label):
            add("store_number_field", value)
        elif _ADDRESS_FIELD_RE.search(label):
            add("address_field", value)
        elif _CITY_FIELD_RE.search(label):
            add("city_field", value)
        elif _STATE_FIELD_RE.search(label) and not re.search(r"statement|status", label, re.I):
            add("state_field", value)
        elif _STORE_FIELD_RE.search(label) and not _NOT_STORE_FIELD_RE.search(label):
            if len(re.findall(r"[A-Za-z]", value)) >= 2:
                add("store_field", value)

    add("typed_store", recap.typed_store_name)
    add("typed_address", recap.typed_store_address)
    event = recap.event
    request = getattr(event, "request", None) if event else None
    if event is not None:
        add("event_address", event.address)
        add("event_name", event.name)
    if request is not None:
        add("request_address", request.address)
        add("request_address", request.retailer_address)
        add("request_name", request.name)
    return out


# ─── Clock-in GPS ───────────────────────────────────────────────────


@dataclass
class ClockinFix:
    lat: float
    lng: float
    accuracy_m: float | None = None
    source: str = "attendance"  # attendance | ping


@dataclass
class ClockinGps:
    """The clock-in fixes of a recap's BA on its event, and the event's own
    position (``Event.coordinates``, else its geocoded map point)."""

    fixes: list[ClockinFix] = field(default_factory=list)
    event_point: tuple[float, float] | None = None


def _coords(raw) -> tuple[float, float] | None:
    if not raw or len(raw) < 2:
        return None
    try:
        lat, lng = float(raw[0]), float(raw[1])
    except (TypeError, ValueError):
        return None
    if (lat == 0.0 and lng == 0.0) or not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
        return None
    return lat, lng


def _miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lng1, lat2, lng2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def clockin_gps_for(recaps) -> dict[int, ClockinGps]:
    """``{recap id: ClockinGps}`` for recaps with both an event and an
    ambassador, in three queries however many recaps there are."""
    from ambassadors.models import Attendance, LocationPing
    from events.models import EventMapPoint

    pairs = {(r.event_id, r.ambassador_id) for r in recaps if r.event_id and r.ambassador_id}
    if not pairs:
        return {}
    event_ids = sorted({e for e, _a in pairs})
    amb_ids = sorted({a for _e, a in pairs})
    fixes: dict[tuple[int, int], list[ClockinFix]] = {}
    rows = (
        Attendance.objects.filter(
            event_id__in=event_ids, ambassador_id__in=amb_ids, source__name="clock_in"
        )
        .exclude(coordinates__isnull=True)
        .order_by("clock_time", "id")
        .values_list("event_id", "ambassador_id", "coordinates")
    )
    for ev, amb, raw in rows.iterator():
        point = _coords(raw)
        if point and (ev, amb) in pairs:
            fixes.setdefault((ev, amb), []).append(ClockinFix(*point, source="attendance"))
    pings = (
        LocationPing.objects.filter(event_id__in=event_ids, ambassador_id__in=amb_ids, source="clock_in")
        .order_by("recorded_at", "id")
        .values_list("event_id", "ambassador_id", "lat", "lng", "accuracy_meters")
    )
    for ev, amb, lat, lng, acc in pings.iterator():
        point = _coords((lat, lng))
        if point and (ev, amb) in pairs:
            fixes.setdefault((ev, amb), []).append(ClockinFix(*point, accuracy_m=acc, source="ping"))
    map_points = {
        ev: (lat, lng)
        for ev, lat, lng in EventMapPoint.objects.filter(
            event_id__in=event_ids, status=EventMapPoint.STATUS_OK
        ).values_list("event_id", "lat", "lng")
        if _coords((lat, lng))
    }
    out = {}
    for r in recaps:
        if not (r.event_id and r.ambassador_id):
            continue
        event_point = _coords(getattr(r.event, "coordinates", None)) or map_points.get(r.event_id)
        out[r.id] = ClockinGps(fixes.get((r.event_id, r.ambassador_id), []), event_point)
    return out


def gps_state(gps: ClockinGps | None) -> tuple[str, str, list[str]]:
    """``(state code, confidence, notes)`` from a recap's clock-in fixes;
    ``("", "", notes)`` when they do not settle it."""
    from recaps.us_state_geo import state_at

    notes: list[str] = []
    if gps is None or not gps.fixes:
        return "", "", notes
    poor = [f for f in gps.fixes if f.accuracy_m is not None and f.accuracy_m > GPS_MAX_ACCURACY_M]
    usable = []
    for f in gps.fixes:
        if f in poor:
            continue
        # The web/mobile clock-in writes the same fix to Attendance (no
        # accuracy) and to a ping (with it): a fix next to a poor ping is
        # that same poor fix.
        if f.accuracy_m is None and any(_miles((f.lat, f.lng), (p.lat, p.lng)) < 0.06 for p in poor):
            continue
        usable.append(f)
    if poor and not usable:
        notes.append(f"state_code: clock-in GPS accuracy worse than {GPS_MAX_ACCURACY_M:.0f} m; not used")
        return "", "", notes
    if gps.event_point is not None:
        near = [f for f in usable if _miles((f.lat, f.lng), gps.event_point) <= GPS_MAX_FROM_EVENT_MILES]
        if usable and not near:
            notes.append(
                f"state_code: clock-in GPS is more than {GPS_MAX_FROM_EVENT_MILES:.0f} mi "
                "from the event; not used"
            )
            return "", "", notes
        usable = near
    codes: set[str] = set()
    confidence = CONF_HIGH
    for f in usable:
        hit = state_at(f.lat, f.lng)
        if hit is None:
            continue
        unsure_km = GPS_BORDER_UNSURE_KM + (f.accuracy_m or 0) / 1000.0
        if hit.border_km < unsure_km:
            notes.append(f"state_code: clock-in GPS is too close to a state line to call ({hit.code}?); not used")
            return "", "", notes
        if not hit.inside or hit.border_km < GPS_NEAR_BORDER_KM:
            confidence = CONF_MEDIUM
        codes.add(hit.code)
    if len(codes) > 1:
        notes.append(f"state_code: clock-in GPS fixes fall in different states ({', '.join(sorted(codes))}); not used")
        return "", "", notes
    if not codes:
        return "", "", notes
    return codes.pop(), confidence, notes


def _catalog_states(city: str) -> set[str]:
    from events.models import Location

    return {
        (code or "").strip().upper()
        for code in Location.objects.filter(name__iexact=city, state__isnull=False).values_list(
            "state__code", flat=True
        )
        if code
    }


# ─── Proposals ──────────────────────────────────────────────────────


@dataclass
class Proposal:
    recap_id: int
    recap_uuid: str
    event_date: str
    before: dict
    after: dict
    sources: dict = field(default_factory=dict)
    confidence: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    # The canonical chain found, the tenant retailer row it resolves to
    # (None: to be created) and what the recap shows today.
    chain: str = ""
    target_retailer_id: int | None = None
    cur_retailer_id: int | None = None
    cur_retailer_hand_set: bool = False
    # Write the retailer even though one shows (same chain, other spelling).
    normalize_retailer: bool = False
    zip: str = ""

    @property
    def changes(self) -> dict:
        """``{field: new value}`` this proposal would write."""
        out = {}
        for f in FIELDS:
            new = self.after.get(f) or ""
            if not new:
                continue
            if f == "retailer":
                if new == self.before.get("retailer"):
                    continue
                if self.before.get("retailer") and not self.normalize_retailer:
                    continue
            elif self.before.get(f):
                continue
            out[f] = new
        return out

    def as_dict(self) -> dict:
        return {
            "recap_id": self.recap_id,
            "recap_uuid": self.recap_uuid,
            "event_date": self.event_date,
            "before": self.before,
            "after": {**self.before, **self.changes},
            "changes": self.changes,
            "sources": {f: self.sources[f] for f in self.changes if f in self.sources},
            "confidence": {f: self.confidence[f] for f in self.changes if f in self.confidence},
            "notes": self.notes,
        }


def _pick(values: list[tuple[str, str]], field_name: str, notes: list[str]) -> tuple[str, str]:
    """The one value every source agrees on, with its first source. Sources
    that disagree leave it blank (and say so)."""
    distinct = {v.casefold(): (v, s) for v, s in reversed(values) if v}
    if len(distinct) > 1:
        notes.append(
            f"{field_name}: sources disagree ("
            + ", ".join(f"{v} from {s}" for v, s in distinct.values())
            + "); left blank"
        )
        return "", ""
    return next(iter(distinct.values())) if distinct else ("", "")


_LOOKUP = object()


def propose(recap, index: ChainIndex, gps: ClockinGps | None = _LOOKUP) -> Proposal:  # type: ignore[assignment]
    """Deterministic proposal for one recap (no network). ``gps``: the
    recap's clock-in fixes from :func:`clockin_gps_for` (looked up here when
    not given)."""
    cur = current_values(recap)
    snippets = gather_snippets(recap)
    when = None
    event = recap.event
    if event is not None:
        when = event.date or event.start_time
    prop = Proposal(
        recap_id=recap.id,
        recap_uuid=str(recap.uuid),
        event_date=when.date().isoformat() if hasattr(when, "date") else "",
        before=cur.as_dict(),
        after=cur.as_dict(),
    )
    notes = prop.notes

    # Retailer: the chain the recap already shows, or one named in its text.
    chain, chain_number = "", ""
    prop.cur_retailer_id = cur.retailer_id
    prop.cur_retailer_hand_set = cur.retailer_source == "recap" and not cur.retailer_ours
    if cur.retailer:
        chain = index.canonical(cur.retailer)
        if chain:
            base, num, marked = split_store_number(resolve_store_name(cur.retailer, index.aliases))
            if num and (marked or index.canonical(base)):
                chain_number = num
            prop.chain = chain
            prop.sources["retailer"] = f"{cur.retailer_source}_retailer"
            prop.confidence["retailer"] = CONF_HIGH
    else:
        strong = [
            (chain_in_text(s.text, index)[0], s.source)
            for s in snippets
            if s.source in ("store_field", "typed_store")
        ]
        chain, src = _pick([c for c in strong if c[0]], "retailer", notes)
        conf = CONF_HIGH
        if not chain and not any(c[0] for c in strong):
            weak = [
                (chain_in_text(s.text, index, builtin_only=True)[0], s.source)
                for s in snippets
                if s.source in ("event_name", "request_name")
            ]
            chain, src = _pick([c for c in weak if c[0]], "retailer", notes)
            conf = CONF_MEDIUM
        if chain:
            prop.chain = chain
            prop.sources["retailer"] = src
            prop.confidence["retailer"] = conf
        else:
            unknown = [s.text for s in snippets if s.source in ("store_field", "typed_store")]
            if unknown and not any(n.startswith("retailer:") for n in notes):
                notes.append(f"retailer: store written as {unknown[0]!r}, not a known chain")

    # Store number.
    if not cur.store_number and chain:
        numbers: list[tuple[str, str]] = []
        if chain_number:
            numbers.append((chain_number, prop.sources.get("retailer", "retailer")))
        for s in snippets:
            if s.source == "store_number_field":
                digits = re.sub(r"\D", "", s.text)
                if 1 <= len(digits) <= 6:
                    numbers.append((digits.lstrip("0") or "0", s.source))
            elif s.source in ("store_field", "typed_store", "event_name", "request_name"):
                found_chain, num = chain_in_text(
                    s.text, index, builtin_only=s.source in ("event_name", "request_name")
                )
                if num and found_chain == chain:
                    numbers.append((num, s.source))
        value, src = _pick(numbers, "store_number", notes)
        if value:
            prop.after["store_number"] = value
            prop.sources["store_number"] = src
            prop.confidence["store_number"] = CONF_HIGH

    # State and city, from addresses and the recap's own answers.
    states: list[tuple[str, str]] = []
    cities: list[tuple[str, str]] = []
    zips: list[str] = []
    for s in snippets:
        if s.source in ("event_address", "request_address", "typed_address", "address_field"):
            geo = parse_address_geo(s.text)
            if geo.state_code:
                states.append((geo.state_code, s.source))
                if geo.city:
                    cities.append((geo.city, s.source))
                if geo.zip:
                    zips.append(geo.zip)
        elif s.source == "state_field":
            code = state_code_of(s.text)
            if code:
                states.append((code, s.source))
        elif s.source == "city_field":
            geo = parse_address_geo(s.text) if "," in s.text else None
            if geo is not None and geo.state_code:
                states.append((geo.state_code, s.source))
                if geo.city:
                    cities.append((geo.city, s.source))
            else:
                city = _city_value(s.text)
                if city:
                    cities.append((city, s.source))

    if not cur.state_code:
        # After the addresses and answers, so one of them is the source
        # whenever they agree with the GPS; a disagreement blanks the state.
        if gps is _LOOKUP:
            gps = clockin_gps_for([recap]).get(recap.id)
        gps_code, gps_conf, gps_notes = gps_state(gps)
        notes.extend(gps_notes)
        if gps_code:
            states.append((gps_code, GPS_SOURCE))
        value, src = _pick(states, "state_code", notes)
        if value:
            prop.after["state_code"] = value
            prop.sources["state_code"] = src
            prop.confidence["state_code"] = gps_conf if src == GPS_SOURCE else CONF_HIGH
    state = prop.after.get("state_code") or ""
    if not cur.city:
        value, src = _pick(cities, "city", notes)
        city_conf = CONF_HIGH
        if value and state and prop.sources.get("state_code") == GPS_SOURCE:
            # The city is the recap's own answer; the state only the GPS.
            city_conf = CONF_MEDIUM
            known = _catalog_states(value)
            if known and state not in known:
                notes.append(
                    f"city: {value!r} is listed only in {', '.join(sorted(known))}, "
                    f"not the clock-in state {state}; left blank"
                )
                value = ""
        if value and state:
            prop.after["city"] = value
            prop.sources["city"] = src
            prop.confidence["city"] = city_conf
        elif value:
            notes.append(f"city: {value!r} found but no state to place it in; left blank")
    prop.zip = zips[0] if len(set(zips)) == 1 else ""
    finalize_retailer(prop, index)
    return prop


def finalize_retailer(prop: Proposal, index: ChainIndex) -> None:
    """Point the proposal at the tenant retailer row for its chain (and store
    number), and decide whether the recap's retailer should change: when it
    has none, or when it shows the same chain spelled another way and was not
    set on the recap by hand."""
    prop.normalize_retailer = False
    before = prop.before.get("retailer") or ""
    if not prop.chain:
        prop.after["retailer"] = before
        return
    rid, name = index.target(prop.chain, prop.after.get("store_number") or "")
    prop.target_retailer_id = rid
    if not before:
        prop.after["retailer"] = name
        return
    prop.after["retailer"] = before
    if rid is not None and rid == prop.cur_retailer_id:
        return
    if name == before:
        return
    if prop.cur_retailer_hand_set:
        note = f"retailer: {before!r} was set on the recap by hand; kept (would be {name!r})"
        if note not in prop.notes:
            prop.notes.append(note)
        return
    prop.after["retailer"] = name
    prop.normalize_retailer = True


def _needs_ai(prop: Proposal) -> bool:
    if any("disagree" in n for n in prop.notes):
        return False
    return not prop.after.get("retailer") or not prop.after.get("state_code") or not prop.after.get("city")


# ─── The model, for what is still blank ─────────────────────────────

_AI_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["recaps"],
    "properties": {
        "recaps": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "retailer", "store_number", "city", "state_code", "confidence"],
                "properties": {
                    "id": {"type": "integer"},
                    "retailer": {"type": ["string", "null"]},
                    "store_number": {"type": ["string", "null"]},
                    "city": {"type": ["string", "null"]},
                    "state_code": {"type": ["string", "null"]},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
            },
        }
    },
}

_AI_SYSTEM = (
    "You read short notes about where in-store product demos happened and say which "
    "retail chain, store number, city and US state each one was at. Use ONLY the words "
    "in the notes. retailer is the chain's banner name (\"Kroger\", not \"Kroger #412 "
    "Houston\"); store_number is digits written next to the chain name, else null; city "
    "must be a city written in the notes, else null; state_code is the 2-letter US code "
    "of a state written in the notes, or of the written city when that city is "
    "unambiguous, else null. Never guess: when unsure, return null. A wrong state is "
    "worse than none. confidence is low when the notes barely support the answer."
)


def ai_texts(recap, snippets: list[Snippet]) -> list[str]:
    """The location text the model may see for one recap, scrubbed."""
    names = _ambassador_names(recap)
    keep = (
        "store_field", "typed_store", "store_number_field", "city_field", "state_field",
        "address_field", "typed_address", "event_name", "event_address",
        "request_name", "request_address",
    )
    return [scrub(s.text, names)[:200] for s in snippets if s.source in keep]


def ask_ai(batch: list[tuple[int, list[str]]]) -> dict[int, dict] | None:
    """``{recap id: answer}`` for a batch of ``(id, texts)``. None when AI is
    not configured or the call fails."""
    from utils.ai_text import AiUnavailable, generate_json

    if not batch:
        return {}
    user = "\n\n".join(
        f"Recap {rid}:\n" + "\n".join(f"- {t}" for t in texts) for rid, texts in batch
    )
    try:
        out = generate_json(
            _AI_SYSTEM, user, schema=_AI_SCHEMA, max_tokens=2000, small=True, timeout=45
        )
    except AiUnavailable:
        return None
    except Exception:  # noqa: BLE001
        logger.info("recap location: AI call failed", exc_info=True)
        return None
    if not isinstance(out, dict) or not isinstance(out.get("recaps"), list):
        return None
    answers: dict[int, dict] = {}
    for item in out["recaps"]:
        if isinstance(item, dict) and isinstance(item.get("id"), int):
            answers[item["id"]] = item
    return answers


def _in_text(word: str, texts: list[str]) -> bool:
    pattern = rf"(?<![A-Za-z0-9]){re.escape(word)}(?![A-Za-z0-9])"
    return any(re.search(pattern, t, re.I) for t in texts)


def apply_ai_answer(prop: Proposal, answer: dict, texts: list[str], index: ChainIndex,
                    city_states: dict[str, set[str]]) -> None:
    """Keep only what the text the model saw supports."""
    conf = answer.get("confidence")
    if conf not in ("high", "medium"):
        return
    if not prop.after.get("retailer"):
        chain = index.canonical(answer.get("retailer"))
        if chain:
            prop.chain = chain
            prop.sources["retailer"] = "ai"
            prop.confidence["retailer"] = CONF_MEDIUM
    number = re.sub(r"\D", "", str(answer.get("store_number") or ""))
    if prop.chain and number and not prop.after.get("store_number") and _in_text(number, texts):
        prop.after["store_number"] = number.lstrip("0") or "0"
        prop.sources["store_number"] = "ai"
        prop.confidence["store_number"] = CONF_MEDIUM

    city = _clean(answer.get("city"))
    city_ok = bool(city) and _in_text(city, texts) and bool(_city_value(city))
    state = prop.after.get("state_code") or ""
    if not state:
        code = state_code_of(answer.get("state_code"))
        if code:
            name = next((n for n, c in US_STATE_NAME_TO_CODE.items() if c == code), "")
            written = any(re.search(rf"(?<![A-Za-z]){code}(?![A-Za-z])", t) for t in texts) or (
                name and _in_text(name, texts)
            )
            # Only the city named: the catalog must place it in one state,
            # the same one, and the model must be sure.
            by_city = city_ok and conf == "high" and city_states.get(city.casefold()) == {code}
            if written or by_city:
                state = code
                prop.after["state_code"] = code
                prop.sources["state_code"] = "ai"
                prop.confidence["state_code"] = CONF_MEDIUM
    if not prop.after.get("city") and city_ok and state:
        prop.after["city"] = _city_value(city)
        prop.sources["city"] = "ai"
        prop.confidence["city"] = CONF_MEDIUM
    finalize_retailer(prop, index)


def _city_states(cities: set[str]) -> dict[str, set[str]]:
    """``{city: {state codes}}`` from the Location catalog, for corroboration."""
    from events.models import Location

    if not cities:
        return {}
    out: dict[str, set[str]] = {}
    rows = Location.objects.filter(state__isnull=False).values_list("name", "state__code")
    wanted = {c.casefold() for c in cities}
    for name, code in rows.iterator():
        key = (name or "").strip().casefold()
        if key in wanted and code:
            out.setdefault(key, set()).add(code.strip().upper())
    return out


# ─── Writing ────────────────────────────────────────────────────────


@dataclass
class _Writer:
    tenant_id: int
    actor: object
    states: dict[str, int] = field(default_factory=dict)
    _made: dict[str, int] = field(default_factory=dict)
    created_retailers: list[str] = field(default_factory=list)
    created_locations: list[str] = field(default_factory=list)

    def retailer_id(self, prop: Proposal) -> int | None:
        """The proposal's retailer row, creating it (once per run) with the
        canonical chain name when the tenant has no row of that chain."""
        from events.models import Retailer

        if prop.target_retailer_id:
            return prop.target_retailer_id
        name = (prop.after.get("retailer") or "").strip()
        if not name:
            return None
        if name.casefold() not in self._made:
            row = (
                Retailer.objects.filter(tenant_id=self.tenant_id, name__iexact=name)
                .order_by("id")
                .first()
            )
            if row is None:
                row = Retailer.objects.create(
                    tenant_id=self.tenant_id, name=name[:255], created_by=self.actor
                )
                self.created_retailers.append(row.name)
            self._made[name.casefold()] = row.id
        return self._made[name.casefold()]

    def location_id(self, city: str, state_code: str, zip_code: str) -> int | None:
        from events.models import Location

        state_id = self.states.get(state_code)
        if not state_id:
            return None
        rows = list(
            Location.objects.filter(name__iexact=city, state_id=state_id)
            .order_by("id")
            .values_list("id", "zip")
        )
        if rows:
            for lid, z in rows:
                if zip_code and z == zip_code:
                    return lid
            return rows[0][0]
        row = Location.objects.create(
            name=city[:50], code=state_code, zip=(zip_code or "")[:10],
            state_id=state_id, created_by=self.actor,
        )
        self.created_locations.append(f"{city}, {state_code}")
        return row.id


def write_proposal(recap, prop: Proposal, writer: _Writer) -> dict:
    """Write one proposal's changes onto its recap. Returns what was written."""
    from recaps.models import CustomRecap

    changes = prop.changes
    if not changes:
        return {}
    updates: dict = {}
    fill = dict(recap.location_fill or {})
    fields = dict(fill.get("fields") or {})
    now = timezone.now()

    def stamp(name, value, **extra):
        fields[name] = {
            "value": value,
            "source": prop.sources.get(name, ""),
            "confidence": prop.confidence.get(name, ""),
            "before": prop.before.get(name, ""),
            "at": now.isoformat(),
            **extra,
        }

    if "retailer" in changes:
        rid = writer.retailer_id(prop)
        if rid and rid != recap.retailer_id:
            updates["retailer_id"] = rid
            stamp("retailer", changes["retailer"], retailer_id=rid)
    if "store_number" in changes:
        updates["store_number"] = changes["store_number"][:16]
        stamp("store_number", changes["store_number"])
    if "state_code" in changes:
        sid = writer.states.get(changes["state_code"])
        if sid:
            updates["state_id"] = sid
            stamp("state_code", changes["state_code"], state_id=sid)
    if "city" in changes:
        state = prop.after.get("state_code") or ""
        lid = writer.location_id(changes["city"], state, prop.zip)
        if lid:
            updates["location_id"] = lid
            stamp("city", changes["city"], location_id=lid)
    if not updates:
        return {}
    fill["v"] = 1
    fill["fields"] = fields
    updates["location_fill"] = fill
    updates["updated_at"] = now
    CustomRecap.objects.filter(pk=recap.pk).update(**updates)
    return {k: v for k, v in updates.items() if k not in ("location_fill", "updated_at")}


# ─── A run ──────────────────────────────────────────────────────────


def resolve_tenant(value):
    from tenants.models import Tenant

    raw = str(value or "").strip()
    if not raw:
        raise ValueError("tenant is required")
    if raw.isdigit():
        tenant = Tenant.objects.filter(pk=int(raw)).first()
    else:
        tenant = Tenant.objects.filter(slug=raw).first() or Tenant.objects.filter(name__iexact=raw).first()
    if tenant is None:
        raise ValueError(f"tenant not found: {raw}")
    return tenant


def recaps_for(tenant_id: int, since: date | None = None, limit: int | None = None):
    from django.db.models import Q
    from django.db.models.functions import Coalesce

    from recaps.models import CustomRecap

    qs = (
        CustomRecap.objects.filter(tenant_id=tenant_id)
        .filter(Q(event__request__isnull=True) | Q(event__request__deleted_at__isnull=True))
        .select_related(
            "retailer", "location", "state", "ambassador__user",
            "event__retailer", "event__location__state", "event__state",
            "event__request__retailer",
        )
        .prefetch_related("custom_field_value__custom_field")
        .order_by("id")
    )
    if since is not None:
        qs = qs.annotate(_day=Coalesce("event__date", "event__start_time")).filter(_day__date__gte=since)
    if limit:
        qs = qs[: int(limit)]
    return qs


def run(
    tenant,
    *,
    since: date | None = None,
    limit: int | None = None,
    apply: bool = False,
    use_ai: bool = True,
    max_ai_calls: int = DEFAULT_MAX_AI_CALLS,
    actor=None,
) -> dict:
    """Propose (and with ``apply`` write) locations for a tenant's recaps.
    Dry run by default. Returns a JSON-safe report."""
    from events.models import Retailer, State

    retailers = list(
        Retailer.objects.filter(tenant_id=tenant.id).exclude(name="").order_by("id").values_list("id", "name")
    )
    index = ChainIndex.for_tenant(tenant.id, retailers)
    recaps = list(recaps_for(tenant.id, since=since, limit=limit))
    gps = clockin_gps_for([r for r in recaps if not current_values(r).state_code])
    proposals: list[tuple[object, Proposal]] = [(r, propose(r, index, gps.get(r.id))) for r in recaps]

    ai_status = "off"
    ai_calls = 0
    if use_ai:
        wanting = []
        for recap, prop in proposals:
            if _needs_ai(prop):
                texts = ai_texts(recap, gather_snippets(recap))
                if texts:
                    wanting.append((recap, prop, texts))
        ai_status = "not needed" if not wanting else "used"
        mentioned = set()
        for _r, _p, texts in wanting:
            for t in texts:
                for part in re.split(r"[,|·•\-–—()]", t):
                    if _city_value(part):
                        mentioned.add(_city_value(part))
        city_states = _city_states(mentioned) if wanting else {}
        for start in range(0, len(wanting), AI_BATCH):
            if ai_calls >= max_ai_calls:
                ai_status = f"stopped after {ai_calls} calls (max_ai_calls)"
                break
            chunk = wanting[start : start + AI_BATCH]
            answers = ask_ai([(r.id, texts) for r, _p, texts in chunk])
            ai_calls += 1
            if answers is None:
                ai_status = "unavailable (no OPENAI_API_KEY or the call failed)"
                break
            for recap, prop, texts in chunk:
                if recap.id in answers:
                    apply_ai_answer(prop, answers[recap.id], texts, index, city_states)

    counts = {f: 0 for f in FIELDS}
    by_source: dict[str, int] = {}
    rows = []
    writer = None
    written = 0
    if apply:
        actor = actor or _system_actor(tenant.id)
        writer = _Writer(
            tenant_id=tenant.id,
            actor=actor,
            states={
                (code or "").strip().upper(): sid
                for sid, code in State.objects.order_by("-id").values_list("id", "code")
                if code
            },
        )
    for recap, prop in proposals:
        changes = prop.changes
        if not changes and not prop.notes:
            continue
        for f in changes:
            counts[f] += 1
            src = prop.sources.get(f, "")
            by_source[src] = by_source.get(src, 0) + 1
        row = prop.as_dict()
        if apply and changes:
            with transaction.atomic():
                did = write_proposal(recap, prop, writer)
            row["written"] = sorted(did)
            written += 1 if did else 0
        rows.append(row)

    with_changes = sum(1 for r in rows if r["changes"])
    return {
        "ok": True,
        "tenant": {"id": tenant.id, "slug": getattr(tenant, "slug", "")},
        "applied": bool(apply),
        "since": since.isoformat() if since else None,
        "limit": limit,
        "scanned": len(recaps),
        "recaps_with_changes": with_changes,
        "recaps_written": written,
        "fills": counts,
        "by_source": dict(sorted(by_source.items())),
        "blank_before": _blank_counts([p.before for _r, p in proposals]),
        "blank_after": _blank_counts([{**p.before, **p.changes} for _r, p in proposals]),
        "ai": {"status": ai_status, "calls": ai_calls},
        "created_retailers": writer.created_retailers if writer else [],
        "created_locations": writer.created_locations if writer else [],
        "rows": rows,
    }


def _blank_counts(values: list[dict]) -> dict:
    return {f: sum(1 for v in values if not v.get(f)) for f in FIELDS}


def _system_actor(tenant_id: int):
    from django.contrib.auth import get_user_model

    from events.models import Retailer

    row = Retailer.objects.filter(tenant_id=tenant_id).order_by("id").select_related("created_by").first()
    if row is not None and row.created_by_id:
        return row.created_by
    User = get_user_model()
    return User.objects.filter(is_superuser=True).order_by("id").first() or User.objects.order_by("id").first()
