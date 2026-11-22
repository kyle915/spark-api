"""One page about one physical store, for one brand.

``storeProfile(tenantId, storeKey)``: every visit to the store, what its
approved recaps add up to, what consumers said there, its photos, and a
short summary written from those figures.

What a store is comes from the Account Map (:mod:`events.account_stores`,
through :mod:`recaps.store_places`). The key in the URL is not trusted: the
tenant's own grouping is rebuilt and the key looked up in it, so a key that
belongs to another brand is not found.

Figures are the dashboards' figures (:func:`recaps.explain_facts.recap_facts`):
approved, not archived, request not deleted, by event date. They are the
same for staff and for the brand. Comments come from
:func:`recaps.feedback_themes.gather_comments` (consumer-comment fields
only, never an ambassador's "Notes", with ambassador names, emails and
phone numbers taken out). Photos are the approved recaps' images without
receipts and other paperwork. No cost figure of any kind is read.

Ignite staff also get ``staff``: who worked the store, the recaps still in
review, and the review checks that flagged. For a brand user ``staff`` is
null and a visit never says a recap is in review.

The summary is two or three sentences built here from the totals. The
model may only reword them (:mod:`recaps.explain_text`): every number in
its text must be one it was given, or the built sentences are used. The
text is stored per store and written again only when the figures change.

READ-ONLY.
"""

from __future__ import annotations

import logging
from datetime import date

import strawberry
from asgiref.sync import sync_to_async
from django.db.models import Sum
from django.db.models.functions import Coalesce

from utils.graphql.permissions import StrictIsAuthenticated

logger = logging.getLogger(__name__)

KIND = "store_profile"
MAX_VISITS = 200
MAX_COMMENTS = 12
MAX_PHOTOS = 24
MAX_PRODUCTS = 12
MAX_PEOPLE = 25
# Review checks are run one recap at a time: only the newest few.
MAX_FLAG_RECAPS = 10

VISIT_UPCOMING = "upcoming"
VISIT_APPROVED = "recap_approved"
VISIT_IN_REVIEW = "recap_in_review"
VISIT_NO_RECAP = "no_approved_recap"


@strawberry.type
class StoreVisit:
    event_id: strawberry.ID
    event_uuid: str
    request_uuid: str | None
    title: str
    date: str | None
    activation_type: str
    # upcoming | recap_approved | no_approved_recap, and for staff only
    # recap_in_review.
    status: str
    recap_uuid: str | None
    # "custom" / "legacy", for the recap link.
    recap_kind: str | None
    consumers: int | None
    units_sold: int | None
    conversion: float | None


@strawberry.type
class StoreTotals:
    visits: int
    upcoming: int
    approved_recaps: int
    consumers: int
    units_sold: int
    # Percent, one decimal. Null when no approved recap has a base.
    conversion: float | None
    average_consumers: float | None
    average_units_sold: float | None
    first_visit: str | None
    last_visit: str | None


@strawberry.type
class StoreProduct:
    label: str
    samples: int
    # Only where recaps log sales per product; never split from a total.
    sold: int | None


@strawberry.type
class StoreComment:
    text: str
    # general | decline | question
    kind: str
    recap_uuid: str | None
    recap_kind: str | None


@strawberry.type
class StorePhoto:
    url: str
    category: str
    recap_uuid: str
    event_date: str


@strawberry.type
class StorePerson:
    name: str
    visits: int
    last_date: str | None


@strawberry.type
class StoreFlag:
    recap_uuid: str
    recap_kind: str
    date: str | None
    labels: list[str]


@strawberry.type
class StoreStaffView:
    people: list[StorePerson]
    flags: list[StoreFlag]
    in_review: int


@strawberry.type
class StoreProfile:
    store_key: str
    name: str
    address: str
    city: str
    state: str
    summary: str
    # "model", "template", or "none" when there is no approved recap.
    summary_source: str
    totals: StoreTotals
    visits: list[StoreVisit]
    visit_total: int
    products: list[StoreProduct]
    comments: list[StoreComment]
    photos: list[StorePhoto]
    # Null for anyone who is not Ignite staff.
    staff: StoreStaffView | None
    logistics_notes: str = ""
    logistics_notes_updated_at: str | None = None


@strawberry.type
class StoreLink:
    name: str
    store_key: str


def long_date(day: date) -> str:
    return f"{day:%B} {day.day}, {day.year}"


def _rate(units: int, base: int) -> float | None:
    return round(units / base * 100, 1) if base else None


def _activation(*names) -> str:
    from recaps.recap_types import RECAP_TYPE_RETAIL, recap_type_for_name

    for name in names:
        key = recap_type_for_name(name)
        if key:
            return key
    return RECAP_TYPE_RETAIL


def _store_events(tenant_id: int, places, key: str) -> dict[int, dict]:
    from recaps.store_places import live_events

    events = live_events(tenant_id)
    ids = [pk for pk, store in places.for_events(events).items() if store == key]
    out: dict[int, dict] = {}
    rows = (
        events.filter(id__in=ids)
        .annotate(_day=Coalesce("date", "start_time", "request__date"))
        .values_list(
            "id",
            "uuid",
            "name",
            "_day",
            "request__uuid",
            "event_type__name",
            "custom_recap_template__name",
            "request__request_type__name",
        )
    )
    for pk, uuid, name, day, request_uuid, type_name, template, request_type in rows:
        out[pk] = {
            "uuid": str(uuid),
            "name": (name or "").strip(),
            "day": day.date() if day else None,
            "request_uuid": str(request_uuid) if request_uuid else None,
            "type": _activation(type_name, template, request_type),
        }
    return out


def _store_recaps(event_ids) -> list[dict]:
    """Every non-archived recap on these events, whatever its approval."""
    from recaps.models import CustomRecap, Recap

    out: list[dict] = []
    for kind, model in (("custom", CustomRecap), ("legacy", Recap)):
        rows = model.objects.filter(
            event_id__in=list(event_ids), archived_at__isnull=True
        ).values_list("id", "uuid", "event_id", "approved")
        for pk, uuid, event_id, approved in rows:
            out.append(
                {
                    "kind": kind,
                    "id": pk,
                    "uuid": str(uuid),
                    "event_id": event_id,
                    "approved": bool(approved),
                }
            )
    return out


def _products(custom_ids: list[int], legacy_ids: list[int]) -> list[dict]:
    """Products sampled on these recaps and, where recaps carry a
    "Units sold: <product>" line, how many of each sold."""
    from recaps.models import CustomFieldValue, CustomRecapProductSample, ProductSamples
    from recaps.tenant_overview import _sku_pulse_label
    from recaps.types import _SOLD_SKU_LINE_RE, _parse_recap_int

    products: dict[int, dict] = {}
    for rows in (
        CustomRecapProductSample.objects.filter(custom_recap_id__in=custom_ids),
        ProductSamples.objects.filter(recap_id__in=legacy_ids),
    ):
        grouped = rows.values(
            "product_id", "product__name", "product__product_type__name"
        ).annotate(total=Coalesce(Sum("quantity"), 0))
        for row in grouped:
            qty = int(row["total"] or 0)
            label = _sku_pulse_label(row["product__name"], row["product__product_type__name"])
            if qty <= 0 or not label:
                continue
            held = products.setdefault(
                int(row["product_id"]),
                {
                    "label": label,
                    "name": (row["product__name"] or "").strip(),
                    "samples": 0,
                    "sold": None,
                },
            )
            held["samples"] += qty

    by_name = {p["name"].casefold(): p for p in products.values() if p["name"]}
    if by_name and custom_ids:
        lines = CustomFieldValue.objects.filter(
            custom_recap_id__in=custom_ids,
            custom_field__name__iregex=_SOLD_SKU_LINE_RE.pattern,
        ).values_list("custom_field__name", "value")
        for name, value in lines.iterator():
            label = _SOLD_SKU_LINE_RE.split(name or "", maxsplit=1)[-1]
            product = by_name.get(" ".join(label.split()).casefold())
            parsed = _parse_recap_int(value)
            if product is None or parsed is None:
                continue
            product["sold"] = (product["sold"] or 0) + max(0, parsed)
    out = sorted(products.values(), key=lambda p: (-p["samples"], p["label"].casefold()))
    return out[:MAX_PRODUCTS]


def _photos(tenant_id: int, custom_ids: list[int]) -> list[dict]:
    from recaps.photo_gallery import _base_queryset, _image_url
    from recaps.types import _PAPERWORK_CATEGORY_RE

    if not custom_ids:
        return []
    files = (
        _base_queryset(tenant_id, None, None)
        .filter(custom_recap_id__in=custom_ids)
        .exclude(file_recap_category__name__iregex=_PAPERWORK_CATEGORY_RE.pattern)
        .select_related("file_recap_category", "custom_recap", "custom_recap__event")
        .order_by("-custom_recap__event__date", "-custom_recap_id", "id")[: MAX_PHOTOS * 2]
    )
    out: list[dict] = []
    for f in files:
        url = _image_url(f)
        if not url:
            continue
        event = getattr(f.custom_recap, "event", None)
        when = getattr(event, "date", None) or getattr(event, "start_time", None)
        out.append(
            {
                "url": url,
                "category": getattr(f.file_recap_category, "name", None) or "",
                "recap_uuid": str(f.custom_recap.uuid),
                "event_date": when.date().isoformat() if when else "",
            }
        )
        if len(out) >= MAX_PHOTOS:
            break
    return out


def _full_name(first, last, fallback="") -> str:
    name = " ".join(part for part in ((first or "").strip(), (last or "").strip()) if part)
    return name or (fallback or "").strip()


def _staff_view(events: dict[int, dict], recaps: list[dict]) -> StoreStaffView:
    """Who clocked in or filed a recap at the store, and what review flagged."""
    from ambassadors.models import Ambassador, Attendance
    from recaps.models import CustomRecap, Recap
    from recaps.recap_review_checks import flagged_review_checks

    worked: dict[object, set[int]] = {}
    clocked = (
        Attendance.objects.filter(
            event_id__in=list(events), ambassador__isnull=False, source__name="clock_in"
        )
        .values_list("ambassador_id", "event_id")
        .distinct()
    )
    for ambassador_id, event_id in clocked:
        worked.setdefault(ambassador_id, set()).add(event_id)
    for model in (CustomRecap, Recap):
        filed = model.objects.filter(
            event_id__in=list(events), archived_at__isnull=True
        ).values_list("ambassador_id", "external_ba_name", "event_id")
        for ambassador_id, external, event_id in filed:
            if ambassador_id:
                worked.setdefault(ambassador_id, set()).add(event_id)
            elif (external or "").strip():
                worked.setdefault(external.strip(), set()).add(event_id)

    names = {
        pk: _full_name(first, last, email)
        for pk, first, last, email in Ambassador.objects.filter(
            id__in=[k for k in worked if isinstance(k, int)]
        ).values_list("id", "user__first_name", "user__last_name", "user__email")
    }
    people: list[StorePerson] = []
    for who, event_ids in worked.items():
        name = names.get(who, "") if isinstance(who, int) else str(who)
        days = [events[e]["day"] for e in event_ids if events.get(e, {}).get("day")]
        people.append(
            StorePerson(
                name=name or "(unnamed)",
                visits=len(event_ids),
                last_date=max(days).isoformat() if days else None,
            )
        )
    people.sort(key=lambda p: (-p.visits, p.name.casefold()))

    newest = sorted(
        recaps,
        key=lambda r: (events.get(r["event_id"], {}).get("day") or date.min, r["id"]),
        reverse=True,
    )[:MAX_FLAG_RECAPS]
    flags: list[StoreFlag] = []
    for recap in newest:
        found = flagged_review_checks(recap["id"], is_custom=recap["kind"] == "custom")
        labels = [item["label"] for item in found if item.get("label")]
        if not labels:
            continue
        day = events.get(recap["event_id"], {}).get("day")
        flags.append(
            StoreFlag(
                recap_uuid=recap["uuid"],
                recap_kind=recap["kind"],
                date=day.isoformat() if day else None,
                labels=labels,
            )
        )
    return StoreStaffView(
        people=people[:MAX_PEOPLE],
        flags=flags,
        in_review=sum(1 for r in recaps if not r["approved"]),
    )


def summary_draft(name: str, totals: dict, top_product: str | None):
    """The built summary and the figures behind it, or None when the store
    has no approved recap to summarise."""
    from recaps.explain_text import Draft, n, pct, plural

    recaps = totals["approved_recaps"]
    if recaps <= 0:
        return None
    store = name or "This store"
    first, last = totals["first_recap"], totals["last_recap"]
    if first == last:
        when = f"on {long_date(first)}"
    else:
        when = f"between {long_date(first)} and {long_date(last)}"
    sentences = [f"{store} has {plural(recaps, 'approved recap')} {when}."]
    figures = (
        f"Those recaps recorded {n(totals['consumers'])} consumers sampled "
        f"and {n(totals['units_sold'])} units sold"
    )
    if totals["conversion"] is not None:
        figures += f", a conversion rate of {pct(totals['conversion'])}%"
    sentences.append(figures + ".")
    if top_product:
        sentences.append(f"The product sampled most was {top_product}.")
    facts = [
        f"Store: {store}.",
        f"Approved recaps: {n(recaps)}.",
        f"First approved recap: {long_date(first)}. Latest: {long_date(last)}.",
        f"Consumers sampled: {n(totals['consumers'])}.",
        f"Units sold: {n(totals['units_sold'])}.",
    ]
    if totals["conversion"] is not None:
        facts.append(f"Conversion rate: {pct(totals['conversion'])}%.")
    if top_product:
        facts.append(f"Product sampled most: {top_product}.")
    return Draft(key="summary", template=" ".join(sentences), facts="\n".join(facts))


def build_store_profile(
    tenant_id: int, raw_key, *, staff: bool, today: date | None = None
) -> StoreProfile | None:
    from events.live_board import _ops_today
    from recaps import explain_text
    from recaps.explain_facts import recap_facts
    from recaps.feedback_themes import gather_comments
    from recaps.store_places import store_header, tenant_places, valid_key

    key = valid_key(raw_key)
    if key is None:
        from store_logistics.service import (
            get_store_logistics_note,
            resolve_non_request_store,
        )

        non_req = resolve_non_request_store(tenant_id, str(raw_key or ""))
        if non_req is None:
            return None
        logistics_notes, logistics_notes_updated_at = get_store_logistics_note(
            tenant_id,
            store_key=non_req["key"],
            store_name=non_req["name"],
            address=non_req["address"],
            coordinates=non_req["coordinates"],
        )
        return StoreProfile(
            store_key=non_req["key"],
            name=non_req["name"],
            address=non_req["address"],
            city=non_req["city"],
            state=non_req["state"],
            summary="",
            summary_source="none",
            totals=StoreTotals(
                visits=0,
                upcoming=0,
                approved_recaps=0,
                consumers=0,
                units_sold=0,
                conversion=None,
                average_consumers=None,
                average_units_sold=None,
                first_visit=None,
                last_visit=None,
            ),
            visits=[],
            visit_total=0,
            products=[],
            comments=[],
            photos=[],
            staff=StoreStaff(in_review=0, missing_recap=0) if staff else None,
            logistics_notes=logistics_notes,
            logistics_notes_updated_at=logistics_notes_updated_at,
        )
    places = tenant_places(tenant_id)
    if not places.has(key):
        return None
    today = today or _ops_today()
    header = store_header(tenant_id, places, key)
    events = _store_events(tenant_id, places, key)
    recaps = _store_recaps(events)
    by_recap = {(r["kind"], r["id"]): r for r in recaps}

    # The dashboards' rows, kept to this store's events.
    facts = [f for f in recap_facts(tenant_id, None, None) if f["event_id"] in events]
    custom_ids = [f["id"] for f in facts if f["kind"] == "custom"]
    legacy_ids = [f["id"] for f in facts if f["kind"] == "legacy"]

    per_event: dict[int, dict] = {}
    for fact in facts:
        held = per_event.setdefault(
            fact["event_id"],
            {"consumers": None, "units": None, "base": None, "recap": None},
        )
        for name in ("consumers", "units", "base"):
            if fact[name] is not None:
                held[name] = (held[name] or 0) + fact[name]
        if held["recap"] is None:
            held["recap"] = by_recap.get((fact["kind"], fact["id"]))
    in_review = {r["event_id"]: r for r in recaps if not r["approved"]}

    visits: list[tuple] = []
    for pk, event in events.items():
        figures = per_event.get(pk)
        recap = figures["recap"] if figures else None
        if figures:
            status = VISIT_APPROVED
        elif event["day"] and event["day"] > today:
            status = VISIT_UPCOMING
        elif staff and pk in in_review:
            status, recap = VISIT_IN_REVIEW, in_review[pk]
        else:
            status = VISIT_NO_RECAP
        visits.append(
            (
                event["day"] or date.min,
                pk,
                StoreVisit(
                    event_id=strawberry.ID(str(pk)),
                    event_uuid=event["uuid"],
                    request_uuid=event["request_uuid"],
                    title=event["name"] or header["name"] or "Visit",
                    date=event["day"].isoformat() if event["day"] else None,
                    activation_type=event["type"],
                    status=status,
                    recap_uuid=recap["uuid"] if recap else None,
                    recap_kind=recap["kind"] if recap else None,
                    consumers=figures["consumers"] if figures else None,
                    units_sold=figures["units"] if figures else None,
                    conversion=(
                        _rate(figures["units"] or 0, figures["base"] or 0)
                        if figures
                        else None
                    ),
                ),
            )
        )
    visits.sort(key=lambda item: (item[0], item[1]), reverse=True)

    with_consumers = [f["consumers"] for f in facts if f["consumers"] is not None]
    counted = [f for f in facts if f["base"]]
    consumers = sum(with_consumers)
    units = sum(f["units"] or 0 for f in counted)
    base = sum(f["base"] for f in counted)
    past = sorted(e["day"] for e in events.values() if e["day"] and e["day"] <= today)
    recap_days = sorted(f["day"] for f in facts)
    totals = {
        "visits": len(past),
        "upcoming": sum(1 for e in events.values() if e["day"] and e["day"] > today),
        "approved_recaps": len(facts),
        "consumers": consumers,
        "units_sold": units,
        "conversion": _rate(units, base),
        "average_consumers": (
            round(consumers / len(with_consumers), 1) if with_consumers else None
        ),
        "average_units_sold": round(units / len(counted), 1) if counted else None,
        "first_visit": past[0].isoformat() if past else None,
        "last_visit": past[-1].isoformat() if past else None,
        "first_recap": recap_days[0] if recap_days else None,
        "last_recap": recap_days[-1] if recap_days else None,
    }

    products = _products(custom_ids, legacy_ids)
    summary, source = "", "none"
    draft = summary_draft(
        header["name"], totals, products[0]["label"] if products else None
    )
    if draft is not None:
        written = explain_text.write(tenant_id, KIND, key, [draft])[0]
        summary, source = written.text, written.source

    comments: list[StoreComment] = []
    if custom_ids or legacy_ids:
        found, _cut = gather_comments(
            tenant_id, None, None, legacy_ids=legacy_ids, custom_ids=custom_ids
        )
        for comment in found[:MAX_COMMENTS]:
            kind = "custom" if comment.recap.startswith("C") else "legacy"
            recap = by_recap.get((kind, int(comment.recap[1:])))
            comments.append(
                StoreComment(
                    text=comment.text,
                    kind=comment.kind,
                    recap_uuid=recap["uuid"] if recap else None,
                    recap_kind=kind if recap else None,
                )
            )

    from store_logistics.service import get_store_logistics_note

    logistics_notes, logistics_notes_updated_at = get_store_logistics_note(
        tenant_id,
        store_key=key,
        store_name=header["name"],
        address=header["address"],
    )

    return StoreProfile(
        store_key=key,
        name=header["name"],
        address=header["address"],
        city=header["city"],
        state=header["state"],
        summary=summary,
        summary_source=source,
        totals=StoreTotals(
            **{
                k: v
                for k, v in totals.items()
                if k not in ("first_recap", "last_recap")
            }
        ),
        visits=[visit for _day, _pk, visit in visits[:MAX_VISITS]],
        visit_total=len(visits),
        products=[
            StoreProduct(label=p["label"], samples=p["samples"], sold=p["sold"])
            for p in products
        ],
        comments=comments,
        photos=[StorePhoto(**p) for p in _photos(tenant_id, custom_ids)],
        staff=_staff_view(events, recaps) if staff else None,
        logistics_notes=logistics_notes,
        logistics_notes_updated_at=logistics_notes_updated_at,
    )


def recap_store_key(tenant_id: int, raw_recap_id: str, *, staff: bool) -> str | None:
    """The store a recap was filed at, or None when it cannot be told, or
    when the caller may not see the recap."""
    from recaps.scoped_ai import _load_recap
    from recaps.store_places import tenant_places

    recap = _load_recap(tenant_id, str(raw_recap_id or ""), client_safe=not staff)
    if recap is None or not getattr(recap, "event_id", None):
        return None
    return tenant_places(tenant_id).for_event_ids([recap.event_id]).get(recap.event_id)


@strawberry.type
class StoreProfileQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def store_profile(
        self, info: strawberry.Info, tenant_id: strawberry.ID, store_key: str
    ) -> StoreProfile | None:
        """One store of one brand. Null when the key is not a store of the
        brand the caller may read."""
        from recaps.explain_schema import _scope

        target, staff = await _scope(info, tenant_id)
        if target is None:
            return None
        try:
            return await sync_to_async(
                lambda: build_store_profile(target, store_key, staff=staff),
                thread_sensitive=True,
            )()
        except Exception as exc:  # noqa: BLE001
            logger.exception("storeProfile failed tenant=%s: %s", target, exc)
            return None

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def recap_store_key(
        self, info: strawberry.Info, tenant_id: strawberry.ID, recap_id: strawberry.ID
    ) -> str | None:
        """The store key for a recap's store name link."""
        from recaps.explain_schema import _scope

        target, staff = await _scope(info, tenant_id)
        if target is None:
            return None
        try:
            return await sync_to_async(
                lambda: recap_store_key(target, str(recap_id), staff=staff),
                thread_sensitive=True,
            )()
        except Exception as exc:  # noqa: BLE001
            logger.exception("recapStoreKey failed tenant=%s: %s", target, exc)
            return None

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def tenant_store_links(
        self, info: strawberry.Info, tenant_id: strawberry.ID
    ) -> list[StoreLink]:
        """Retailer names that are exactly one store, for linking a chart
        row to its store page. A banner with several locations is left out."""
        from recaps.explain_schema import _scope
        from recaps.store_places import named_store_links

        target, _staff = await _scope(info, tenant_id)
        if target is None:
            return []
        try:
            rows = await sync_to_async(
                lambda: named_store_links(target), thread_sensitive=True
            )()
        except Exception as exc:  # noqa: BLE001
            logger.exception("tenantStoreLinks failed tenant=%s: %s", target, exc)
            return []
        return [StoreLink(name=r["name"], store_key=r["store_key"]) for r in rows]
