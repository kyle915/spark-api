from __future__ import annotations

from typing import List, TYPE_CHECKING, Annotated

import strawberry
import strawberry_django
from strawberry.relay import Node

import datetime
from django.utils import timezone
from asgiref.sync import sync_to_async
from tenants.types import SparkUserType, TenantType
from utils.gcs import extract_blob_name_from_url, public_url

from . import models

if TYPE_CHECKING:
    from recaps.types import CustomRecapTemplate


def _serialize_dt(value, offset_minutes: int = 0):
    """Serialize datetime applying explicit offset (minutes) and no server TZ conversion.

    DEPRECATED for any caller that has access to a TimeZone row — use
    `_serialize_dt_for_tz` instead. This signature only knows about a
    fixed offset and therefore can't get DST right (the canonical case:
    Pacific rows stored with offset -480 under-shift during PDT by 1 hour,
    which is exactly the symptom Kyle reported on Liquid Death edits).
    Kept for `date`-only fields where DST doesn't matter and for any
    legacy resolvers we haven't migrated yet.
    """
    if not value:
        return None
    # Normalize to UTC to remove server TZ influence
    if hasattr(value, "tzinfo") and timezone.is_aware(value):
        value = value.astimezone(datetime.timezone.utc)
    # If naive, assume stored in UTC
    if not hasattr(value, "tzinfo") or not timezone.is_aware(value):
        value = value.replace(tzinfo=datetime.timezone.utc)
    # Apply desired offset (event/request timezone)
    value = value + datetime.timedelta(minutes=offset_minutes)
    return value.replace(tzinfo=None).isoformat()


def _serialize_dt_for_tz(value, tz_row):
    """DST-aware variant — converts a UTC datetime to a naive ISO string
    in the request/event's local timezone. Use this for `start_time` /
    `end_time` resolvers so PDT-vs-PST is computed against the actual
    datetime, not a static `TimeZone.offset` int.
    """
    # Import inside the function to avoid a circular import when
    # utils/tz.py is imported during Django startup before events.types
    # has finished registering its strawberry types.
    from utils.tz import naive_local_iso

    return naive_local_iso(value, tz_row)


def _get_field(instance, name: str):
    """Safely fetch a model field value, bypassing descriptor overrides."""
    try:
        field = instance._meta.get_field(name)
        return field.value_from_object(instance)
    except Exception:
        return None


def _get_offset_minutes_from_instance(instance) -> int:
    """Return timezone offset in minutes without extra queries.

    DEPRECATED in favor of resolving the full TimeZone row and using
    `_serialize_dt_for_tz`. Kept so any callers we haven't migrated
    yet keep working.
    """
    try:
        tz = getattr(instance, "timezone", None)
        return int(tz.offset) if tz and tz.offset is not None else 0
    except Exception:
        return 0


def _get_tz_row_from_instance(instance):
    """Return the TimeZone instance attached to an event/request, or None."""
    try:
        return getattr(instance, "timezone", None)
    except Exception:
        return None


@strawberry_django.type(models.EventType)
class EventType(Node):
    uuid: str
    name: str
    is_default: bool
    slug: str | None
    tenant_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry.type
class EventTypeDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    event_type: EventType | None = None


@strawberry_django.type(models.TimeZone)
class TimeZone(Node):
    uuid: str
    name: str
    code: str
    offset: int
    created_at: str
    updated_at: str


@strawberry.type
class TimeZoneResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    timezone: TimeZone | None = None


@strawberry_django.type(models.EventStatus)
class EventStatus(Node):
    uuid: str
    name: str
    slug: str
    is_default: bool
    tenant_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry.type
class EventStatusDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    event_status: EventStatus | None = None


@strawberry_django.type(models.State)
class State(Node):
    uuid: str
    name: str
    code: str
    created_at: str
    updated_at: str


@strawberry_django.type(models.Location)
class Location(Node):
    uuid: str
    name: str
    code: str
    zip: str
    state: State | None = None
    created_at: str
    updated_at: str


@strawberry.type
class LocationDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    location: Location | None = None


@strawberry_django.type(models.Client)
class Client(Node):
    uuid: str
    name: str
    email: str
    tenant_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry.type
class ClientDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    client: Client | None = None


@strawberry_django.type(models.Distributor)
class Distributor(Node):
    uuid: str
    name: str
    email: str | None
    tenant_id: strawberry.ID
    location: Location | None = None
    state: State | None = None
    created_at: str
    updated_at: str


@strawberry.type
class DistributorDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    distributor: Distributor | None = None


@strawberry_django.type(models.Retailer)
class Retailer(Node):
    uuid: str
    name: str
    address: str | None
    store_contact: str | None
    is_national: bool
    tenant_id: strawberry.ID
    location: Location | None = None
    created_at: str
    updated_at: str


@strawberry.type
class RetailerDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    retailer: Retailer | None = None


@strawberry_django.type(models.ProductType)
class ProductType(Node):
    uuid: str
    name: str
    tenant_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry.type
class ProductTypeDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    product_type: ProductType | None = None


@strawberry_django.type(models.Product)
class Product(Node):
    uuid: str
    name: str
    description: str | None = None
    product_type: ProductType | None = None
    tenant_id: strawberry.ID
    created_at: str
    updated_at: str

    @strawberry.field(name="image")
    def image_url(self) -> str | None:
        """Return the public URL for the product image if one exists.

        Aliased via name= so the resolver method doesn't shadow the
        Django ImageField on `self`.

        Pattern: __dict__ fast path, then a getattr fallback wrapped
        in a broad except. Bare __dict__-only is too strict (breaks
        optimizer-deferred queries); bare getattr triggers FieldFile
        lazy load → refresh_from_db → SynchronousOnlyOperation in
        async resolvers. The try/except gives us the fallback's
        coverage without the crash — if we can't read the column,
        return None.
        """
        field_file = self.__dict__.get("image")
        if field_file is None:
            try:
                field_file = getattr(self, "image", None)
            except Exception:
                return None
        if not field_file:
            return None
        try:
            blob = field_file.name
        except Exception:
            blob = str(field_file)
        blob_name = extract_blob_name_from_url(blob)
        return public_url(blob_name)


@strawberry.type
class ProductDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    product: Product | None = None


@strawberry_django.type(models.RequestType)
class RequestType(Node):
    uuid: str
    name: str
    tenant_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry.type
class RequestTypeDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    request_type: RequestType | None = None


@strawberry_django.type(models.BillingEntity)
class BillingEntity(Node):
    uuid: str
    name: str
    state: State | None = None
    tenant_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry.type
class BillingEntityDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    billing_entity: BillingEntity | None = None


@strawberry_django.type(models.RequestStatus)
class RequestStatus(Node):
    uuid: str
    name: str
    slug: str
    create_event: bool
    is_default: bool
    created_at: str
    updated_at: str


@strawberry.type
class RequestStatusDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    request_status: RequestStatus | None = None


@strawberry.type
class OpenShiftAdminType:
    """Admin-side view of a shift-swap slot — a shift a BA dropped that
    reopened for self-serve claim. ``status`` is "open" (still claimable) or
    "claimed" (another BA grabbed it). Surfaced on the request so RMMs see
    swap activity at a glance."""

    uuid: strawberry.ID
    event_uuid: strawberry.ID
    event_name: str | None
    released_by_name: str | None
    claimed_by_name: str | None
    claimed_at: str | None
    created_at: str | None
    status: str


@strawberry_django.type(models.Request)
class Request(Node):
    uuid: str

    @strawberry.field
    def name(self) -> str:
        return _get_field(self, "name") or ""

    # Date/time fields returned as stored, without server TZ conversion
    @strawberry.field
    def date(self) -> str | None:
        return _serialize_dt(_get_field(self, "date"), offset_minutes=0)

    @strawberry.field
    def start_time(self) -> str | None:
        # Use the DST-aware path so a Pacific request renders 12:00 PM
        # in May (PDT) and 12:00 PM in February (PST) without depending
        # on a static -480/-420 minutes field. See utils/tz.py.
        return _serialize_dt_for_tz(
            _get_field(self, "start_time"), _get_tz_row_from_instance(self)
        )

    @strawberry.field
    def end_time(self) -> str | None:
        return _serialize_dt_for_tz(
            _get_field(self, "end_time"), _get_tz_row_from_instance(self)
        )

    address: str
    decline_reason: str | None = None
    reviewed: bool
    scheduling_status: str | None = None
    store_number: str | None = None
    notes: str | None = None
    coordinates: List[float]
    requestor_email: str | None = None
    client_name: str | None = None
    client_email: str | None = None
    distributor_name: str | None = None
    distributor_email: str | None = None
    retailer_name: str | None = None
    retailer_address: str | None = None
    retailer_store_contact: str | None = None
    store_manager_name: str | None = None
    store_manager_phone: str | None = None
    timezone: TimeZone | None = None
    client: Client | None = None
    billing_entity: BillingEntity | None = None
    distributor: Distributor | None = None
    retailer: Retailer | None = None
    location: Location | None = None
    state: State | None = None

    @strawberry.field
    async def store_managers(self) -> List[RequestStoreManager]:
        cached = getattr(self, "_prefetched_objects_cache", {}).get(
            "requests_stores_manager"
        )
        if cached is not None:
            return list(cached)
        return await sync_to_async(list)(self.requests_stores_manager.all())

    @strawberry.field
    async def products(self) -> List[Product]:
        cached = getattr(self, "_prefetched_objects_cache", {}).get("request_product")
        if cached is not None:
            return [item.product for item in cached if item.product]
        items = await sync_to_async(list)(
            self.request_product.select_related("product").all()
        )
        return [item.product for item in items if item.product]

    @strawberry.field
    async def open_shifts(self) -> List[OpenShiftAdminType]:
        """Shift-swap activity across this request's events — dropped slots
        that reopened for self-serve claim (open or claimed). Reads from the
        event_set__open_shifts prefetch when present (no N+1)."""

        def _name(u):
            if not u:
                return None
            full = (
                (getattr(u, "first_name", "") or "")
                + " "
                + (getattr(u, "last_name", "") or "")
            ).strip()
            return full or getattr(u, "email", None)

        def _collect():
            ev_cache = getattr(self, "_prefetched_objects_cache", {}).get("event_set")
            events = list(ev_cache) if ev_cache is not None else list(
                self.event_set.all()
            )
            out: list[OpenShiftAdminType] = []
            for ev in events:
                os_cache = getattr(ev, "_prefetched_objects_cache", {}).get(
                    "open_shifts"
                )
                rows = (
                    list(os_cache)
                    if os_cache is not None
                    else list(
                        ev.open_shifts.select_related(
                            "released_by", "claimed_by"
                        ).all()
                    )
                )
                for r in rows:
                    out.append(
                        OpenShiftAdminType(
                            uuid=strawberry.ID(str(r.uuid)),
                            event_uuid=strawberry.ID(str(getattr(ev, "uuid", ""))),
                            event_name=getattr(ev, "name", None),
                            released_by_name=_name(getattr(r, "released_by", None)),
                            claimed_by_name=_name(getattr(r, "claimed_by", None)),
                            claimed_at=(
                                r.claimed_at.isoformat()
                                if getattr(r, "claimed_at", None)
                                else None
                            ),
                            created_at=(
                                r.created_at.isoformat()
                                if getattr(r, "created_at", None)
                                else None
                            ),
                            status="claimed" if getattr(r, "claimed_at", None) else "open",
                        )
                    )
            out.sort(key=lambda x: x.created_at or "", reverse=True)
            return out

        return await sync_to_async(_collect)()

    @strawberry.field
    async def event(self) -> Event | None:
        cached = getattr(self, "_prefetched_objects_cache", {}).get("event_set")
        if cached is not None:
            return cached[0] if cached else None
        return await sync_to_async(self.event_set.first)()

    @strawberry.field
    async def events(self) -> List["Event"]:
        """All events for this request, oldest first.

        A request can spawn multiple events when an activation is
        scheduled across multiple days or venues. Front-end uses this
        to render the Field Reports panel — one section per event,
        each with its recap(s).
        """
        cached = getattr(self, "_prefetched_objects_cache", {}).get("event_set")
        if cached is not None:
            return list(cached)
        return await sync_to_async(list)(self.event_set.order_by("start_time"))

    # --- Scalar recap rollups for the Master Tracker list ---
    #
    # The tracker LIST view used to select `events { recaps { id }
    # customRecaps { id } }` per request purely to compute a recap COUNT +
    # find which event holds a recap — thousands of nested id objects over
    # the wire + into the Relay store for a big tenant. These three scalars
    # collapse that to flat numbers, computed from the SAME event_set /
    # event_set__recaps / event_set__custom_recap prefetch caches the detail
    # view already loads (no N+1, prefetch untouched). "Filed" = recap EXISTS
    # (legacy OR custom-template), matching /recaps/missing — counted off the
    # prefetch cache, so it is not approval-filtered.

    # NOTE: logic is inlined per-resolver on purpose. `self` here is the
    # Django Request model instance (strawberry_django passes the model as
    # root), NOT this type — so calling helper METHODS on self throws
    # AttributeError ('Request' object has no attribute ...). Inlining keeps
    # every access a real model attribute/manager.

    @staticmethod
    def _recap_total(ev) -> int:
        """Recaps (legacy + custom) on one event, read from its prefetch
        cache (no query) when present. Module-style staticmethod called as
        Request._recap_total(ev) — never via self."""
        ev_pc = getattr(ev, "_prefetched_objects_cache", {})
        r = ev_pc.get("recaps")
        cr = ev_pc.get("custom_recap")
        rc = len(r) if r is not None else ev.recaps.count()
        crc = len(cr) if cr is not None else ev.custom_recap.count()
        return rc + crc

    @strawberry.field
    async def recaps_filed_count(self) -> int:
        """Total recaps (legacy + custom-template) filed across this
        request's events — powers the Master Tracker RECAP chip count.

        Master Tracker LIST path: reads the `_recaps_filed_count_ann` subquery
        annotation the list queryset adds (one scalar column, zero extra
        queries). DETAIL path (no annotation): falls back to the event_set /
        recaps / custom_recap prefetch caches the detail queryset loads."""
        if hasattr(self, "_recaps_filed_count_ann"):
            return int(self._recaps_filed_count_ann or 0)

        def _count():
            cached = getattr(self, "_prefetched_objects_cache", {}).get("event_set")
            events = list(cached) if cached is not None else list(self.event_set.all())
            return sum(Request._recap_total(ev) for ev in events)

        return await sync_to_async(_count)()

    @strawberry.field
    async def recap_event_uuid(self) -> str | None:
        """UUID of the first event that actually holds a recap, so a "N
        RECAP" row click lands on a page that shows one (the primary
        `event` is the lowest-id event, which may be recap-less).

        LIST: reads the `_recap_event_uuid_ann` subquery annotation — which
        is legitimately NULL when no event holds a recap, so we test attribute
        PRESENCE (hasattr), not truthiness, to decide list-vs-detail."""
        if hasattr(self, "_recap_event_uuid_ann"):
            val = self._recap_event_uuid_ann
            return str(val) if val else None

        def _find():
            cached = getattr(self, "_prefetched_objects_cache", {}).get("event_set")
            events = list(cached) if cached is not None else list(self.event_set.all())
            for ev in events:
                if Request._recap_total(ev) > 0:
                    return str(getattr(ev, "uuid", "")) or None
            return None

        return await sync_to_async(_find)()

    @strawberry.field
    async def events_count(self) -> int:
        """Number of events on this request — lets the tracker tell "no
        events" (none) apart from "events but no recap yet" (due).

        LIST: reads the `_events_count_ann` subquery annotation; DETAIL: counts
        the prefetched event_set."""
        if hasattr(self, "_events_count_ann"):
            return int(self._events_count_ann or 0)

        def _count():
            cached = getattr(self, "_prefetched_objects_cache", {}).get("event_set")
            return len(cached) if cached is not None else self.event_set.count()

        return await sync_to_async(_count)()

    request_type: RequestType | None = None
    status: RequestStatus | None = None
    tenant_id: strawberry.ID
    tenant: TenantType | None = None
    rmm_asigned: SparkUserType | None = None
    created_by: SparkUserType | None = None
    updated_by: SparkUserType | None = None
    approved_by: SparkUserType | None = None
    created_at: str
    updated_at: str

    @strawberry.field
    async def activity_log(self) -> List["RequestActivityLogEntry"]:
        """Audit trail of every meaningful change to this request.

        Newest first. Powers the timeline panel on the front-end
        request detail page so kyle / RMMs can answer "who did what
        when" without going to the DB.
        """
        from .models import RequestActivityLog as _Log

        rows = await sync_to_async(list)(
            _Log.objects.filter(request=self)
            .select_related("actor_user")
            .order_by("-created_at")[:200]
        )
        return [
            RequestActivityLogEntry(
                uuid=str(r.uuid),
                kind=r.kind,
                summary=r.summary or "",
                metadata_json=__import__("json").dumps(r.metadata or {}),
                actor_email=(r.actor_user.email if r.actor_user_id else None),
                actor_name=(
                    " ".join(
                        filter(
                            None,
                            [
                                getattr(r.actor_user, "first_name", None),
                                getattr(r.actor_user, "last_name", None),
                            ],
                        )
                    )
                    if r.actor_user_id
                    else None
                ),
                created_at=r.created_at.isoformat() if r.created_at else "",
            )
            for r in rows
        ]


@strawberry.type
class RequestActivityLogEntry:
    """Single entry in a request's append-only audit trail.

    `metadata_json` is shipped as a string-encoded JSON blob (not a raw
    JSON scalar) so the GraphQL schema stays portable across clients
    without needing a JSON scalar definition. The front-end parses on
    read.
    """

    uuid: str
    kind: str
    summary: str
    metadata_json: str
    actor_email: str | None
    actor_name: str | None
    created_at: str


@strawberry_django.type(models.RequestStoreManager)
class RequestStoreManager(Node):
    uuid: str
    name: str
    phone: str
    request_id: strawberry.ID | None = None
    request: Request | None = None
    tenant_id: strawberry.ID | None = None
    created_at: str
    updated_at: str


@strawberry.type
class RequestStoreManagerDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    request_store_manager: RequestStoreManager | None = None


@strawberry.type
class RequestDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    request: Request | None = None


@strawberry.type
class BulkCloneRequestResponse:
    """Result of `bulkCloneRequest`. `created_count` is the number of
    new requests actually saved; `created_uuids` lets the UI deep-link
    to each one (or open a filtered Master Tracker view).
    """

    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    created_count: int = 0
    created_uuids: list[str] = strawberry.field(default_factory=list)


@strawberry.type
class RequestBatchRowResult:
    row_number: int
    success: bool
    message: str
    request_id: strawberry.ID | None = None
    request_uuid: str | None = None
    # True when the row was a duplicate and intentionally skipped (not
    # created, not a failure).
    skipped: bool = False


@strawberry.type
class RequestBatchImportResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    total_rows: int
    success_count: int
    failed_count: int
    rolled_back: bool
    errors: List[str]
    rows: List[RequestBatchRowResult]
    skipped_count: int = 0


@strawberry.type
class RequestBatchTemplateResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    # Public URL to a GCS-hosted copy. Legacy path — requires the
    # Cloud Run service account to have storage.objects.create on
    # the import-templates prefix. Kept for backward compat.
    file_url: str | None = None
    # Base64-encoded XLSX bytes inlined into the response. Preferred
    # — no GCS round-trip, no IAM dependencies, ~30 kB inline is
    # cheap. Front-end decodes into a Blob and triggers download.
    file_base64: str | None = None
    file_name: str | None = None


@strawberry.type
class RequestListResponse:
    total_pages: int
    requests: List[Request]


@strawberry.type
class EventAmbassador:
    """A single BA assigned to an event, for the roster panel.

    Flattens an AmbassadorEvent row down to what the /event/view
    roster UI needs: who they are (name/email) and whether they've
    confirmed (`is_approved=True`) versus are still just invited.
    """

    uuid: strawberry.ID
    name: str
    email: str
    is_approved: bool
    # Day-before confirmation stamps (ISO 8601, null until set):
    # `confirmation_requested_at` — the T-24h "confirm you're in" push went
    # out; `confirmed_at` — the BA tapped confirm or arrived/clocked in.
    # Roster chips derive from the pair: confirmed ✓ / asked-but-silent ⏳ /
    # not asked yet —.
    confirmation_requested_at: str | None = None
    confirmed_at: str | None = None


@strawberry_django.type(models.Event)
class Event(Node):
    uuid: str
    coordinates: List[float] | None = None
    address: str
    is_national: bool
    notes: str | None = None
    request: Request | None = None
    retailer: Retailer | None = None
    distributor: Distributor | None = None
    location: Location | None = None
    state: State | None = None
    created_at: str
    updated_at: str
    tenant_id: strawberry.ID
    tenant: TenantType | None = None
    event_type: EventType | None = None
    status: EventStatus | None = None
    timezone: TimeZone | None = None
    rmm_asigned: SparkUserType | None = None
    custom_recap_template_id: strawberry.ID
    custom_recap_template: (
        Annotated["CustomRecapTemplate", strawberry.lazy("recaps.types")] | None
    ) = None

    @strawberry.field
    async def tenant_image(self) -> str | None:
        """Return the public URL for the tenant image if one exists."""
        tenant = await sync_to_async(lambda: self.tenant, thread_sensitive=True)()
        if not tenant:
            return None
        image = await sync_to_async(lambda: tenant.image, thread_sensitive=True)()
        if not image:
            return None
        try:
            blob = image.name
        except Exception:
            blob = str(image)
        blob_name = extract_blob_name_from_url(blob)
        return public_url(blob_name)

    @strawberry.field
    def name(self) -> str:
        return _get_field(self, "name") or ""

    @strawberry.field
    def date(self) -> str | None:
        return _serialize_dt(_get_field(self, "date"), offset_minutes=0)

    @strawberry.field
    def start_time(self) -> str | None:
        # DST-aware — see Request.start_time above.
        return _serialize_dt_for_tz(
            _get_field(self, "start_time"), _get_tz_row_from_instance(self)
        )

    @strawberry.field
    def end_time(self) -> str | None:
        return _serialize_dt_for_tz(
            _get_field(self, "end_time"), _get_tz_row_from_instance(self)
        )

    @strawberry.field
    def new_end_time(self) -> str | None:
        return _serialize_dt_for_tz(
            _get_field(self, "new_end_time"), _get_tz_row_from_instance(self)
        )

    @strawberry.field
    async def recaps(
        self,
        info: strawberry.Info,
    ) -> List[Annotated["Recap", strawberry.lazy("recaps.types")]]:
        """All recaps filed against this event, newest first.

        Used by the front-end Field Reports panel on /request/view to
        surface what BAs reported in for the activation. Empty list
        is normal — recap is filed post-event.

        Client-only users (tenant-side, no Ignite escalation) see only
        approved recaps. Drafts / unapproved recaps would otherwise
        leak through here even though /recap/list filters them — a
        client viewing a Request page could read raw BA submissions
        before they're approved.
        """
        # Local import to avoid circular import at module load time
        # (events.types ⇄ recaps.queries pull in each other).
        from recaps.queries import _is_client_only_user

        is_client_only = await _is_client_only_user(info)

        cached = getattr(self, "_prefetched_objects_cache", {}).get("recaps")
        if cached is not None:
            recaps = list(cached)
            if is_client_only:
                recaps = [r for r in recaps if getattr(r, "approved", False)]
            return recaps

        qs = self.recaps.order_by("-created_at")
        if is_client_only:
            qs = qs.filter(approved=True)
        return await sync_to_async(list)(qs)

    @strawberry.field
    async def assigned_ambassadors_count(self) -> int:
        """Total BAs assigned to this event (invited + confirmed).

        Counts every AmbassadorEvent row for the event regardless of
        approval state. Powers the Master Tracker "BA assigned"
        indicator. Reuses the `event_set__ambassadors_events` prefetch
        on the requests resolver (no extra query per row); falls back to
        a single COUNT when the relation wasn't prefetched.
        """
        cached = getattr(self, "_prefetched_objects_cache", {}).get(
            "ambassadors_events"
        )
        if cached is not None:
            return len(cached)
        return await sync_to_async(self.ambassadors_events.count)()

    @strawberry.field
    async def confirmed_ambassadors_count(self) -> int:
        """BAs confirmed (is_approved=True) for this event.

        Subset of assignedAmbassadorsCount. Same prefetch-reuse strategy:
        counts in python off the prefetched list when available, else a
        single filtered COUNT.
        """
        cached = getattr(self, "_prefetched_objects_cache", {}).get(
            "ambassadors_events"
        )
        if cached is not None:
            return sum(1 for ae in cached if ae.is_approved)
        return await sync_to_async(
            self.ambassadors_events.filter(is_approved=True).count
        )()

    @strawberry.field
    async def ambassadors(self) -> List[EventAmbassador]:
        """Full BA roster for this event — powers the /event/view panel.

        Each entry is one AmbassadorEvent flattened to name/email plus
        `is_approved` so the UI can split confirmed BAs from still-invited
        ones. Reuses the `ambassadors_events` prefetch when present (the
        list resolver primes it); otherwise issues one select_related
        query. All ORM access — including walking ambassador__user — is
        done inside a single sync_to_async block so a non-prefetched
        relation can't trip SynchronousOnlyOperation.
        """
        cached = getattr(self, "_prefetched_objects_cache", {}).get(
            "ambassadors_events"
        )

        def _build() -> List[EventAmbassador]:
            if cached is not None:
                rows = list(cached)
            else:
                rows = list(
                    self.ambassadors_events.select_related(
                        "ambassador__user"
                    ).all()
                )
            roster: List[EventAmbassador] = []
            for ae in rows:
                user = getattr(ae.ambassador, "user", None)
                full_name = (user.get_full_name() if user else "") or ""
                email = (user.email if user else "") or ""
                roster.append(
                    EventAmbassador(
                        uuid=str(ae.uuid),
                        name=full_name or email,
                        email=email,
                        is_approved=ae.is_approved,
                        confirmation_requested_at=(
                            ae.confirmation_requested_at.isoformat()
                            if ae.confirmation_requested_at
                            else None
                        ),
                        confirmed_at=(
                            ae.confirmed_at.isoformat()
                            if ae.confirmed_at
                            else None
                        ),
                    )
                )
            return roster

        return await sync_to_async(_build)()

    @strawberry.field
    async def recaps_filed_count(self) -> int:
        """Recaps already filed for this event — legacy AND custom
        families combined (same either-list-counts rule as the Master
        Tracker chip). The Connecteam import picker badges rows that
        already have one so nobody double-imports a PDF onto a finished
        event. Prefetch-aware like the sibling resolvers; the fallback
        is two indexed COUNTs (the picker pages 25 rows)."""
        cached_legacy = getattr(self, "_prefetched_objects_cache", {}).get(
            "recaps"
        )
        cached_custom = getattr(self, "_prefetched_objects_cache", {}).get(
            "custom_recap"
        )

        def _count() -> int:
            legacy = (
                len(cached_legacy)
                if cached_legacy is not None
                else self.recaps.count()
            )
            custom = (
                len(cached_custom)
                if cached_custom is not None
                else self.custom_recap.count()
            )
            return legacy + custom

        return await sync_to_async(_count)()

    @strawberry.field
    async def custom_recaps(
        self,
    ) -> List[Annotated["CustomRecap", strawberry.lazy("recaps.types")]]:
        """Custom-template recaps (per-tenant schemas) tied to this event.

        Same shape as `recaps` but for tenants on the custom recap
        builder (Borjomi, Carbliss, etc.). The Master Tracker chip
        considers an event "recap filed" if EITHER list is non-empty,
        so this needs to be queryable alongside `recaps`.
        """
        cached = getattr(self, "_prefetched_objects_cache", {}).get(
            "custom_recap"
        )
        if cached is not None:
            return list(cached)
        return await sync_to_async(list)(
            self.custom_recap.order_by("-created_at")
        )


@strawberry.type
class EventListResponse:
    total_pages: int
    events: List[Event]


@strawberry.type
class EventDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    event: Event | None = None


@strawberry.type
class EventWithRequestDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    event: Event | None = None
    request: Request | None = None


@strawberry.type
class ApproveRequestResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    request: Request | None = None
    event: Event | None = None


@strawberry.type
class DeclineRequestResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    request: Request | None = None


@strawberry.type
class DeleteRequestResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    deleted_request_uuid: str | None = None


@strawberry.type
class NotifyNoteMentionResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    sent_count: int = 0
    failed_emails: List[str] | None = None
