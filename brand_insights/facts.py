"""One row per approved visit, with the store it was at and whether the
product was there. Everything the detectors use comes from here.

Basis (the dashboard's, via :func:`recaps.explain_facts.recap_facts`):
approved, not archived, request not soft-deleted, event on the dashboard,
windowed by EVENT date. Consumers sampled and the conversion base / units are
the figures the charts and the Conversion tile use.

Store identity
--------------
A custom recap's own retailer and store number (set by staff or by the recap
location normalizer, :mod:`recaps.recap_location`) win over its event's, the
order :func:`recaps.consumer_breakdown.consumer_rows` uses. Accepted
store-name aliases (:mod:`events.retailer_aliases`) apply. Then:

* a name with a store number ("Kroger #412") is that store;
* a banner name with a street address is the store at that address;
* a banner name with only a city is the banner in that city;
* a banner name with nothing else ("Kroger") is not a store, only a chain.
  It still counts towards the chain, never as a store of its own.

No product
----------
Kyle's rule: a visit where the product was not there is left out of every
rate and ranking, and reported on its own. A visit counts as one when

* its shelf photos were read (or corrected by staff) and every one says the
  product is not on the shelf (:func:`recaps.shelf_check.visit_answer`); or
* a yes/no recap question about stock ("Was product on shelf?", "In stock?")
  is answered no; or
* a notes-style answer says so in plain words ("out of stock", "no product",
  "not carried"). Consumer comment fields are never read for this.

READ-ONLY. No ambassador, no free text leaves this module: only the flag.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

# A yes/no question about whether the product was there.
_STOCK_QUESTION_RE = re.compile(
    r"\b(in[\s-]?stock|on[\s-]+(the\s+)?shelf|on[\s-]+hand|stocked|carried|"
    r"product\s+(was\s+)?(available|present|there|at\s+the\s+store))\b",
    re.IGNORECASE,
)
_NO_VALUES = frozenset({"no", "n", "false", "0", "none", "no product"})

# Notes-style answers that are read for plain "no product" wording. A field
# about consumers is never one of them.
_NOTES_FIELD_RE = re.compile(
    r"\b(notes?|issues?|problems?|stock|inventory|availab\w*|shelf|"
    r"anything\s+else|comments?\s+about\s+the\s+(store|event|visit))\b",
    re.IGNORECASE,
)
_CONSUMER_FIELD_RE = re.compile(
    r"\b(consumers?|customers?|shoppers?|people|feedback|quotes?|questions?|"
    r"declin\w*|liked|favou?rite|said)\b",
    re.IGNORECASE,
)
_NO_PRODUCT_TEXT_RE = re.compile(
    r"\b(out\s+of\s+stock|no\s+product(?!\s+(issues?|problems?|concerns?))|"
    r"(product|it)\s+(was\s+)?not\s+(carried|stocked|on\s+(the\s+)?shelf|available|in\s+stock)|"
    r"not\s+carried|store\s+(does\s+not|doesn'?t)\s+carry|"
    r"(product\s+)?(was\s+)?unavailable|discontinued|unapproved)\b",
    re.IGNORECASE,
)


@dataclass
class Visit:
    recap_kind: str
    recap_id: int
    day: date
    # Physical store: key and label; blank when the visit is not at a known store.
    store_key: str
    store_label: str
    # The banner: key and label; blank when the visit names no retailer.
    chain_key: str
    chain_label: str
    state: str
    consumers: int | None
    base: int | None
    units: int | None
    no_product: bool = False
    # Shelf answers for the visit, when read: "yes" / "no" / "unclear" / "".
    price_tag: str = ""
    sources: list[str] = field(default_factory=list)

    @property
    def rated(self) -> bool:
        """In the conversion rate: has a base and the product was there."""
        return bool(self.base) and not self.no_product


def store_identity(
    name: str, place: str, city: str, state: str
) -> tuple[str, str, str, str]:
    """``(store_key, store_label, chain_key, chain_label)``. Pure."""
    from events.retailer_aliases import chain_key, is_junk_name, split_store_number

    cleaned = " ".join((name or "").split())
    if not cleaned or is_junk_name(cleaned):
        return "", "", "", ""
    base, number, marked = split_store_number(cleaned)
    # A bare trailing number is a store number only for a known chain
    # pattern; "Forever 21" keeps its 21.
    if number and not marked and len(number) < 3:
        base, number = cleaned, ""
    banner = base or cleaned
    ckey = chain_key(banner)
    if not ckey:
        return "", "", "", ""
    if number:
        return f"{ckey}#{number}", f"{banner} #{number}", ckey, banner
    if place:
        # Named by city; two of a banner in one city are told apart after
        # all visits are read (:func:`_tell_apart`).
        where = f"{city}, {state}" if city and state else (city or state)
        label = f"{banner} ({where})" if where else banner
        return f"{ckey}@{place}", label, ckey, banner
    if city:
        where = f"{city}, {state}" if state else city
        return f"{ckey}|{city.casefold()}|{state}", f"{banner} ({where})", ckey, banner
    return "", "", ckey, banner


def _answer_says_no_product(name: str, value: str) -> bool:
    """One recap answer read for the no-product flag. Pure."""
    name = name or ""
    text = " ".join((value or "").split())
    if not text:
        return False
    if _STOCK_QUESTION_RE.search(name) and not _CONSUMER_FIELD_RE.search(name):
        return text.casefold().strip(" .!") in _NO_VALUES
    if _NOTES_FIELD_RE.search(name) and not _CONSUMER_FIELD_RE.search(name):
        return bool(_NO_PRODUCT_TEXT_RE.search(text))
    return False


def _answer_flags(recap_ids: list[int]) -> set[int]:
    from recaps.models import CustomFieldValue

    flagged: set[int] = set()
    if not recap_ids:
        return flagged
    rows = CustomFieldValue.objects.filter(custom_recap_id__in=recap_ids).values_list(
        "custom_recap_id", "custom_field__name", "value"
    )
    for recap_id, name, value in rows.iterator():
        if recap_id not in flagged and _answer_says_no_product(name, value):
            flagged.add(recap_id)
    return flagged


def _shelf_answers(recap_ids: list[int]) -> dict[int, dict]:
    """``{recap_id: visit answer}`` for recaps whose shelf photos have an
    answer (read or corrected)."""
    from recaps.models import CustomRecapFile
    from recaps.shelf_check import SHELF_CATEGORY_IREGEX, _image_q, effective_answers, visit_answer

    if not recap_ids:
        return {}
    by_recap: dict[int, list[dict]] = {}
    files = (
        CustomRecapFile.objects.filter(
            custom_recap_id__in=recap_ids,
            file_recap_category__name__iregex=SHELF_CATEGORY_IREGEX,
        )
        .filter(_image_q())
        .select_related("shelf_reading", "shelf_correction")
    )
    for recap_file in files:
        answer = effective_answers(
            getattr(recap_file, "shelf_reading", None),
            getattr(recap_file, "shelf_correction", None),
        )
        if answer is not None:
            by_recap.setdefault(recap_file.custom_recap_id, []).append(answer)
    return {rid: visit_answer(answers) for rid, answers in by_recap.items() if answers}


def gather_visits(tenant_id: int, start: date, end: date) -> list[Visit]:
    """Every approved visit of one brand between ``start`` and ``end``
    (inclusive, by event date)."""
    from events.retailer_aliases import accepted_alias_map, resolve_store_name
    from recaps.consumer_breakdown import _numbered
    from recaps.explain_facts import recap_facts
    from recaps.models import CustomRecap

    rows = recap_facts(tenant_id, start, end)
    custom_ids = [r["id"] for r in rows if r["kind"] == "custom"]
    own = {
        pk: (retailer or "", number or "", state or "")
        for pk, retailer, number, state in CustomRecap.objects.filter(
            id__in=custom_ids
        ).values_list("id", "retailer__name", "store_number", "state__code")
    }
    aliases = accepted_alias_map(tenant_id)
    flags = _answer_flags(custom_ids)
    shelf = _shelf_answers(custom_ids)

    visits: list[Visit] = []
    for r in rows:
        name = r["store"]
        state = r["state"]
        sources: list[str] = []
        no_product = False
        price_tag = ""
        if r["kind"] == "custom":
            retailer, number, recap_state = own.get(r["id"], ("", "", ""))
            if retailer:
                name = resolve_store_name(_numbered(retailer, number), aliases)
            state = state or recap_state.upper()
            if r["id"] in flags:
                no_product = True
                sources.append("answer")
            answer = shelf.get(r["id"])
            if answer is not None:
                price_tag = answer["price_tag_visible"]
                if answer["product_on_shelf"] == "no":
                    no_product = True
                    sources.append("shelf_photo")
        skey, slabel, ckey, clabel = store_identity(name, r["place"], r["city"], state)
        visits.append(
            Visit(
                recap_kind=r["kind"],
                recap_id=r["id"],
                day=r["day"],
                store_key=skey,
                store_label=slabel,
                chain_key=ckey,
                chain_label=clabel,
                state=state,
                consumers=r["consumers"],
                base=r["base"],
                units=r["units"],
                no_product=no_product,
                price_tag=price_tag,
                sources=sources,
            )
        )
    _tell_apart(visits)
    return visits


def _tell_apart(visits: list[Visit]) -> None:
    """Two stores with the same label (two Krogers in one city, each with
    its own address) get the start of their street address added."""
    keys_by_label: dict[str, set[str]] = {}
    for v in visits:
        if v.store_key:
            keys_by_label.setdefault(v.store_label, set()).add(v.store_key)
    for v in visits:
        if len(keys_by_label.get(v.store_label, ())) > 1 and "@" in v.store_key:
            street = " ".join(v.store_key.split("@", 1)[1].split()[:3]).title()
            v.store_label = (
                f"{v.store_label[:-1]}, {street})"
                if v.store_label.endswith(")")
                else f"{v.store_label} ({street})"
            )
