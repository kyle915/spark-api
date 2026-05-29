from __future__ import annotations

import strawberry_django
import strawberry
from strawberry.relay import Node
from typing import List
from strawberry.scalars import JSON

from events import types as event_types
from jobs import types as job_types
from ambassadors import types as ambassador_types
from tenants import types as tenant_types
from . import models
from asgiref.sync import sync_to_async
from utils.gcs import public_url, extract_blob_name_from_url


@strawberry_django.type(models.RecapFile)
class RecapFile(Node):
    uuid: str
    name: str
    approved: bool
    file_type_id: strawberry.ID
    file_recap_category_id: strawberry.ID | None
    created_at: str
    updated_at: str

    file_type: ambassador_types.FileType
    file_recap_category: FileRecapCategory | None

    # We deliberately use a different Python method name (file_url) and
    # alias it back to the GraphQL field name `file` via the strawberry
    # `name=` arg. A resolver literally named `file` would shadow the
    # Django FileField on the instance and force a fresh per-row DB
    # lookup — fatal for the recaps list which serializes 9.4k file
    # rows per request.
    @strawberry.field(name="file")
    async def file_url(self) -> str | None:
        """Return the public URL for the recap file if one exists.

        Two-step (mirrors CustomRecapFile.url_str):
        1. Fast path — __dict__ when the column is loaded.
        2. Slow path — `refresh_from_db(fields=["file"])` wrapped in
           sync_to_async when the column was deferred. Previously the
           bare getattr fallback raised SynchronousOnlyOperation in
           async Django and silently returned None — the bug that
           produced empty <img src> tags on recap pages even though
           the bucket had every blob.
        """
        field_file = self.__dict__.get("file")
        if field_file is None:
            def _reload():
                self.refresh_from_db(fields=["file"])
                return self.__dict__.get("file")
            try:
                field_file = await sync_to_async(
                    _reload, thread_sensitive=True
                )()
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
class RecapFileDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    recap_file: RecapFile | None = None


@strawberry.type
class RecapExportResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    file_url: str | None = None


@strawberry.type
class RecapFileUrlResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    file_url: str | None = None


@strawberry.type
class MissingRecapAmbassadorInfo:
    """Shape of an assigned BA on a recap-missing event row.

    Surfaced to the /recaps/missing UI so the admin can see who was on
    the shift, then either nudge them via push or file the recap on
    their behalf.
    """

    # AmbassadorEvent.uuid — what the nudge mutation takes.
    ambassador_event_uuid: strawberry.ID
    # Ambassador.uuid — passed to /recap/create?event=…&ambassador=…
    # when admin clicks "File for them".
    ambassador_uuid: strawberry.ID
    name: str
    email: str | None = None
    # Whether the BA accepted the invite (is_approved). Pending invites
    # haven't formally said yes; the admin may want to nudge them
    # differently or skip nudging entirely.
    is_approved: bool


@strawberry.type
class MissingRecapEventType:
    """One row in the /recaps/missing report — an event that's already
    over but doesn't have a recap yet.
    """

    event_uuid: strawberry.ID
    # Human-friendly fallback chain for the row label: event name →
    # retailer name → "(shift)". Lets the UI skip the null-check it'd
    # otherwise scatter across the row template.
    event_name: str
    venue: str | None = None
    address: str | None = None
    state_code: str | None = None
    date: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    # Hours since the shift ended (end_time, falling back to start_time).
    # Lets the UI render an "OVERDUE 18h" chip without re-computing
    # local dates from the date/time strings.
    hours_overdue: int | None = None
    # Deep-link target so the row can deep-link to the parent request
    # on the Master Tracker.
    request_uuid: strawberry.ID | None = None
    # All BAs assigned to this shift — admin can nudge any of them.
    assigned_ambassadors: List[MissingRecapAmbassadorInfo] = strawberry.field(
        default_factory=list,
    )


@strawberry.type
class NudgeRecapResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    # Number of devices the push hit (Expo "ok" tickets). 0 means the
    # BA has no registered devices — UI surfaces a clearer hint than
    # a generic "failed".
    devices_notified: int | None = None


@strawberry.type
class ExecutiveSummaryRow:
    """One ranked row in the top-stores / top-BAs lists."""

    label: str
    primary_metric: int
    secondary_metric: str | None = None


@strawberry.type
class ExecutiveSummaryType:
    """GraphQL projection of digest.exec_services.ExecutiveSummary.

    Powers the dashboard "Pace" widget — same numbers the weekly
    email surfaces, exposed live so kyle can refresh and see the
    delta without waiting for Monday morning.
    """

    tenant_id: strawberry.ID
    tenant_name: str
    period_label: str
    recap_count: int
    consumer_reach: int
    samples_distributed: int
    top_stores: List[ExecutiveSummaryRow] = strawberry.field(
        default_factory=list,
    )
    top_bas: List[ExecutiveSummaryRow] = strawberry.field(
        default_factory=list,
    )
    # Same delta logic the email template uses. None means no prior
    # period available (e.g. tenant is brand new).
    recap_count_delta: int | None = None
    consumer_reach_delta: int | None = None
    recap_count_delta_chip: str | None = None
    consumer_reach_delta_chip: str | None = None


@strawberry_django.type(models.ConsumerEngagements)
class ConsumerEngagements(Node):
    uuid: str
    total_consumer: int | None
    first_time_consumers: int | None
    brand_aware_consumers: int | None
    willing_to_purchase_consumers: int | None
    not_willing_consumers: int | None
    created_at: str
    updated_at: str


@strawberry_django.type(models.ProductSamples)
class ProductSamples(Node):
    uuid: str
    product: event_types.Product
    quantity: int
    created_at: str
    updated_at: str


@strawberry_django.type(models.TypeOfGood)
class TypeOfGood(Node):
    uuid: str
    name: str
    created_at: str
    updated_at: str


@strawberry_django.type(models.FileRecapCategory)
class FileRecapCategory(Node):
    uuid: str
    name: str
    created_at: str
    updated_at: str


@strawberry_django.type(models.SalesPerformance)
class SalesPerformance(Node):
    uuid: str
    product: event_types.Product
    type_of_good: TypeOfGood
    price: float
    created_at: str
    updated_at: str


@strawberry_django.type(models.ConsumerFeedback)
class ConsumerFeedback(Node):
    uuid: str
    demographics: str | None
    feedback: str | None
    quotes: str | None
    positive_stories: str | None
    reasons_to_decline: str | None
    created_at: str
    updated_at: str


@strawberry_django.type(models.AccountFeedback)
class AccountFeedback(Node):
    uuid: str
    do_differently_feedback: str | None
    feedback: str | None
    corpo_card: str | None
    was_corpo_card_used: bool
    created_at: str
    updated_at: str


@strawberry_django.type(models.Recap)
class Recap(Node):
    uuid: str
    name: str
    approved: bool
    filling_for_ambassador: bool
    event: event_types.Event
    event_id: strawberry.ID
    ambassador: ambassador_types.Ambassador | None
    external_ba_name: str | None
    job_id: strawberry.ID | None
    job: job_types.Job | None
    retailer_id: strawberry.ID | None
    retailer: event_types.Retailer | None
    location_id: strawberry.ID | None
    location: event_types.Location | None
    state_id: strawberry.ID | None
    state: event_types.State | None
    created_at: str
    updated_at: str

    total_engagements: int | None
    products_sold: int | None
    total_cans_sold: int | None
    total_packs_sold: int | None
    total_earnings: float | None
    account_spend_amount: float | None
    traffic_description: str | None
    competitive_presence: str | None

    # Relationships
    consumer_engagements: List[ConsumerEngagements]
    product_samples: List[ProductSamples]
    sales_performance: List[SalesPerformance]
    consumer_feedback: List[ConsumerFeedback]
    account_feedback: List[AccountFeedback]

    # NOTE: we use models.RecapFile.objects.filter(recap=self) instead
    # of self.recap_files.all() because defining a method named
    # `recap_files` on a strawberry type shadows the Django reverse-
    # accessor of the same name. Inside the resolver body, `self.recap_files`
    # would resolve to the bound method (not the manager), and `.all()`
    # would silently fail — returning [] for every recap in the API
    # despite the DB having the rows. Going through the model manager
    # directly side-steps the name collision.

    @strawberry.field
    async def recap_file(self) -> RecapFile | None:
        """Return first linked recap file for backward compatibility."""
        first = await sync_to_async(
            lambda: models.RecapFile.objects.filter(recap=self)
            .order_by("id")
            .first(),
            thread_sensitive=True,
        )()
        return first

    @strawberry.field
    async def recap_file_id(self) -> strawberry.ID | None:
        """Return id for the first linked recap file."""
        first = await sync_to_async(
            lambda: models.RecapFile.objects.filter(recap=self)
            .order_by("id")
            .first(),
            thread_sensitive=True,
        )()
        return strawberry.ID(str(first.id)) if first else None

    @strawberry.field
    async def recap_files(self) -> List[RecapFile]:
        """Return all recap files linked to this recap. ORM call has
        to be wrapped in sync_to_async because Strawberry runs the
        resolver inside the request's async loop and Django refuses
        synchronous DB I/O there."""
        return await sync_to_async(
            lambda: list(
                models.RecapFile.objects.filter(recap=self).order_by("id")
            ),
            thread_sensitive=True,
        )()

    @strawberry.field
    async def recap_files_count(self) -> int:
        """Count of files linked to this recap. Cheap COUNT(*) instead
        of returning the full array — the recap list card only needs
        the number for the "◉ N FILES" chip, not the per-file metadata."""
        return await sync_to_async(
            lambda: models.RecapFile.objects.filter(recap=self).count(),
            thread_sensitive=True,
        )()

    @strawberry.field
    async def hero_file(self) -> RecapFile | None:
        """First renderable image attached to this recap, if any.

        Used by the /recaps list card to render a single thumbnail
        without round-tripping the full recapFiles array.

        Picking order:
          1. Any JPG/PNG/WEBP/GIF — most browsers render these natively.
          2. Fall back to HEIC/HEIF — the frontend has a libheif
             decoder for these on the detail page, and Safari/iOS
             render them natively. Better to show a real (decoded)
             photo than the empty-diamond placeholder.
          3. Skip PDFs / video / unknown — those can't <img src>.
        """
        WEB_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
        HEIC_EXTS = (".heic", ".heif")
        SKIP_EXTS = (".pdf", ".mp4", ".mov", ".webm", ".doc", ".docx", ".xlsx")

        def _pick():
            qs = models.RecapFile.objects.filter(recap=self).order_by("id")
            heic_fallback = None
            for f in qs:
                path = (getattr(f, "file", None) or "")
                if not path:
                    continue
                try:
                    path = str(path).lower()
                except Exception:
                    continue
                clean = path.split("?", 1)[0]
                if clean.endswith(SKIP_EXTS):
                    continue
                if clean.endswith(WEB_EXTS):
                    return f
                if clean.endswith(HEIC_EXTS) and heic_fallback is None:
                    heic_fallback = f
            return heic_fallback

        return await sync_to_async(_pick, thread_sensitive=True)()

    @strawberry.field(deprecation_reason="Use ambassador instead.")
    def ambassadors(self) -> List[ambassador_types.Ambassador]:
        """Backward-compatible ambassador list wrapper."""
        return [self.ambassador] if self.ambassador else []

    @strawberry.field
    def request_store_managers(self) -> List[event_types.RequestStoreManager]:
        """Return store managers associated with the recap's request."""
        if not self.event or not self.event.request:
            return []
        return list(self.event.request.requests_stores_manager.all())


@strawberry.type
class RecapDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    recap: Recap | None = None


@strawberry.type
class DeleteRecapResponse:
    """Result of deleting a legacy Recap.

    Returns the deleted recap's uuid so the client can drop the row
    from Relay's store / the recaps list without a full refetch.
    Mirrors events.types.DeleteRequestResponse.
    """

    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    deleted_recap_uuid: str | None = None


@strawberry_django.type(models.CustomRecap)
class CustomRecap(Node):
    uuid: str
    name: str
    submitted_at: str | None
    total_engagements: int | None
    filling_for_ambassador: bool
    late: bool
    incomplete: bool
    approved: bool
    used_corpo_card: bool
    timezone_id: strawberry.ID | None

    event_id: strawberry.ID
    event: event_types.Event
    ambassador_id: strawberry.ID | None
    ambassador: ambassador_types.Ambassador | None
    job_id: strawberry.ID | None
    job: job_types.Job | None
    retailer_id: strawberry.ID | None
    retailer: event_types.Retailer | None
    location_id: strawberry.ID | None
    location: event_types.Location | None
    state_id: strawberry.ID | None
    state: event_types.State | None
    custom_recap_template_id: strawberry.ID
    custom_recap_template: "CustomRecapTemplate"
    created_at: str
    updated_at: str
    custom_recap_product_sample: List["CustomRecapProductSample"]
    custom_recap_sale_performance: List["CustomRecapSalePerformance"]
    custom_recap_files: List["CustomRecapFile"]

    @strawberry.field
    def custom_field(self) -> List["CustomField"]:
        """
        Return template fields enriched with the value submitted in this custom recap.
        Value source: CustomFieldValue table.
        """
        value_by_field_id = {
            item.custom_field_id: item for item in self.custom_field_value.all()
        }
        fields: list[CustomField] = []
        for custom_field in self.custom_recap_template.custom_field.all():
            custom_field_value = value_by_field_id.get(custom_field.id)
            setattr(
                custom_field,
                "_custom_recap_value",
                custom_field_value.value if custom_field_value else None,
            )
            setattr(
                custom_field,
                "_custom_field_value_id",
                custom_field_value.id if custom_field_value else None,
            )
            fields.append(custom_field)
        return fields

    # ---- Linked-event location (for the recap "Information" panel) ----
    #
    # The recap card surfaces only the event NAME, and the recap's own
    # retailer/location/state FKs are usually null on custom recaps (they
    # live on the linked Event and its Request instead) — so a client
    # opening the recap sees "Retailer -" with no address/city/state and
    # can't tell where the event actually happened. These resolvers walk
    # the recap → event → (retailer / location / state / request) chain
    # and expose a flat, client-readable location block.
    #
    # Each resolver is `async def` with an inner `@sync_to_async` helper
    # that touches the lazy FK chain: Strawberry runs resolvers in the
    # request's async loop, and on a freshly-fetched recap these FKs are
    # NOT select_related'd, so a bare `self.event.retailer` access raises
    # SynchronousOnlyOperation. This mirrors the FK-access pattern already
    # used by ChatThread.ambassador_name / job_name in chats/types.py and
    # the file resolvers above. All paths are null-safe — a recap with no
    # linked event (or an event with no retailer/location/state/request)
    # returns None rather than raising.

    @strawberry.field
    async def event_date(self) -> str | None:
        """Date of the linked event (the day the activation happened)."""
        if not getattr(self, "event_id", None):
            return None

        @sync_to_async
        def _get():
            ev = getattr(self, "event", None)
            d = getattr(ev, "date", None) if ev else None
            return d.isoformat() if d else None

        return await _get()

    @strawberry.field
    async def event_address(self) -> str | None:
        """Street address where the event happened.

        Preference: the event's own address (most specific to the
        activation) → the linked retailer's address → the parent
        request's retailer address → the request's address.
        """
        if not getattr(self, "event_id", None):
            return None

        @sync_to_async
        def _get():
            ev = getattr(self, "event", None)
            if ev is None:
                return None
            addr = (getattr(ev, "address", None) or "").strip()
            if addr:
                return addr
            retailer = getattr(ev, "retailer", None)
            r_addr = (getattr(retailer, "address", None) or "").strip() if retailer else ""
            if r_addr:
                return r_addr
            req = getattr(ev, "request", None)
            if req is not None:
                req_retailer_addr = (getattr(req, "retailer_address", None) or "").strip()
                if req_retailer_addr:
                    return req_retailer_addr
                req_addr = (getattr(req, "address", None) or "").strip()
                if req_addr:
                    return req_addr
            return None

        return await _get()

    @strawberry.field
    async def event_venue(self) -> str | None:
        """Venue / store name for the event — the physical place a client
        would look for. Pulls the linked retailer's name, falling back to
        the request's retailer name."""
        if not getattr(self, "event_id", None):
            return None

        @sync_to_async
        def _get():
            ev = getattr(self, "event", None)
            if ev is None:
                return None
            retailer = getattr(ev, "retailer", None)
            name = (getattr(retailer, "name", None) or "").strip() if retailer else ""
            if name:
                return name
            req = getattr(ev, "request", None)
            req_name = (getattr(req, "retailer_name", None) or "").strip() if req else ""
            return req_name or None

        return await _get()

    @strawberry.field
    async def event_retailer(self) -> str | None:
        """Retailer name for the linked event.

        Walks the event chain (event.retailer → request.retailer →
        request.retailer_name) so the panel shows a real retailer even
        when the recap's own `retailer` FK is null — which it usually is
        on custom recaps, producing the "Retailer -" the client sees.
        """
        if not getattr(self, "event_id", None):
            return None

        @sync_to_async
        def _get():
            ev = getattr(self, "event", None)
            if ev is None:
                return None
            retailer = getattr(ev, "retailer", None)
            name = (getattr(retailer, "name", None) or "").strip() if retailer else ""
            if name:
                return name
            req = getattr(ev, "request", None)
            if req is not None:
                req_retailer = getattr(req, "retailer", None)
                req_retailer_name = (
                    (getattr(req_retailer, "name", None) or "").strip()
                    if req_retailer
                    else ""
                )
                if req_retailer_name:
                    return req_retailer_name
                req_name = (getattr(req, "retailer_name", None) or "").strip()
                if req_name:
                    return req_name
            return None

        return await _get()

    @strawberry.field
    async def event_city(self) -> str | None:
        """City of the event — the linked Location's name (falling back to
        the retailer's location)."""
        if not getattr(self, "event_id", None):
            return None

        @sync_to_async
        def _get():
            ev = getattr(self, "event", None)
            if ev is None:
                return None
            loc = getattr(ev, "location", None)
            name = (getattr(loc, "name", None) or "").strip() if loc else ""
            if name:
                return name
            retailer = getattr(ev, "retailer", None)
            r_loc = getattr(retailer, "location", None) if retailer else None
            r_loc_name = (getattr(r_loc, "name", None) or "").strip() if r_loc else ""
            return r_loc_name or None

        return await _get()

    @strawberry.field
    async def event_state(self) -> str | None:
        """State of the event. Prefers the event's State FK, falling back
        to the event location's state, then the retailer location's
        state."""
        if not getattr(self, "event_id", None):
            return None

        @sync_to_async
        def _get():
            ev = getattr(self, "event", None)
            if ev is None:
                return None
            st = getattr(ev, "state", None)
            name = (getattr(st, "name", None) or "").strip() if st else ""
            if name:
                return name
            loc = getattr(ev, "location", None)
            loc_state = getattr(loc, "state", None) if loc else None
            loc_state_name = (
                (getattr(loc_state, "name", None) or "").strip() if loc_state else ""
            )
            if loc_state_name:
                return loc_state_name
            retailer = getattr(ev, "retailer", None)
            r_loc = getattr(retailer, "location", None) if retailer else None
            r_state = getattr(r_loc, "state", None) if r_loc else None
            r_state_name = (
                (getattr(r_state, "name", None) or "").strip() if r_state else ""
            )
            return r_state_name or None

        return await _get()


@strawberry_django.type(models.CustomFieldValue)
class CustomFieldValue(Node):
    uuid: str
    value: str
    custom_field_id: strawberry.ID
    custom_field: "CustomField"
    created_at: str
    updated_at: str


@strawberry_django.type(models.CustomRecapProductSample)
class CustomRecapProductSample(Node):
    uuid: str
    product: event_types.Product
    quantity: int
    created_at: str
    updated_at: str


@strawberry_django.type(models.CustomRecapSalePerformance)
class CustomRecapSalePerformance(Node):
    uuid: str
    product: event_types.Product
    type_of_good: TypeOfGood
    price: float
    created_at: str
    updated_at: str


@strawberry_django.type(models.CustomRecapFile)
class CustomRecapFile(Node):
    uuid: str
    name: str
    approved: bool
    file_type_id: strawberry.ID
    file_recap_category_id: strawberry.ID | None
    created_at: str
    updated_at: str

    file_type: ambassador_types.FileType
    file_recap_category: FileRecapCategory | None

    @strawberry.field(name="url")
    async def url_str(self) -> str | None:
        """Return the public URL for the custom recap file if any.

        Two-step:
        1. Read from __dict__ when the column is loaded (the optimizer
           usually selects every column on a one-shot customRecap
           lookup, so this is the fast path).
        2. If the column is deferred — which happens when the
           strawberry-django optimizer trims to only-the-fields-Relay-
           asked-for and the parent is a *plural* connection — pull the
           row's `url` via `refresh_from_db` inside `sync_to_async`.
           The previous implementation did a bare `getattr(self, "url")`
           that raised `SynchronousOnlyOperation` in async Django and
           the broad `except` silently returned None. That's exactly
           what produced 62 × `<img src="">` on the custom-recap
           detail page even though the DB had every blob path.
        """
        # Fast path: column already on instance.
        field_file = self.__dict__.get("url")
        if field_file is None:
            # Slow path: refresh just the `url` column so we don't pull
            # the whole row twice. Async-safe.
            def _reload():
                self.refresh_from_db(fields=["url"])
                return self.__dict__.get("url")
            try:
                field_file = await sync_to_async(
                    _reload, thread_sensitive=True
                )()
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
class CustomRecapDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    custom_recap: CustomRecap | None = None


@strawberry.type
class DeleteCustomRecapResponse:
    """Result of deleting a CustomRecap.

    Returns the deleted recap's uuid so the client can prune it from
    Relay's store / the recaps list without a full refetch. Mirrors
    DeleteRecapResponse / events.types.DeleteRequestResponse.
    """

    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    deleted_custom_recap_uuid: str | None = None


@strawberry.type
class ImportConnecteamRecapPdfStat:
    """One row in the importer's per-field report. Tells the admin
    exactly which PDF labels mapped where and at what confidence."""

    pdf_label: str
    pdf_value: str
    field_name: str | None = None
    field_id: strawberry.ID | None = None
    score: float | None = None  # null when exact match
    skipped_reason: str | None = None  # null when value imported


@strawberry.type
class ImportConnecteamRecapPdfResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    custom_recap: CustomRecap | None = None
    matched_count: int = 0
    unmatched_count: int = 0
    stats: list[ImportConnecteamRecapPdfStat] = strawberry.field(
        default_factory=list,
    )


@strawberry.type
class ConnecteamRawPair:
    """One raw label/value pair lifted straight from the PDF — returned
    alongside the mapped fields so the admin can eyeball anything the
    auto-mapper didn't recognize and copy it in by hand."""

    label: str
    value: str


@strawberry.type
class ParseConnecteamRecapPdfResponse:
    """Read-only parse of a Connecteam PDF for the standard recap form.
    Every mapped field is a string so the frontend can drop it straight
    into the create form's inputs. Null = the parser found nothing for
    that field. Nothing here is persisted — the admin reviews, edits, and
    submits via createRecap."""

    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None

    # Consumer-engagement numbers (strings, ready for the form inputs).
    total_consumer: str | None = None
    first_time: str | None = None
    brand_aware: str | None = None
    willing: str | None = None
    not_willing: str | None = None
    # Sales.
    products_sold: str | None = None
    total_cans_sold: str | None = None
    total_packs_sold: str | None = None
    account_spend: str | None = None
    # Free-text sections.
    traffic_description: str | None = None
    competitive_presence: str | None = None
    quotes: str | None = None
    feedback: str | None = None
    demographics: str | None = None
    positive_stories: str | None = None
    reasons_to_decline: str | None = None
    do_differently: str | None = None
    account_notes: str | None = None

    matched_count: int = 0
    raw_pairs: list[ConnecteamRawPair] = strawberry.field(
        default_factory=list,
    )


@strawberry.type
class CustomRecapFileDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    custom_recap_file: CustomRecapFile | None = None


@strawberry_django.type(models.CustomField)
class CustomField(Node):
    uuid: str
    name: str
    required: bool
    custom_recap_template_id: strawberry.ID
    custom_field_type_id: strawberry.ID
    custom_field_type: "CustomRecapFieldType"
    recap_section_id: strawberry.ID
    recap_section: "RecapSection"
    created_at: str
    updated_at: str

    @strawberry.field
    def value(self) -> str | None:
        """Value for this field in a specific custom recap context, if present."""
        return getattr(self, "_custom_recap_value", None)

    @strawberry.field
    def custom_field_value_id(self) -> strawberry.ID | None:
        """Custom field value ID for this recap context, if present."""
        return getattr(self, "_custom_field_value_id", None)


@strawberry.type
class CustomFieldDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    custom_field: CustomField | None = None


@strawberry_django.type(models.CustomRecapTemplate)
class CustomRecapTemplate(Node):
    uuid: str
    name: str
    product_samples: bool
    sales_performance: bool
    layout: JSON
    event_type_id: strawberry.ID
    event_type: event_types.EventType
    tenant_id: strawberry.ID
    tenant: tenant_types.TenantType
    created_at: str
    updated_at: str
    custom_field: List[CustomField]


@strawberry.type
class CustomRecapTemplateDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    custom_recap_template: CustomRecapTemplate | None = None


@strawberry_django.type(models.CustomRecapFieldType)
class CustomRecapFieldType(Node):
    uuid: str
    name: str
    created_at: str
    updated_at: str


@strawberry.type
class CustomRecapFieldTypeDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    custom_recap_field_type: CustomRecapFieldType | None = None


@strawberry_django.type(models.RecapSection)
class RecapSection(Node):
    uuid: str
    name: str
    tenant_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry.type
class RecapSectionDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    recap_section: RecapSection | None = None


@strawberry.type
class RecapListResponse:
    total_pages: int
    recaps: List[Recap]
