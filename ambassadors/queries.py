import logging
import asyncio
from typing import Annotated, List
import strawberry
from enum import Enum
from asgiref.sync import sync_to_async
from graphql import GraphQLError
from django.db.models import Exists, Model, OuterRef, QuerySet

from ambassadors import types
from ambassadors import models
from ambassadors import inputs
from jobs import models as job_models
from utils.graphql.permissions import (
    StrictIsAuthenticated,
    IsClientOrSparkAdmin,
    email_grants_ignite_admin,
)
from utils.gcs import public_url, extract_blob_name_from_url
from events import models as event_models
from events import types as event_types
from utils.graphql.mixins import SparkGraphQLMixin, resolve_id_to_int
from utils.graphql.queries import BaseQueriesService
from utils.graphql.inputs import SparkGraphQLInput
from utils.graphql.relay import (
    CountableConnection,
    connection_from_queryset_async,
)


@strawberry.enum
class AmbassadorEventStatus(str, Enum):
    APPROVED = "approved"
    DECLINED = "declined"
    CANCELED = "canceled"


def _resolve_filter_id(value: strawberry.ID | None, label: str) -> int | None:
    """Resolve relay/global IDs used in filters to database IDs."""
    if value in (None, ""):
        return None
    try:
        return resolve_id_to_int(value)
    except (TypeError, ValueError, GraphQLError) as exc:
        raise GraphQLError(f"Invalid {label} ID.") from exc


def _resolve_filter_id_list(values: list[strawberry.ID], label: str) -> list[int]:
    """Resolve a list of relay/global IDs used in filters to database IDs."""
    try:
        return [resolve_id_to_int(value) for value in values]
    except (TypeError, ValueError, GraphQLError) as exc:
        raise GraphQLError(f"Invalid {label} ID.") from exc


def _shift_time_labels(event) -> tuple[str | None, str | None, str | None]:
    """Pre-format (date_label, start_label, end_label) for a shift in the
    EVENT's timezone so the mobile client displays them verbatim.

    The mobile app was rendering raw datetimes against the DEVICE clock,
    so a NY 10:30 PM shift showed as 5:30 AM on a CA phone. We format
    here against the venue's tz instead.

    Uses utils.tz.apply_dst_aware_offset — the same DST-aware helper the
    email / recap formatters use (it resolves event.timezone to a real
    IANA zone via ZoneInfo, falling back to the TimeZone.offset field, and
    finally to a 0-minute shift i.e. server/UTC wall-clock when the event
    has no resolvable timezone). Never raises; returns None for any label
    whose source datetime is missing.

    Label formats (strftime, with the leading-zero stripped so we get
    "10:15 PM" not "10:15 PM" with a padded hour, and "May 28" not
    "May 08"):
        date_label  → "Tue, May 28"
        start_label → "10:15 PM"
        end_label   → "10:30 PM"
    """
    from utils.tz import apply_dst_aware_offset

    tz_row = getattr(event, "timezone", None)
    start = getattr(event, "start_time", None)
    end = getattr(event, "end_time", None)

    def _fmt(value, fmt: str) -> str | None:
        local = apply_dst_aware_offset(value, tz_row)
        if local is None:
            return None
        # %-d / %-I are POSIX (Linux/macOS, our deploy target) — strip the
        # leading zero. Guard with a replace fallback just in case.
        try:
            return local.strftime(fmt)
        except ValueError:
            return local.strftime(fmt.replace("%-", "%")).lstrip("0")

    # date_label is derived from the venue-local start_time so it agrees
    # with start_label across a midnight/DST boundary; fall back to the
    # event's end_time if start is missing.
    date_source = start or end
    date_label = _fmt(date_source, "%a, %b %-d")
    start_label = _fmt(start, "%-I:%M %p")
    end_label = _fmt(end, "%-I:%M %p")
    return date_label, start_label, end_label


# Fallback IANA zone for events that have no resolvable timezone row. The
# business is US-marketing-led and the create flow defaults venues to a US
# zone; Pacific is the most conservative choice for "is this shift today?"
# because it's the latest US local clock (an event with no tz that's stored
# at e.g. 2026-06-03T02:00:00Z is still "Jun 2" in Pacific, matching how the
# BA who booked it thinks about it). Mirrors utils.tz / events.queries which
# both treat a missing offset as a soft fallback rather than crashing.
_DEFAULT_SHIFT_TZ = "America/Los_Angeles"


def _resolve_shift_zone(event):
    """ZoneInfo for an event: its own `timezone` row, else `_DEFAULT_SHIFT_TZ`.

    Resolves through `utils.tz.resolve_zoneinfo` (DST-aware IANA lookup with
    the static `offset` field as a fallback) — the same resolution
    `_shift_time_labels` uses to format the shift. Never raises; UTC is the
    last-ditch fallback if tzdata is somehow unavailable.
    """
    from datetime import timezone as _dt_timezone
    from zoneinfo import ZoneInfo
    from utils.tz import resolve_zoneinfo

    zi = resolve_zoneinfo(getattr(event, "timezone", None))
    if zi is not None:
        return zi
    try:
        return ZoneInfo(_DEFAULT_SHIFT_TZ)
    except Exception:  # pragma: no cover - tzdata always present on deploy
        return _dt_timezone.utc


def _event_local_dates(event):
    """(event_local_date, today_local_date) BOTH in the event's own timezone.

    The Active/Upcoming partition is decided entirely *within a single zone per
    event* so it can never leave a gap or double-count across the UTC/local
    midnight boundary:

      * event_local_date — the shift's wall-clock date in its own tz. A shift
        stored `2026-06-03T00:00:00Z` (5 PM Pacific Jun 2) is Jun 2 locally,
        even though its UTC date is Jun 3. (`start_time` is the authority;
        a dated-but-untimed booking falls back to `date`.)
      * today_local_date — "now" rendered into the *same* zone. Comparing two
        dates computed in one frame is what guarantees every approved booking
        in [today, +N] lands on exactly one of Active (==today) or
        Upcoming ((today, +N]).

    The event's tz resolves via `_resolve_shift_zone` (own `timezone` row, else
    Pacific). Returns (None, today) when the event has no placeable date (both
    start_time and date null) — callers skip such bookings (invisible until
    scheduled) rather than crashing.
    """
    from datetime import datetime as _datetime, timezone as _dt_timezone

    zi = _resolve_shift_zone(event)
    today_local = _datetime.now(_dt_timezone.utc).astimezone(zi).date()

    source = getattr(event, "start_time", None) or getattr(event, "date", None)
    if source is None:
        return None, today_local

    # Normalize to a UTC-aware datetime; stored datetimes are UTC under
    # USE_TZ=True but a naive value (some fixtures/imports) is treated as UTC.
    if source.tzinfo is None:
        source = source.replace(tzinfo=_dt_timezone.utc)
    else:
        source = source.astimezone(_dt_timezone.utc)

    return source.astimezone(zi).date(), today_local


@strawberry.input
class AmbassadorEventsFiltersInput:
    """Filters for ambassador-scoped events."""

    ambassador_uuid: strawberry.ID | None = None
    event_id: strawberry.ID | None = None
    types: list[strawberry.ID] | None = None
    statuses: list[AmbassadorEventStatus] | None = None
    start_date: str | None = None
    end_date: str | None = None


class BaseAmbassadorQueriesService(SparkGraphQLMixin):
    """Service for ambassador queries."""

    ordering: tuple[str, ...] = ("-created_at",)

    def get_model(self) -> Model:
        """Get the model for the service."""
        raise NotImplementedError("Subclasses must implement this method.")

    def get_queryset(self) -> QuerySet:
        """Get the queryset for the service."""
        return self.get_model().objects.all()

    def get_filtered_queryset(self, q: str | None = None) -> QuerySet:
        """Get the filtered queryset for the service."""
        queryset = self.get_queryset()
        if q:
            queryset = queryset.filter(name__icontains=q)
        return queryset

    def get_ordered_queryset(
        self,
        q: str | None = None,
        ordering: tuple[str, ...] | None = None,
    ) -> QuerySet:
        """Return the filtered queryset with ordering applied."""
        queryset = self.get_filtered_queryset(q)
        ordering = ordering or self.ordering
        if ordering:
            queryset = queryset.order_by(*ordering)
        return queryset

    async def get_connection(
        self,
        *,
        q: str | None = None,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        default_limit: int = 10,
        max_limit: int = 50,
        ordering: tuple[str, ...] | None = None,
        queryset: QuerySet | None = None,
    ) -> CountableConnection[Model]:
        """Return a Relay compliant connection for the queryset."""
        if queryset is None:
            queryset = self.get_ordered_queryset(q, ordering)
        try:
            return await connection_from_queryset_async(
                queryset,
                first=first,
                after=after,
                last=last,
                before=before,
                default_limit=default_limit,
                max_limit=max_limit,
            )
        except ValueError as exc:
            raise GraphQLError(str(exc)) from exc

    async def get_record(self, id: strawberry.ID) -> Model | None:
        """Get a single record."""
        try:
            return await sync_to_async(self.get_model().objects.get)(id=id)
        except self.get_model().DoesNotExist:
            raise GraphQLError("Record not found.")

    async def get_record_by_uuid(self, uuid: str) -> Model | None:
        """Get a single record by UUID."""
        try:
            return await sync_to_async(self.get_model().objects.get)(uuid=uuid)
        except self.get_model().DoesNotExist:
            raise GraphQLError("Record not found.")


class AmbassadorsTenantQueriesService(BaseQueriesService):
    """Base service with tenant resolution adjusted for ambassador role."""

    async def resolve_query_tenant_id(
        self,
        info: strawberry.Info,
        *,
        filters: SparkGraphQLInput | None = None,
    ) -> int | None:
        """Allow spark-admin/ambassador to query any tenant; clients stay restricted."""
        user = await self.get_user(info)
        filters_tenant_id = getattr(filters, "tenant_id", None) if filters else None
        role_slug = self.get_role_slug(user)
        resolved_tenant_id: int | None = None

        if filters_tenant_id is not None:
            try:
                resolved_tenant_id = resolve_id_to_int(filters_tenant_id)
            except (TypeError, ValueError, GraphQLError) as exc:
                raise GraphQLError("Invalid tenant ID.") from exc

        if role_slug in {"spark-admin", "ambassador"}:
            if filters_tenant_id is None:
                return None
            tenant = await self._get_tenant_without_membership(
                tenant_id=resolved_tenant_id
            )
            return tenant.id

        try:
            tenant = await self.get_user_tenant(
                info,
                tenant_id=resolved_tenant_id,
                user=user,
            )
            return tenant.id
        except GraphQLError as exc:
            membership_error = "not a member of this tenant" in str(exc).lower()
            if membership_error and role_slug != "client":
                raise GraphQLError("Tenant access denied.") from exc
            raise


class FileTypeQueriesService(BaseAmbassadorQueriesService):
    """Service for file type queries."""

    def get_model(self) -> type[models.FileType]:
        """Get the model for the service."""
        return models.FileType


class AmbassadorEventQueriesService(BaseAmbassadorQueriesService):
    """Service for ambassador event queries."""

    def get_model(self) -> type[models.AmbassadorEvent]:
        """Get the model for the service."""
        return models.AmbassadorEvent

    def get_ambassador_queryset(self, user, filter_by_user: bool = True) -> QuerySet:
        """Return ambassador events, optionally filtered by user."""
        queryset = self.get_model().objects.select_related(
            "ambassador__user",
            "event__request",
            "event__status",
            "event__event_type",
            "event__timezone",
        )

        if filter_by_user:
            queryset = queryset.filter(ambassador__user=user)

        return queryset.distinct()


@strawberry.type
class FileTypeQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def file_types(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.FileType]:
        """Get all file types using Relay pagination."""
        service = FileTypeQueriesService()
        user = await service.get_user(info)

        queryset = service.get_ordered_queryset(q=q)

        return await service.get_connection(
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
            queryset=queryset,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def file_type(
        self, info: strawberry.Info, uuid: strawberry.ID
    ) -> types.FileType | None:
        """Get a single file type by UUID."""
        try:
            service = FileTypeQueriesService()
            user = await service.get_user(info)
            file_type = await service.get_record_by_uuid(str(uuid))
            return file_type
        except GraphQLError:
            return None


@strawberry.type
class AmbassadorEventQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def tenant_ba_activation(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
    ) -> List[types.BaActivationRow]:
        """Admin dashboard: every BA booked on this tenant's events with
        their sign-in state — the "who hasn't activated their account yet"
        view (Feel Free week one: 5 of 8 BAs hadn't signed in and we only
        knew by running ad-hoc scripts). Admin access only; others get []."""
        from django.db.models import Count, Min, Q
        from django.utils import timezone as _tz

        from utils.graphql.mixins import resolve_id_to_int
        from utils.graphql.permissions import (
            _is_admin_access,
            resolve_request_user_access,
        )

        user = info.context.request.user
        if not getattr(user, "is_authenticated", False):
            return []
        _rs, _st, _su, _em = await resolve_request_user_access(user)
        if not _is_admin_access(_rs, _st, _su, _em):
            return []

        tid = resolve_id_to_int(tenant_id)

        def _fetch() -> List[types.BaActivationRow]:
            from ambassadors import models as a_models

            now = _tz.now()
            scoped = Q(
                ambassadors_events__tenant_id=tid,
                ambassadors_events__is_approved=True,
            )
            qs = (
                a_models.Ambassador.objects.filter(scoped)
                .select_related("user")
                .annotate(
                    booked=Count(
                        "ambassadors_events",
                        filter=Q(
                            ambassadors_events__tenant_id=tid,
                            ambassadors_events__is_approved=True,
                        ),
                        distinct=True,
                    ),
                    upcoming=Min(
                        "ambassadors_events__event__start_time",
                        filter=Q(
                            ambassadors_events__tenant_id=tid,
                            ambassadors_events__is_approved=True,
                            ambassadors_events__event__start_time__gte=now,
                        ),
                    ),
                )
                .distinct()
            )
            rows = []
            for amb in qs:
                u = amb.user
                name = (
                    f"{getattr(u, 'first_name', '')} "
                    f"{getattr(u, 'last_name', '') or ''}"
                ).strip() or getattr(u, "email", "?")
                rows.append(
                    types.BaActivationRow(
                        ambassador_uuid=strawberry.ID(str(amb.uuid)),
                        name=name,
                        email=getattr(u, "email", ""),
                        phone=amb.phone,
                        last_login=(
                            u.last_login.isoformat() if u.last_login else None
                        ),
                        signed_in=bool(u.last_login),
                        bookings=amb.booked,
                        next_shift=(
                            amb.upcoming.isoformat() if amb.upcoming else None
                        ),
                    )
                )
            # Not-signed-in first — they're the ones needing a nudge.
            rows.sort(key=lambda r: (r.signed_in, r.name.lower()))
            return rows

        return await sync_to_async(_fetch)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def my_upcoming_shifts(
        self,
        info: strawberry.Info,
        within_days: int = 14,
    ) -> List[types.ShiftOfferDetails]:
        """Accepted (is_approved=True) AmbassadorEvent rows for the
        current BA whose LOCAL event date falls AFTER today and within the
        next N days (default 14) — i.e. local date in (today, today+N].

        Powers the "Upcoming" section on the spark-mobile Shifts tab. This is
        the future-dated complement to `my_active_shifts` (local date == today):
        together the two cover every approved booking in [today, today+N] with
        no gap and no overlap. The partition is on each event's LOCAL date vs
        local "today" computed in the SAME zone (`_event_local_dates`, the
        event's own timezone, Pacific fallback) NOT its UTC date — so a shift
        stored 2026-06-03T00:00:00Z (5 PM Pacific Jun 2) is correctly "today"
        (Active) rather than slipping a UTC day forward into this list.

        Sorted by event start_time ascending (next-up first). Returns empty for
        non-ambassador users; cross-tenant access blocked via the
        Ambassador→user relationship.
        """
        from datetime import timedelta
        from django.utils import timezone
        from django.db.models import Q

        user = info.context.request.user
        if not getattr(user, "is_authenticated", False):
            return []

        from ambassadors import models as a_models
        from ambassadors import types as a_types

        within_days = max(1, within_days)

        def _fetch() -> List:
            try:
                ambassador = a_models.Ambassador.objects.get(user=user)
            except a_models.Ambassador.DoesNotExist:
                return []
            now = timezone.now()
            # Coarse UTC bracket for the DB scan only. The precise, gap-free
            # bucketing is done per-event in the loop (each event's local date
            # vs local-today in its own zone). 1 day of slack on the near edge
            # and 2 on the far edge generously cover every real UTC↔local
            # offset so no in-window shift is sliced off by this coarse bound.
            scan_start = now - timedelta(days=1)
            scan_end = now + timedelta(days=within_days + 2)
            qs = (
                a_models.AmbassadorEvent.objects.select_related(
                    "event",
                    "event__retailer",
                    "event__state",
                    "event__timezone",
                )
                .filter(
                    ambassador=ambassador,
                    is_approved=True,
                )
                .filter(
                    Q(event__start_time__gte=scan_start, event__start_time__lte=scan_end)
                    | Q(event__date__gte=scan_start, event__date__lte=scan_end)
                )
                .order_by("event__start_time")
            )
            out: List = []
            for ae in qs:
                ev = ae.event
                # Upcoming = LOCAL date strictly after local-today, through the
                # window end (today, today+N]. Both dates are computed in the
                # event's own zone (`_event_local_dates`) so the boundary with
                # my_active_shifts (local date == today) is exact: every booking
                # lands on exactly one list. Today-local shifts belong to Active;
                # an event we can't date (no start/date) is skipped, matching the
                # documented "won't show until scheduled" behavior.
                local_date, local_today = _event_local_dates(ev)
                if local_date is None:
                    continue
                window_end = local_today + timedelta(days=within_days)
                if not (local_today < local_date <= window_end):
                    continue
                venue = (
                    getattr(ev, "name", None)
                    or getattr(getattr(ev, "retailer", None), "name", None)
                )
                state_code = getattr(getattr(ev, "state", None), "code", None)
                # Event.coordinates is ArrayField(FloatField, size=2)
                # storing [latitude, longitude]. May be None or [] when
                # the venue hasn't been geocoded yet.
                coords = getattr(ev, "coordinates", None) or []
                lat = float(coords[0]) if len(coords) >= 1 else None
                lng = float(coords[1]) if len(coords) >= 2 else None
                date_label, start_label, end_label = _shift_time_labels(ev)
                out.append(
                    a_types.ShiftOfferDetails(
                        ambassador_event_uuid=strawberry.ID(str(ae.uuid)),
                        event_uuid=strawberry.ID(str(ev.uuid)),
                        event_name=venue or "(shift)",
                        venue=venue,
                        address=getattr(ev, "address", None),
                        date=ev.date.isoformat() if getattr(ev, "date", None) else None,
                        start_time=(
                            ev.start_time.isoformat()
                            if getattr(ev, "start_time", None)
                            else None
                        ),
                        end_time=(
                            ev.end_time.isoformat()
                            if getattr(ev, "end_time", None)
                            else None
                        ),
                        state_code=state_code,
                        is_approved=True,
                        latitude=lat,
                        longitude=lng,
                        date_label=date_label,
                        start_label=start_label,
                        end_label=end_label,
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
            return out

        return await sync_to_async(_fetch, thread_sensitive=True)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def my_active_shifts(
        self,
        info: strawberry.Info,
    ) -> List[types.ShiftOfferDetails]:
        """The current BA's accepted (is_approved=True) AmbassadorEvent rows
        scheduled for TODAY — powers the "Active" section on the mobile
        Shifts tab.

        This replaces the old tenant-wide `todayEvents` for that section:
        because every row here is one of the caller's OWN AmbassadorEvents,
        clock-in / extend / heads-up can resolve the shift reliably via
        `ambassadorEventUuid` (the previous `todayEvents` showed every
        tenant event, so acting on one the BA wasn't rostered on returned
        "Shift not found").

        "Today" is the shift's LOCAL date (the venue's wall-clock day via
        `_event_local_dates`) compared against local "today" in the SAME zone,
        NOT the server's UTC date. Under TIME_ZONE=UTC, a 5 PM Pacific shift on
        Jun 2 is stored 2026-06-03T00:00:00Z; the old `__date` filter compared
        its UTC date (Jun 3) against a UTC `today` (Jun 2) and dropped it — so a
        just-booked evening shift showed on neither Active nor Upcoming. We now
        compare local-to-local. This includes shifts whose start time has
        already passed earlier today so an in-progress shift can still be
        clocked in. BA-scoped via Ambassador→user; empty for non-ambassador
        users.
        """
        from datetime import timedelta
        from django.utils import timezone
        from django.db.models import Q

        user = info.context.request.user
        if not getattr(user, "is_authenticated", False):
            return []

        from ambassadors import models as a_models
        from ambassadors import types as a_types

        def _fetch() -> List:
            try:
                ambassador = a_models.Ambassador.objects.get(user=user)
            except a_models.Ambassador.DoesNotExist:
                return []
            now = timezone.now()
            # Coarse ±1 day UTC bracket for the DB scan only; the exact
            # "local date == local today (same zone)" decision happens in the
            # loop via _event_local_dates so the UTC/local boundary can't drop a
            # today-evening shift. start_time is the authority, but a
            # dated-but-untimed booking is bracketed on `date` too — both share
            # the same local-date test below.
            scan_start = now - timedelta(days=1)
            scan_end = now + timedelta(days=1)
            qs = (
                a_models.AmbassadorEvent.objects.select_related(
                    "event",
                    "event__retailer",
                    "event__state",
                    "event__timezone",
                )
                .filter(
                    ambassador=ambassador,
                    is_approved=True,
                )
                .filter(
                    Q(event__start_time__gte=scan_start, event__start_time__lte=scan_end)
                    | Q(event__date__gte=scan_start, event__date__lte=scan_end)
                )
                .order_by("event__start_time")
            )
            rows = list(qs)
            # Latest clock-in per event for these bookings, so the app can
            # show clocked-in state instead of re-offering "Clock in".
            clock_map: dict = {}
            if rows:
                from ambassadors.models import Attendance

                for att in Attendance.objects.filter(
                    ambassador=ambassador,
                    event_id__in=[ae.event_id for ae in rows],
                    source__name="clock_in",
                ).order_by("clock_time"):
                    clock_map[att.event_id] = att.clock_time.isoformat()
            out: List = []
            for ae in rows:
                ev = ae.event
                # Active = the shift's LOCAL date equals local today, both in
                # the event's own zone. Skip events with no placeable date
                # (both start_time and date null) — they stay invisible until
                # scheduled, matching prior behavior.
                local_date, local_today = _event_local_dates(ev)
                if local_date is None or local_date != local_today:
                    continue
                venue = (
                    getattr(ev, "name", None)
                    or getattr(getattr(ev, "retailer", None), "name", None)
                )
                state_code = getattr(getattr(ev, "state", None), "code", None)
                coords = getattr(ev, "coordinates", None) or []
                lat = float(coords[0]) if len(coords) >= 1 else None
                lng = float(coords[1]) if len(coords) >= 2 else None
                date_label, start_label, end_label = _shift_time_labels(ev)
                out.append(
                    a_types.ShiftOfferDetails(
                        ambassador_event_uuid=strawberry.ID(str(ae.uuid)),
                        event_uuid=strawberry.ID(str(ev.uuid)),
                        event_name=venue or "(shift)",
                        venue=venue,
                        address=getattr(ev, "address", None),
                        date=ev.date.isoformat() if getattr(ev, "date", None) else None,
                        start_time=(
                            ev.start_time.isoformat()
                            if getattr(ev, "start_time", None)
                            else None
                        ),
                        end_time=(
                            ev.end_time.isoformat()
                            if getattr(ev, "end_time", None)
                            else None
                        ),
                        state_code=state_code,
                        is_approved=True,
                        latitude=lat,
                        longitude=lng,
                        date_label=date_label,
                        start_label=start_label,
                        end_label=end_label,
                        clocked_in_at=clock_map.get(ae.event_id),
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
            return out

        return await sync_to_async(_fetch, thread_sensitive=True)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def my_past_shifts_owing_recap(
        self,
        info: strawberry.Info,
        within_days: int = 30,
    ) -> List[types.ShiftOfferDetails]:
        """The current BA's accepted shifts that have already ENDED but
        still have no recap — powers a "Needs a recap" section on the
        mobile Shifts tab so a BA who forgot to clock in (or whose shift
        closed) can still file late.

        A shift "owes a recap" when neither a Recap nor a CustomRecap
        exists for that event by this ambassador. Scoped to shifts that
        ended within the last `within_days`. BA-scoped via Ambassador→user;
        empty for non-ambassador users.
        """
        from datetime import timedelta
        from django.utils import timezone

        user = info.context.request.user
        if not getattr(user, "is_authenticated", False):
            return []

        from ambassadors import models as a_models
        from ambassadors import types as a_types
        from recaps.models import Recap, CustomRecap

        def _fetch() -> List:
            try:
                ambassador = a_models.Ambassador.objects.get(user=user)
            except a_models.Ambassador.DoesNotExist:
                return []
            now = timezone.now()
            window_start = now - timedelta(days=within_days)
            qs = (
                a_models.AmbassadorEvent.objects.select_related(
                    "event",
                    "event__retailer",
                    "event__state",
                    "event__timezone",
                )
                .filter(
                    ambassador=ambassador,
                    is_approved=True,
                    event__end_time__lt=now,
                    event__end_time__gte=window_start,
                )
                .order_by("-event__end_time")
            )
            out: List = []
            for ae in qs:
                ev = ae.event
                # Skip shifts that already have a recap (either family)
                # filed for this BA — they don't "owe" one.
                already_recapped = (
                    Recap.objects.filter(event=ev, ambassador=ambassador).exists()
                    or CustomRecap.objects.filter(
                        event=ev, ambassador=ambassador
                    ).exists()
                )
                if already_recapped:
                    continue
                venue = (
                    getattr(ev, "name", None)
                    or getattr(getattr(ev, "retailer", None), "name", None)
                )
                state_code = getattr(getattr(ev, "state", None), "code", None)
                coords = getattr(ev, "coordinates", None) or []
                lat = float(coords[0]) if len(coords) >= 1 else None
                lng = float(coords[1]) if len(coords) >= 2 else None
                date_label, start_label, end_label = _shift_time_labels(ev)
                out.append(
                    a_types.ShiftOfferDetails(
                        ambassador_event_uuid=strawberry.ID(str(ae.uuid)),
                        event_uuid=strawberry.ID(str(ev.uuid)),
                        event_name=venue or "(shift)",
                        venue=venue,
                        address=getattr(ev, "address", None),
                        date=ev.date.isoformat() if getattr(ev, "date", None) else None,
                        start_time=(
                            ev.start_time.isoformat()
                            if getattr(ev, "start_time", None)
                            else None
                        ),
                        end_time=(
                            ev.end_time.isoformat()
                            if getattr(ev, "end_time", None)
                            else None
                        ),
                        state_code=state_code,
                        is_approved=True,
                        latitude=lat,
                        longitude=lng,
                        date_label=date_label,
                        start_label=start_label,
                        end_label=end_label,
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
            return out

        return await sync_to_async(_fetch, thread_sensitive=True)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def shift_context(
        self,
        info: strawberry.Info,
        event_uuid: strawberry.ID,
    ) -> types.ShiftContext | None:
        """Brand / project / product context for a single shift, keyed by
        the parent event's UUID — the same identifier the mobile app
        already holds on the shift-detail screen (where it also fetches
        jobBriefingForEvent).

        Purely additive read-only display that complements the pre-shift
        briefing (#191): everything comes from the event's parent Request
        (Request.client / client_name, Request.notes, and
        RequestProduct -> Product -> image). Admins keep editing brand +
        products in the existing request flows; this only surfaces them.

        Scoping mirrors jobBriefingForEvent: any authenticated BA can read
        context for an event by UUID (BAs receive shift offers keyed by
        event UUID and don't see internal IDs). We don't widen visibility
        beyond what the existing event-by-uuid mobile reads already allow.

        Returns a populated ShiftContext with null fields + an empty
        products list when the event has no request attached, and None
        only when the event UUID doesn't resolve at all.
        """

        def _fetch() -> "types.ShiftContext | None":
            try:
                event_uuid_str = str(event_uuid)
            except Exception:
                return None

            event = (
                event_models.Event.objects.select_related(
                    "request",
                    "request__client",
                )
                .filter(uuid=event_uuid_str)
                .first()
            )
            if event is None:
                return None

            _mileage_rate = (
                float(event.mileage_rate)
                if getattr(event, "mileage_rate", None) is not None
                else None
            )
            _track_mileage = bool(getattr(event, "track_mileage", False))

            request = getattr(event, "request", None)
            if request is None:
                # Event with no parent Request — nothing to surface, but
                # the card is still a valid (empty) context.
                return types.ShiftContext(
                    brand_name=None,
                    products=[],
                    project_notes=None,
                    track_mileage=_track_mileage,
                    mileage_rate=_mileage_rate,
                    event_uuid=str(event.uuid),
                )

            # Brand: prefer the linked Client's name, fall back to the
            # free-text client_name snapshot stored on the Request.
            client = getattr(request, "client", None)
            brand_name = (
                getattr(client, "name", None)
                or getattr(request, "client_name", None)
                or None
            )

            project_notes = getattr(request, "notes", None) or None

            products: List[types.ShiftProduct] = []
            rp_qs = (
                event_models.RequestProduct.objects.select_related("product")
                .filter(request=request)
                .order_by("id")
            )
            for rp in rp_qs:
                product = getattr(rp, "product", None)
                if product is None:
                    continue
                name = getattr(product, "name", None)
                if not name:
                    continue

                image_url = None
                # Match the web Product type's image resolution: pull the
                # FieldFile's blob name and run it through public_url
                # (non-signed; the bucket grants public object read).
                field_file = getattr(product, "image", None)
                if field_file:
                    try:
                        blob = field_file.name
                    except Exception:
                        blob = str(field_file)
                    image_url = public_url(extract_blob_name_from_url(blob))

                products.append(
                    types.ShiftProduct(
                        id=strawberry.ID(str(product.id)),
                        name=name,
                        image_url=image_url,
                    )
                )

            return types.ShiftContext(
                brand_name=brand_name,
                products=products,
                project_notes=project_notes,
                track_mileage=_track_mileage,
                mileage_rate=_mileage_rate,
                event_uuid=str(event.uuid),
            )

        return await sync_to_async(_fetch, thread_sensitive=True)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def my_mileage_sessions(
        self,
        info: strawberry.Info,
        event_uuid: strawberry.ID,
    ) -> list[types.MileageSessionType]:
        """The calling BA's mileage trips for a gig (newest first, with the
        GPS trail). The mobile tracker reads this to show the running total +
        resume an active trip after an app restart."""
        from .services import MileageService
        from .models import MileageSession

        user = info.context.request.user

        def _fetch():
            amb = MileageService._ambassador_for(user)
            event = MileageService._resolve_event(event_uuid)
            if not amb or not event:
                return []
            sessions = (
                MileageSession.objects.filter(ambassador=amb, event=event)
                .select_related("ambassador__user", "event")
                .order_by("-started_at")
            )
            return [
                MileageService._session_type(s, include_breadcrumbs=True)
                for s in sessions
            ]

        return await sync_to_async(_fetch, thread_sensitive=True)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def event_mileage_summary(
        self,
        info: strawberry.Info,
        event_id: strawberry.ID,
    ) -> types.EventMileageSummary | None:
        """Admin viewer: every BA's mileage trips for a gig + total miles and
        reimbursement. Ignite admins see all trips; anyone else sees only
        their own (mileage/reimbursement is an internal payout concern)."""
        from .services import MileageService
        from .models import MileageSession, Ambassador as _Amb

        user = info.context.request.user

        def _fetch():
            event = MileageService._resolve_event(event_id)
            if not event:
                return None
            qs = (
                MileageSession.objects.filter(event=event)
                .select_related("ambassador__user", "event")
                .order_by("-started_at")
            )
            email = (getattr(user, "email", "") or "").lower()
            is_admin = bool(
                getattr(user, "is_staff", False)
                or getattr(user, "is_superuser", False)
                or email_grants_ignite_admin(email)
            )
            if not is_admin:
                amb = _Amb.objects.filter(user=user).first()
                qs = qs.filter(ambassador=amb) if amb else qs.none()
            sessions = list(qs)
            total_miles = round(
                sum(float(s.total_miles or 0) for s in sessions), 2
            )
            total_reimb = round(
                sum(float(s.reimbursement_amount or 0) for s in sessions), 2
            )
            return types.EventMileageSummary(
                event_uuid=str(event.uuid),
                total_miles=total_miles,
                total_reimbursement=total_reimb,
                session_count=len(sessions),
                sessions=[
                    MileageService._session_type(s, include_breadcrumbs=True)
                    for s in sessions
                ],
                track_mileage=bool(getattr(event, "track_mileage", False)),
                mileage_rate=(
                    float(event.mileage_rate)
                    if getattr(event, "mileage_rate", None) is not None
                    else None
                ),
            )

        return await sync_to_async(_fetch, thread_sensitive=True)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def shift_offer(
        self,
        info: strawberry.Info,
        ambassador_event_uuid: strawberry.ID,
    ) -> types.ShiftOfferDetails | None:
        """Fetch a single shift offer (AmbassadorEvent invitation) for
        the current BA. Powers the mobile ShiftOfferScreen that deep-
        links from the "New shift offered" push notification."""
        from .services import ShiftOfferService

        return await ShiftOfferService.get_offer(
            str(ambassador_event_uuid), info
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def my_pending_offers(
        self,
        info: strawberry.Info,
    ) -> list[types.ShiftOfferDetails]:
        """All unaccepted shift offers for the current BA — invitations
        that haven't been accepted yet (is_approved=False).

        Powers the "Pending invites" section on the mobile Shifts tab,
        which surfaces offers the BA missed via push tap. The mobile
        client opens each into the existing ShiftOfferScreen for
        accept/decline.

        Excludes shifts whose event.start_time is already in the past
        — those are stale and shouldn't be acted on. Sorted by start
        time ascending (next-up first). Returns empty for non-
        ambassador users; cross-tenant access blocked via the
        Ambassador→user relationship.
        """
        from django.utils import timezone

        user = info.context.request.user
        if not getattr(user, "is_authenticated", False):
            return []

        def _fetch() -> list:
            try:
                ambassador = models.Ambassador.objects.get(user=user)
            except models.Ambassador.DoesNotExist:
                return []
            now = timezone.now()
            qs = (
                models.AmbassadorEvent.objects.select_related(
                    "event",
                    "event__retailer",
                    "event__state",
                    "event__timezone",
                )
                .filter(
                    ambassador=ambassador,
                    is_approved=False,
                    event__start_time__gte=now,
                )
                .order_by("event__start_time")
            )
            out: list = []
            for ae in qs:
                ev = ae.event
                venue = (
                    getattr(ev, "name", None)
                    or getattr(getattr(ev, "retailer", None), "name", None)
                )
                state_code = getattr(getattr(ev, "state", None), "code", None)
                # Pre-format date/time in the VENUE's tz so the mobile client
                # renders them verbatim — a NY shift shows NY time on a CA
                # phone. Without these the pending-invite row device-parsed the
                # raw datetimes (wrong time out-of-state; and event.date at
                # 00:00Z showed the prior day). Mirrors my_upcoming_shifts.
                date_label, start_label, end_label = _shift_time_labels(ev)
                out.append(
                    types.ShiftOfferDetails(
                        ambassador_event_uuid=strawberry.ID(str(ae.uuid)),
                        event_uuid=strawberry.ID(str(ev.uuid)),
                        event_name=venue or "(shift)",
                        venue=venue,
                        address=getattr(ev, "address", None),
                        date=ev.date.isoformat() if getattr(ev, "date", None) else None,
                        start_time=(
                            ev.start_time.isoformat()
                            if getattr(ev, "start_time", None)
                            else None
                        ),
                        end_time=(
                            ev.end_time.isoformat()
                            if getattr(ev, "end_time", None)
                            else None
                        ),
                        state_code=state_code,
                        is_approved=False,
                        date_label=date_label,
                        start_label=start_label,
                        end_label=end_label,
                    )
                )
            return out

        return await sync_to_async(_fetch, thread_sensitive=True)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def my_open_shifts(
        self,
        info: strawberry.Info,
    ) -> list[types.OpenShiftItem]:
        """Dropped shifts the current BA can claim — the self-serve "Open
        shifts" board.

        A shift shows here when it is: unclaimed, still in the future, for a
        brand (tenant) the BA has worked with before, not one they dropped
        themselves, and not an event they're already on. This keeps the board
        relevant and tenant-safe (no cross-brand exposure). Claiming is
        instant via claimOpenShift. Sorted next-up first.
        """
        from django.utils import timezone

        user = info.context.request.user
        if not getattr(user, "is_authenticated", False):
            return []

        def _fetch() -> list:
            ambassador = models.Ambassador.objects.filter(user=user).first()
            if ambassador is None:
                return []
            now = timezone.now()
            # Brands this BA has any history with (vetted audience for a drop).
            worked_tenant_ids = set(
                models.AmbassadorEvent.objects.filter(ambassador=ambassador)
                .values_list("event__tenant_id", flat=True)
            )
            if not worked_tenant_ids:
                return []
            # Events the BA already has a row on — never show those.
            my_event_ids = set(
                models.AmbassadorEvent.objects.filter(
                    ambassador=ambassador
                ).values_list("event_id", flat=True)
            )
            qs = (
                models.OpenShift.objects.select_related(
                    "event", "event__retailer", "event__state"
                )
                .filter(
                    claimed_at__isnull=True,
                    event__start_time__gte=now,
                    event__tenant_id__in=worked_tenant_ids,
                )
                .exclude(released_by_id=getattr(user, "id", None))
                .exclude(event_id__in=my_event_ids)
                .order_by("event__start_time")[:100]
            )
            out: list = []
            seen_events: set = set()
            for row in qs:
                ev = row.event
                # One card per event even if it was dropped more than once.
                if ev.id in seen_events:
                    continue
                seen_events.add(ev.id)
                venue = (
                    getattr(ev, "name", None)
                    or getattr(getattr(ev, "retailer", None), "name", None)
                )
                out.append(
                    types.OpenShiftItem(
                        open_shift_uuid=strawberry.ID(str(row.uuid)),
                        event_uuid=strawberry.ID(str(ev.uuid)),
                        event_name=venue or "(shift)",
                        venue=venue,
                        address=getattr(ev, "address", None),
                        start_time=(
                            ev.start_time.isoformat()
                            if getattr(ev, "start_time", None)
                            else None
                        ),
                        end_time=(
                            ev.end_time.isoformat()
                            if getattr(ev, "end_time", None)
                            else None
                        ),
                        state_code=getattr(getattr(ev, "state", None), "code", None),
                    )
                )
            return out

        return await sync_to_async(_fetch, thread_sensitive=True)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def my_earnings_stats(
        self,
        info: strawberry.Info,
        within_days: int = 30,
    ) -> types.MyEarningsStats:
        """Honest BA-facing earnings preview: completed shift count + an
        hour estimate over the last `withinDays` days.

        Approximations (until payroll is wired):
        - "Completed" = AmbassadorEvent.is_approved=True AND event.date
          < now. Doesn't require an attendance row, so it works even
          where the BA forgot to clock out.
        - "Hours" = sum of (event.end_time - event.start_time) across
          those shifts. Treats start/end as same-day local; rolls over
          midnight as +24h. None when the window has zero shifts.
        """
        from datetime import datetime, timedelta, timezone as _tz

        service = AmbassadorEventQueriesService()
        user = await service.get_user(info)

        days = max(1, min(int(within_days), 365))
        now = datetime.now(_tz.utc)
        cutoff = now - timedelta(days=days)

        qs = (
            service.get_model()
            .objects.select_related("event")
            .filter(
                ambassador__user=user,
                is_approved=True,
                event__date__gte=cutoff,
                event__date__lte=now,
            )
        )
        rows = await sync_to_async(list)(qs)

        shifts_count = len(rows)
        if shifts_count == 0:
            return types.MyEarningsStats(
                shifts_count=0,
                hours_estimate=None,
                within_days=days,
                approved_shifts_count=0,
                approved_hours=None,
            )

        # Which of these shifts have an APPROVED recap (legacy or custom)
        # filed by this BA? Approving the recap is what "approves" the hours
        # (Kyle's model), so those shifts' scheduled length becomes the
        # locked-in approved-hours figure the BA sees.
        event_ids = [ae.event_id for ae in rows if ae.event_id]
        approved_event_ids: set = set()
        if event_ids:
            from recaps.models import CustomRecap, Recap

            def _approved_event_ids() -> set:
                legacy = set(
                    Recap.objects.filter(
                        ambassador__user=user,
                        approved=True,
                        event_id__in=event_ids,
                    ).values_list("event_id", flat=True)
                )
                custom = set(
                    CustomRecap.objects.filter(
                        ambassador__user=user,
                        approved=True,
                        event_id__in=event_ids,
                    ).values_list("event_id", flat=True)
                )
                return legacy | custom

            approved_event_ids = await sync_to_async(_approved_event_ids)()

        def _shift_hours(ae) -> float:
            ev = ae.event
            start = getattr(ev, "start_time", None)
            end = getattr(ev, "end_time", None)
            if not start or not end:
                return 0.0
            # start/end are TimeField; roll over midnight by adding 24h.
            s_secs = (start.hour * 3600) + (start.minute * 60) + start.second
            e_secs = (end.hour * 3600) + (end.minute * 60) + end.second
            delta = e_secs - s_secs
            if delta < 0:
                delta += 24 * 3600
            return float(delta) / 3600.0

        total_hours = 0.0
        approved_hours = 0.0
        approved_shifts = 0
        for ae in rows:
            h = _shift_hours(ae)
            total_hours += h
            if ae.event_id in approved_event_ids:
                approved_hours += h
                approved_shifts += 1

        return types.MyEarningsStats(
            shifts_count=shifts_count,
            hours_estimate=round(total_hours, 2),
            within_days=days,
            approved_shifts_count=approved_shifts,
            approved_hours=round(approved_hours, 2) if approved_shifts else None,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def my_earnings_breakdown(
        self,
        info: strawberry.Info,
        within_days: int = 30,
    ) -> types.MyEarningsBreakdown:
        """Per-shift earnings breakdown for the current BA over the last
        `withinDays` days (1-365, default 30).

        Each row is a completed (is_approved=True, event.date in window)
        AmbassadorEvent with its venue, date, hours, and a block proxy.

        HONESTY CONTRACT: Spark does not own payroll. Wingspan holds the
        money and links payments to BAs by email only — there is no
        payment->shift join — so `gross` is None and `payment_status` is
        "not_available" on every row, and `payments_available` is False.
        These fields are typed/forward-compatible: when a real payment
        correlation lands, populate them with NO schema change. We never
        fabricate a dollar figure here.
        """
        from datetime import datetime, timedelta, timezone as _tz
        from math import ceil

        user = info.context.request.user
        if not getattr(user, "is_authenticated", False):
            return types.MyEarningsBreakdown(
                within_days=max(1, min(int(within_days), 365)),
                shifts_count=0,
                hours_total=None,
                payments_available=False,
                rows=[],
            )

        days = max(1, min(int(within_days), 365))
        now = datetime.now(_tz.utc)
        cutoff = now - timedelta(days=days)

        def _hours(start, end) -> float | None:
            # Mirror my_earnings_stats: treat start/end via their clock
            # components and roll a negative delta over midnight (+24h).
            # Works whether the ORM hands back time or datetime objects.
            if not start or not end:
                return None
            s = (start.hour * 3600) + (start.minute * 60) + start.second
            e = (end.hour * 3600) + (end.minute * 60) + end.second
            delta = e - s
            if delta < 0:
                delta += 24 * 3600
            return round(delta / 3600.0, 2)

        def _fetch() -> types.MyEarningsBreakdown:
            try:
                ambassador = models.Ambassador.objects.get(user=user)
            except models.Ambassador.DoesNotExist:
                return types.MyEarningsBreakdown(
                    within_days=days,
                    shifts_count=0,
                    hours_total=None,
                    payments_available=False,
                    rows=[],
                )

            qs = (
                models.AmbassadorEvent.objects.select_related(
                    "event",
                    "event__retailer",
                    "event__state",
                )
                .filter(
                    ambassador=ambassador,
                    is_approved=True,
                    event__date__gte=cutoff,
                    event__date__lte=now,
                )
                .order_by("-event__date")
            )

            rows: list[types.EarningsShiftRow] = []
            total_hours = 0.0
            any_hours = False
            for ae in qs:
                ev = ae.event
                venue = (
                    getattr(ev, "name", None)
                    or getattr(getattr(ev, "retailer", None), "name", None)
                    or "(shift)"
                )
                state_code = getattr(getattr(ev, "state", None), "code", None)
                hrs = _hours(
                    getattr(ev, "start_time", None),
                    getattr(ev, "end_time", None),
                )
                blocks = None
                if hrs is not None:
                    any_hours = True
                    total_hours += hrs
                    blocks = max(1, ceil(hrs / 4.0)) if hrs > 0 else 0
                rows.append(
                    types.EarningsShiftRow(
                        ambassador_event_uuid=strawberry.ID(str(ae.uuid)),
                        event_uuid=strawberry.ID(str(ev.uuid)),
                        venue=venue,
                        date=(
                            ev.date.isoformat()
                            if getattr(ev, "date", None)
                            else None
                        ),
                        start_time=(
                            ev.start_time.isoformat()
                            if getattr(ev, "start_time", None)
                            else None
                        ),
                        end_time=(
                            ev.end_time.isoformat()
                            if getattr(ev, "end_time", None)
                            else None
                        ),
                        state_code=state_code,
                        hours=hrs,
                        blocks=blocks,
                        gross=None,  # honest: no per-shift $ source exists
                        payment_status="not_available",
                    )
                )

            return types.MyEarningsBreakdown(
                within_days=days,
                shifts_count=len(rows),
                hours_total=round(total_hours, 2) if any_hours else None,
                payments_available=False,
                rows=rows,
            )

        return await sync_to_async(_fetch, thread_sensitive=True)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def my_rating_summary(
        self,
        info: strawberry.Info,
    ) -> types.MyRatingSummary:
        """BA-facing ratings + reliability streak (#197).

        Ratings: average + count over ALL of this BA's AmbassadorRating
        rows, plus the newest 5. Streak: consecutive most-recent
        completed shifts (event.start_time in the past) that have BOTH
        an on-time clock-in (Attendance.source='clock_in' with
        clock_time <= start_time + 10m grace) AND a filed Recap.
        """
        from datetime import datetime, timedelta, timezone as _tz

        service = AmbassadorEventQueriesService()
        user = await service.get_user(info)

        GRACE = timedelta(minutes=10)
        BEST_SCAN_LIMIT = 180  # bound the history walk
        RECENT_LIMIT = 5
        now = datetime.now(_tz.utc)

        def _compute():
            from ambassadors.models import Ambassador, Attendance, AmbassadorRating
            from recaps.models import Recap
            from django.db.models import Avg, Count

            ba = (
                Ambassador.objects.filter(user=user)
                .only("id")
                .first()
            )
            if ba is None:
                return types.MyRatingSummary(
                    average=0.0,
                    count=0,
                    recent=[],
                    current_streak=0,
                    best_streak=0,
                    last_shift_on_time=None,
                )

            ba_id = ba.id

            # ---- ratings aggregate ----
            agg = AmbassadorRating.objects.filter(ambassador_id=ba_id).aggregate(
                avg=Avg("score"), cnt=Count("id")
            )
            avg = round(float(agg["avg"]), 1) if agg["avg"] is not None else 0.0
            cnt = int(agg["cnt"] or 0)

            recent_rows = list(
                AmbassadorRating.objects.filter(ambassador_id=ba_id)
                .select_related("event")
                .order_by("-created_at")[:RECENT_LIMIT]
            )
            recent = [
                types.MyRatingRecent(
                    score=int(r.score),
                    comment=(r.comment or None),
                    created_at=r.created_at.isoformat() if r.created_at else "",
                    event_name=(getattr(r.event, "name", None) if r.event_id else None),
                )
                for r in recent_rows
            ]

            # ---- reliability streak ----
            # Completed shifts for this BA, newest first, bounded.
            # event.start_time in the past = "completed" (matches the
            # my_earnings_stats convention of date < now without
            # requiring an attendance row).
            ae_rows = list(
                models.AmbassadorEvent.objects.filter(
                    ambassador_id=ba_id,
                    event__start_time__isnull=False,
                    event__start_time__lte=now,
                )
                .select_related("event")
                .order_by("-event__start_time")[:BEST_SCAN_LIMIT]
            )
            if not ae_rows:
                return types.MyRatingSummary(
                    average=avg,
                    count=cnt,
                    recent=recent,
                    current_streak=0,
                    best_streak=0,
                    last_shift_on_time=None,
                )

            event_ids = [ae.event_id for ae in ae_rows]

            # On-time clock-in set: event_ids where a clock_in Attendance
            # exists for this BA with clock_time <= start_time + grace.
            # Pull the earliest clock_in per event, compare in python so
            # the grace add is straightforward and tz-safe.
            clockins = (
                Attendance.objects.filter(
                    ambassador_id=ba_id,
                    event_id__in=event_ids,
                    source__name="clock_in",
                )
                .select_related("event")
                .values("event_id", "clock_time", "event__start_time")
            )
            on_time_event_ids = set()
            earliest_ci = {}  # event_id -> earliest clock_time
            start_by_event = {}
            for row in clockins:
                eid = row["event_id"]
                ct = row["clock_time"]
                st = row["event__start_time"]
                start_by_event[eid] = st
                if eid not in earliest_ci or (ct and ct < earliest_ci[eid]):
                    earliest_ci[eid] = ct
            for eid, ct in earliest_ci.items():
                st = start_by_event.get(eid)
                if ct is not None and st is not None and ct <= st + GRACE:
                    on_time_event_ids.add(eid)

            # Filed-recap set: event_ids with a submitted recap by this BA.
            recap_event_ids = set(
                Recap.objects.filter(
                    ambassador_id=ba_id,
                    event_id__in=event_ids,
                    submited_at__isnull=False,
                ).values_list("event_id", flat=True)
            )

            def _ok(eid: int) -> bool:
                return eid in on_time_event_ids and eid in recap_event_ids

            # current streak: walk newest->oldest, stop at first failure.
            current = 0
            for ae in ae_rows:
                if _ok(ae.event_id):
                    current += 1
                else:
                    break

            # best streak: longest run across the scanned window.
            best = 0
            run = 0
            for ae in ae_rows:
                if _ok(ae.event_id):
                    run += 1
                    if run > best:
                        best = run
                else:
                    run = 0

            last_on_time = ae_rows[0].event_id in on_time_event_ids

            return types.MyRatingSummary(
                average=avg,
                count=cnt,
                recent=recent,
                current_streak=current,
                best_streak=best,
                last_shift_on_time=last_on_time,
            )

        return await sync_to_async(_compute, thread_sensitive=True)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def ambassador_events(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: AmbassadorEventsFiltersInput | None = None,
    ) -> CountableConnection[types.AmbassadorEventType]:
        """Return ambassador events with ambassador and user nested.

        If user role is 'ambassador', only returns events for the logged ambassador.
        Otherwise, returns all ambassador events (for admins, clients, etc).
        """
        service = AmbassadorEventQueriesService()
        user = await service.get_user(info)

        # Check if user role is ambassador
        role_slug = service.get_role_slug(user)
        filter_by_user = role_slug == "ambassador"

        queryset = service.get_ambassador_queryset(user, filter_by_user=filter_by_user)
        if q:
            queryset = queryset.filter(event__name__icontains=q)

        if filters:
            if filters.ambassador_uuid:
                queryset = queryset.filter(ambassador__uuid=filters.ambassador_uuid)
            if filters.event_id:
                event_id = _resolve_filter_id(filters.event_id, "event")
                queryset = queryset.filter(event_id=event_id)
            if filters.types:
                type_ids = _resolve_filter_id_list(filters.types, "event type")
                queryset = queryset.filter(event__event_type_id__in=type_ids)
            if filters.statuses:
                status_slugs = [status.value for status in filters.statuses]
                queryset = queryset.filter(event__status__slug__in=status_slugs)
            if filters.start_date:
                queryset = queryset.filter(event__request__date__gte=filters.start_date)
            if filters.end_date:
                queryset = queryset.filter(event__request__date__lte=filters.end_date)

        queryset = queryset.order_by(*service.ordering)

        return await service.get_connection(
            queryset=queryset,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def ambassadors_booked_on_date(
        self,
        info: strawberry.Info,
        on_date: str,
        tenant_id: strawberry.ID | None = None,
    ) -> List[strawberry.ID]:
        """Return Ambassador relay-encoded IDs already booked on the
        given date (any AmbassadorEvent.event.date == on_date).

        Powers the "⚠ Already on another shift that day" red chip in
        the InviteBAModal. The front-end calls this with the event
        date being scheduled and cross-references the returned set
        against the BA list so admins don't double-book a BA.

        `on_date` is an ISO date string (YYYY-MM-DD). Tolerates a
        full ISO datetime and slices to the date part.

        Includes BOTH approved (already accepted) and pending
        (invited-but-not-yet-responded) shifts so the chip catches
        the "this BA has 3 pending invites for the same day" case
        too, not just confirmed double-bookings.
        """
        from datetime import datetime
        from ambassadors import models as a_models

        raw = (on_date or "").strip()
        if not raw:
            return []
        # Accept "2026-05-10" or "2026-05-10T18:30:00Z" — pick the date.
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            target_date = parsed.date()
        except ValueError:
            try:
                target_date = datetime.strptime(raw[:10], "%Y-%m-%d").date()
            except ValueError:
                raise GraphQLError("on_date must be ISO format.")

        resolved_tenant_id: int | None = None
        if tenant_id not in (None, ""):
            try:
                resolved_tenant_id = resolve_id_to_int(tenant_id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid tenant id.")

        # Tenant gate: an admin may probe any/all tenants' bookings (the chip
        # powers the cross-tenant InviteBAModal); a client/non-admin is
        # constrained to the tenant(s) they belong to so they can't enumerate
        # which BAs are booked across other brands — including the no-arg
        # variant that otherwise spans every tenant. A supplied foreign
        # tenant_id intersects to empty (-> []) rather than leaking.
        from utils.graphql.permissions import (
            resolve_request_user_access,
            _is_admin_access,
        )

        user = info.context.request.user
        _rs, _st, _su, _em = await resolve_request_user_access(user)
        is_admin = _is_admin_access(_rs, _st, _su, _em)

        def _fetch() -> List[strawberry.ID]:
            # Only the ambassador FK id is needed, and it's a LOCAL column on
            # AmbassadorEvent — read it directly with values_list. (The old
            # code paired select_related("event") with .only("ambassador__id"),
            # which defers `event` while traversing it → Django raises
            # "cannot be both deferred and traversed using select_related".)
            # event__date / event__tenant_id are WHERE-clause joins, no
            # select_related required.
            qs = a_models.AmbassadorEvent.objects.filter(event__date=target_date)
            if not is_admin:
                # Non-admin: hard-constrain to the caller's own tenant(s).
                allowed_tenant_ids = set(
                    user.tenanted_users.filter(is_active=True).values_list(
                        "tenant_id", flat=True
                    )
                )
                qs = qs.filter(event__tenant_id__in=allowed_tenant_ids)
            if resolved_tenant_id is not None:
                qs = qs.filter(event__tenant_id=resolved_tenant_id)
            seen: set[str] = set()
            out: list[strawberry.ID] = []
            # The front-end's row.id comparison matches the int pk (as a
            # string); return distinct ambassador ids in encounter order.
            for amb_id in qs.values_list("ambassador_id", flat=True):
                if not amb_id:
                    continue
                key = str(amb_id)
                if key in seen:
                    continue
                seen.add(key)
                out.append(strawberry.ID(key))
            return out

        # Non-thread-sensitive: this read now runs after an async role
        # resolution (resolve_request_user_access), and chaining two
        # thread-sensitive sync_to_async calls can deadlock the single shared
        # executor under the test harness. A fresh worker thread is safe for a
        # read-only ORM query.
        #
        # fresh_db_connection is REQUIRED here: the non-thread-sensitive pool
        # thread keeps its thread-local DB connection across requests, and
        # Django's request-lifecycle cleanup never reaches it — so a connection
        # the server later closes (Cloud SQL idle timeout) gets reused and
        # raises "the connection is closed" (which surfaced as the BA-assign
        # "This section couldn't load" error). The wrapper forces a fresh
        # connection per call.
        from utils.db import fresh_db_connection

        return await sync_to_async(
            fresh_db_connection(_fetch), thread_sensitive=False
        )()


@strawberry.type
class AmbassadorManagementQueries:
    """Queries for managing ambassadors and invitations (client/spark-admin only)."""

    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def sent_invitations(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        filters: inputs.AmbassadorInvitationFiltersInput | None = None,
    ) -> CountableConnection[types.AmbassadorInvitationType]:
        """Get sent invitations for a tenant (client/spark-admin only)."""
        from .services import AmbassadorInvitationQueriesService

        service = AmbassadorInvitationQueriesService()
        return await service.get_sent_invitations(
            info=info,
            first=first,
            after=after,
            last=last,
            before=before,
            filters=filters,
        )

    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def available_ambassadors(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        filters: inputs.AmbassadorFiltersInput | None = None,
    ) -> CountableConnection[types.Ambassador]:
        """Get available ambassadors for a tenant (client/spark-admin only)."""
        from .services import AmbassadorQueriesService

        service = AmbassadorQueriesService()
        return await service.get_available_ambassadors(
            info=info,
            first=first,
            after=after,
            last=last,
            before=before,
            filters=filters,
        )

    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def pending_ambassadors(
        self,
        info: strawberry.Info,
        first: int | None = 50,
        after: str | None = None,
    ) -> CountableConnection[types.Ambassador]:
        """Ambassadors waiting for admin approval — newest first.

        Pulls every Ambassador with is_active=False whose user is
        also active (i.e. signed up but not yet approved). Admin
        front-end uses this to render the Pending queue on /people.
        """
        from utils.graphql.relay import connection_from_queryset_async
        from ambassadors.models import Ambassador as AmbassadorModel

        qs = (
            AmbassadorModel.objects.filter(
                is_active=False,
                user__is_active=True,
            )
            .select_related("user")
            .order_by("-created_at")
        )
        return await connection_from_queryset_async(
            qs, first=first, after=after, last=None, before=None
        )

    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def invited_groups_by_job(
        self,
        info: strawberry.Info,
        filters: inputs.AmbassadorGroupFiltersInput | None = None,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
    ) -> CountableConnection[types.AmbassadorGroup]:
        """Get groups that include ambassadors invited to a given job."""
        from .services import AmbassadorInvitationQueriesService

        service = AmbassadorInvitationQueriesService()
        return await service.get_invited_groups_by_job(
            info=info,
            filters=filters,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def active_ambassadors(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: inputs.ActiveAmbassadorFiltersInput | None = None,
    ) -> CountableConnection[types.Ambassador]:
        """Get all active ambassadors (client/spark-admin only)."""
        from .services import AmbassadorQueriesService

        service = AmbassadorQueriesService()
        return await service.get_active_ambassadors(
            info=info,
            first=first,
            after=after,
            last=last,
            before=before,
            q=q,
            filters=filters,
        )

    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def ambassadors(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
        filters: inputs.AmbassadorFiltersInput | None = None,
    ) -> CountableConnection[types.Ambassador]:
        """List ambassadors with filters for status, rating, name, email, address and about_me."""
        from .services import AmbassadorQueriesService

        service = AmbassadorQueriesService()
        return await service.get_ambassadors(
            info=info,
            first=first,
            after=after,
            last=last,
            before=before,
            q=q,
            filters=filters,
        )

    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def ambassador(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
    ) -> types.Ambassador | None:
        """Get a single ambassador by id or uuid (client/spark-admin only)."""
        from .services import AmbassadorQueriesService

        if not id and not uuid:
            raise GraphQLError("Either id or uuid must be provided")

        service = AmbassadorQueriesService()

        try:
            if id:
                ambassador = await sync_to_async(
                    models.Ambassador.objects.select_related(
                        "user", "location", "location__state"
                    ).get
                )(id=id)
            else:
                ambassador = await sync_to_async(
                    models.Ambassador.objects.select_related(
                        "user", "location", "location__state"
                    ).get
                )(uuid=uuid)
            return ambassador
        except models.Ambassador.DoesNotExist:
            return None


@strawberry.type
class AmbassadorProfileQueries:
    """Aggregate query for full ambassador profile."""

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def ambassador_profile(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: strawberry.ID | None = None,
        user_id: strawberry.ID | None = None,
    ) -> types.AmbassadorProfile | None:
        """Return ambassador profile with related data in a single query."""
        if id is None and uuid is None and user_id is None:
            raise GraphQLError("Either id, uuid, or user_id must be provided")

        if id is not None:
            filters = {"id": id}
        elif uuid is not None:
            filters = {"uuid": uuid}
        else:
            try:
                resolved_user_id = resolve_id_to_int(user_id)
            except (TypeError, ValueError, GraphQLError) as exc:
                raise GraphQLError("Invalid user ID.") from exc
            filters = {"user_id": resolved_user_id}

        try:
            ambassador = await models.Ambassador.objects.select_related(
                "user", "location", "location__state"
            ).aget(
                **filters
            )
        except models.Ambassador.DoesNotExist:
            return None

        ambassador_id = ambassador.id

        async def fetch_reviews():
            queryset = models.AmbassadorReview.objects.filter(
                ambassador_id=ambassador_id
            )
            return await sync_to_async(list)(queryset)

        async def fetch_files():
            queryset = models.AmbassadorFile.objects.select_related("file_type").filter(
                ambassador_id=ambassador_id
            )
            return await sync_to_async(list)(queryset)

        async def fetch_traits():
            queryset = models.AmbassadorTrait.objects.filter(
                ambassador_id=ambassador_id
            )
            return await sync_to_async(list)(queryset)

        async def fetch_skills():
            queryset = models.AmbassadorSkill.objects.select_related("skill").filter(
                ambassador_id=ambassador_id
            )
            return await sync_to_async(list)(queryset)

        async def fetch_notes():
            queryset = models.AmbassadorNote.objects.filter(ambassador_id=ambassador_id)
            return await sync_to_async(list)(queryset)

        async def fetch_work_history():
            queryset = models.AmbassadorWorkHistory.objects.filter(
                ambassador_id=ambassador_id
            )
            return await sync_to_async(list)(queryset)

        (
            reviews,
            files,
            traits,
            skills,
            notes,
            work_history,
        ) = await asyncio.gather(
            fetch_reviews(),
            fetch_files(),
            fetch_traits(),
            fetch_skills(),
            fetch_notes(),
            fetch_work_history(),
        )

        return types.AmbassadorProfile(
            ambassador=ambassador,
            reviews=reviews,
            files=files,
            traits=traits,
            skills=skills,
            notes=notes,
            work_history=work_history,
        )


# ---------------------------------------------------------------------------
# Gig-history aggregation + admin TALENT profile detail.
#
# These two resolvers are the "ambassador-events aggregation" the web
# "Search talent" page was waiting on, plus the openable admin profile
# pop-up. Both are clients-schema surfaces (IsClientOrSparkAdmin).
# ---------------------------------------------------------------------------
def _gig_rows_for_ambassador(ambassador_id: int, tenant_id: int | None) -> list:
    """Build the gig-history rows for a BA from AmbassadorEvent -> Event.

    Mirrors the EarningsShiftRow aggregation precedent. When `tenant_id`
    is given the history is scoped to that tenant's gigs (the admin view);
    when None it spans all of the BA's gigs (used by self-serve mobile if
    wired later). Synchronous — call via sync_to_async.
    """
    from datetime import datetime, timezone as _tz

    qs = (
        models.AmbassadorEvent.objects.select_related(
            "event",
            "event__retailer",
            "event__state",
            "event__request",
            "event__request__client",
        )
        .filter(ambassador_id=ambassador_id)
    )
    if tenant_id is not None:
        qs = qs.filter(event__tenant_id=tenant_id)
    qs = qs.order_by("-event__date")

    now = datetime.now(_tz.utc)
    rows: list[types.GigHistoryRow] = []
    for ae in qs:
        ev = ae.event
        retailer = getattr(ev, "retailer", None)
        request = getattr(ev, "request", None)
        client = getattr(request, "client", None) if request else None
        brand_name = (
            (getattr(client, "name", None) if client else None)
            or (getattr(request, "client_name", None) if request else None)
            or (getattr(retailer, "name", None) if retailer else None)
        )
        venue = (
            getattr(ev, "name", None)
            or (getattr(retailer, "name", None) if retailer else None)
        )
        state_obj = getattr(ev, "state", None)
        state_code = getattr(state_obj, "code", None) if state_obj else None
        ev_date = getattr(ev, "date", None)
        is_approved = bool(getattr(ae, "is_approved", False))
        if not is_approved:
            status = "pending"
        elif ev_date is not None and ev_date < now:
            status = "worked"
        else:
            status = "upcoming"
        rows.append(
            types.GigHistoryRow(
                ambassador_event_uuid=strawberry.ID(str(ae.uuid)),
                event_uuid=strawberry.ID(str(ev.uuid)),
                brand_name=brand_name,
                venue=venue,
                city=None,
                state_code=state_code,
                date=ev_date.isoformat() if ev_date else None,
                is_approved=is_approved,
                status=status,
            )
        )
    return rows


def _ba_stats(ambassador_id: int, tenant_id: int | None) -> dict:
    """Compute rating avg/count, all-time approved jobs count, and the
    on-time rate for a BA. Synchronous — call via sync_to_async.

    on_time_rate mirrors the mobile reliability math: a completed shift
    (event.start_time in the past) counts as on-time when the BA's
    earliest clock-in for that event is <= start_time + 10-minute grace.
    Scoped to the tenant's gigs when tenant_id is supplied.
    """
    from datetime import datetime, timedelta, timezone as _tz

    # ---- ratings (mean of all ratings, tenant-scoped when applicable) ----
    rating_qs = models.AmbassadorRating.objects.filter(ambassador_id=ambassador_id)
    if tenant_id is not None:
        rating_qs = rating_qs.filter(tenant_id=tenant_id)
    scores = list(rating_qs.values_list("score", flat=True))
    rating_count = len(scores)
    rating_average = round(sum(scores) / rating_count, 1) if rating_count else 0.0

    # ---- jobs: approved AmbassadorEvent rows (all-time) ----
    jobs_qs = models.AmbassadorEvent.objects.filter(
        ambassador_id=ambassador_id, is_approved=True
    )
    if tenant_id is not None:
        jobs_qs = jobs_qs.filter(event__tenant_id=tenant_id)
    jobs_count = jobs_qs.count()

    # ---- on-time rate over completed shifts ----
    now = datetime.now(_tz.utc)
    completed = (
        models.AmbassadorEvent.objects.select_related("event")
        .filter(
            ambassador_id=ambassador_id,
            is_approved=True,
            event__start_time__lt=now,
        )
    )
    if tenant_id is not None:
        completed = completed.filter(event__tenant_id=tenant_id)
    completed_events = {ae.event_id: ae.event for ae in completed if ae.event_id}
    on_time_rate: float | None = None
    if completed_events:
        # Earliest clock-in per event for this BA. Mirror the mobile
        # rating-summary precedent: a clock-in Attendance is identified
        # by source__name == "clock_in".
        earliest: dict[int, object] = {}
        for row in models.Attendance.objects.filter(
            ambassador_id=ambassador_id,
            event_id__in=list(completed_events.keys()),
            source__name="clock_in",
        ).values("event_id", "clock_time"):
            eid = row["event_id"]
            ct = row["clock_time"]
            if ct is None:
                continue
            if eid not in earliest or ct < earliest[eid]:
                earliest[eid] = ct
        on_time = 0
        measured = 0
        grace = timedelta(minutes=10)
        for eid, ev in completed_events.items():
            start = getattr(ev, "start_time", None)
            ci = earliest.get(eid)
            if start is None or ci is None:
                continue
            measured += 1
            if ci <= start + grace:
                on_time += 1
        if measured:
            on_time_rate = round((on_time / measured) * 100.0, 1)

    return {
        "rating_average": rating_average,
        "rating_count": rating_count,
        "jobs_count": jobs_count,
        "on_time_rate": on_time_rate,
    }


@strawberry.type
class AmbassadorGigHistoryQueries:
    """The ambassador-events aggregation — a BA's gig history."""

    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def ambassador_gig_history(
        self,
        info: strawberry.Info,
        ambassador_uuid: strawberry.ID,
        tenant_id: strawberry.ID | None = None,
        tenant_uuid: strawberry.ID | None = None,
    ) -> list[types.GigHistoryRow]:
        """Aggregate a BA's AmbassadorEvent history into gig rows.

        Tenant-scoped exactly like recapEventOptions: resolve the active
        tenant, return an EMPTY list when none is in scope, never
        all-tenants. Clients resolve to their own tenant.
        """
        from events.queries import EventQueriesService

        service = EventQueriesService()
        try:
            resolved_tenant_id = await service.resolve_tenant_id(
                info, tenant_id=tenant_id, tenant_uuid=tenant_uuid
            )
        except GraphQLError:
            resolved_tenant_id = None
        if not resolved_tenant_id:
            return []

        try:
            ambassador = await models.Ambassador.objects.aget(uuid=ambassador_uuid)
        except models.Ambassador.DoesNotExist:
            return []

        # Only expose a BA reachable in this tenant (worked/assigned a
        # gig here) — same rule the chat recipient list uses.
        reachable = await models.AmbassadorEvent.objects.filter(
            ambassador_id=ambassador.id, event__tenant_id=resolved_tenant_id
        ).aexists()
        if not reachable:
            return []

        return await sync_to_async(_gig_rows_for_ambassador)(
            ambassador.id, resolved_tenant_id
        )


@strawberry.type
class TalentProfileDetailQueries:
    """Openable admin/client TALENT profile pop-up (clients schema)."""

    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def ambassador_profile_detail(
        self,
        info: strawberry.Info,
        ambassador_uuid: strawberry.ID,
        tenant_id: strawberry.ID | None = None,
        tenant_uuid: strawberry.ID | None = None,
    ) -> types.AmbassadorProfileDetail | None:
        """Full BA profile for the admin pop-up: headshot, bio,
        college/in_college, email + phone, event photos, résumé, gig
        history, and rating/on-time/jobs stats.

        Tenant-scoped like recapEventOptions — resolve the active tenant,
        return None when none is in scope, and only surface a BA reachable
        in that tenant (worked/assigned a gig there).
        """
        from events.queries import EventQueriesService

        service = EventQueriesService()
        try:
            resolved_tenant_id = await service.resolve_tenant_id(
                info, tenant_id=tenant_id, tenant_uuid=tenant_uuid
            )
        except GraphQLError:
            resolved_tenant_id = None
        if not resolved_tenant_id:
            return None

        try:
            ambassador = await models.Ambassador.objects.select_related(
                "user", "location", "location__state"
            ).aget(uuid=ambassador_uuid)
        except models.Ambassador.DoesNotExist:
            return None

        # A BA is "reachable" in this tenant if they've worked/been booked on
        # one of its events (AmbassadorEvent) OR — so an admin can open a
        # not-yet-booked applicant's profile from the Jobs page — if they have
        # a JobApplication on one of this tenant's jobs (task #256). All other
        # auth/tenant scoping is unchanged.
        reachable = await models.AmbassadorEvent.objects.filter(
            ambassador_id=ambassador.id, event__tenant_id=resolved_tenant_id
        ).aexists()
        if not reachable:
            from jobs.models import JobApplication

            reachable = await JobApplication.objects.filter(
                ambassador_id=ambassador.id, tenant_id=resolved_tenant_id
            ).aexists()
        if not reachable:
            return None

        async def fetch_photos():
            return await sync_to_async(list)(
                models.AmbassadorPhoto.objects.filter(ambassador_id=ambassador.id)
            )

        from ambassadors.reliability import compute_reliability

        photos, gig_history, stats, reliability = await asyncio.gather(
            fetch_photos(),
            sync_to_async(_gig_rows_for_ambassador)(
                ambassador.id, resolved_tenant_id
            ),
            sync_to_async(_ba_stats)(ambassador.id, resolved_tenant_id),
            # Reliability is shift-history-wide (completed/dropped/claimed
            # across all tenants), not tenant-scoped — a BA's dependability is
            # a property of the BA, so it reads the same from any admin.
            sync_to_async(compute_reliability)(ambassador.user_id),
        )

        user = ambassador.user
        full_name = " ".join(
            filter(
                None,
                [
                    getattr(user, "first_name", "") if user else "",
                    getattr(user, "last_name", "") if user else "",
                ],
            )
        ).strip() or (getattr(user, "email", "") if user else "") or ""

        return types.AmbassadorProfileDetail(
            ambassador=ambassador,
            full_name=full_name,
            email=(getattr(user, "email", None) if user else None),
            phone=ambassador.phone,
            bio=ambassador.bio or (ambassador.about_me or ""),
            college=ambassador.college or "",
            in_college=bool(ambassador.in_college),
            headshot_url=public_url(ambassador.headshot)
            if ambassador.headshot
            else None,
            resume_url=public_url(ambassador.resume)
            if ambassador.resume
            else None,
            photos=photos,
            gig_history=gig_history,
            rating_average=stats["rating_average"],
            rating_count=stats["rating_count"],
            jobs_count=stats["jobs_count"],
            on_time_rate=stats["on_time_rate"],
            reliability_score=reliability.score,
            reliability_label=reliability.label,
            completed_shifts=reliability.completed,
            dropped_shifts=reliability.dropped,
            claimed_shifts=reliability.claimed,
        )


@strawberry.type
class AmbassadorReviewQueries:
    """Queries for ambassador reviews."""

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def ambassador_reviews(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        filters: inputs.AmbassadorReviewFiltersInput | None = None,
    ) -> CountableConnection[types.AmbassadorReviewType]:
        """Get ambassador reviews with filters (authenticated users only)."""
        from .services import AmbassadorReviewQueriesService

        service = AmbassadorReviewQueriesService()
        return await service.get_ambassador_reviews(
            info=info,
            first=first,
            after=after,
            last=last,
            before=before,
            filters=filters,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def ambassador_review(
        self,
        info: strawberry.Info,
        review_id: strawberry.ID,
    ) -> types.AmbassadorReviewType | None:
        """Get a single ambassador review by ID (authenticated users only)."""
        from .models import AmbassadorReview

        try:

            @sync_to_async
            def get_review():
                return AmbassadorReview.objects.select_related(
                    "ambassador", "client", "tenant"
                ).get(pk=int(review_id))

            return await get_review()
        except (AmbassadorReview.DoesNotExist, ValueError, TypeError):
            return None

    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def ambassador_ratings(
        self,
        info: strawberry.Info,
        ambassador_id: strawberry.ID,
    ) -> List[types.AmbassadorRatingType]:
        """Star ratings left for one BA, newest first.

        Visibility: Ignite (spark-admin) sees every rating; a client only
        sees the ratings they themselves submitted. Client ratings stay
        private to Ignite and are never shown to other clients.
        """
        user = info.context.request.user
        try:
            ba_id = resolve_id_to_int(ambassador_id)
        except (TypeError, ValueError, GraphQLError):
            raise GraphQLError("Invalid ambassador ID.")

        # Effective role via the shared, email-aware resolver: any Ignite
        # admin (staff / superuser / spark-admin / @igniteproductions.co) is
        # treated as spark-admin so they see every rating; otherwise fall
        # through to the real role (clients see only their own, below).
        from utils.graphql.permissions import (
            resolve_request_user_access,
            _is_admin_access,
        )

        _rs, _st, _su, _em = await resolve_request_user_access(user)
        slug = (
            "spark-admin"
            if _is_admin_access(_rs, _st, _su, _em)
            else ("client" if _rs == "client" else "")
        )

        @sync_to_async
        def fetch():
            qs = models.AmbassadorRating.objects.select_related(
                "created_by", "event", "tenant"
            ).filter(ambassador_id=ba_id)
            if slug != "spark-admin":
                # Clients only ever see their own ratings.
                qs = qs.filter(created_by=user)
            return list(qs.order_by("-created_at"))

        return await fetch()


@strawberry.type
class AmbassadorNoteQueries:
    """Queries for ambassador notes."""

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def ambassador_notes(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        filters: inputs.AmbassadorNoteFiltersInput | None = None,
    ) -> CountableConnection[types.AmbassadorNoteType]:
        """Get ambassador notes with filters (authenticated users only)."""
        from .services import AmbassadorNoteQueriesService

        service = AmbassadorNoteQueriesService()
        return await service.get_ambassador_notes(
            info=info,
            first=first,
            after=after,
            last=last,
            before=before,
            filters=filters,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def ambassador_note(
        self,
        info: strawberry.Info,
        note_id: strawberry.ID,
    ) -> types.AmbassadorNoteType | None:
        """Get a single ambassador note by ID (authenticated users only)."""
        from .models import AmbassadorNote

        try:

            @sync_to_async
            def get_note():
                return AmbassadorNote.objects.select_related(
                    "ambassador", "tenant", "created_by", "updated_by"
                ).get(pk=int(note_id))

            return await get_note()
        except (AmbassadorNote.DoesNotExist, ValueError, TypeError):
            return None


@strawberry.type
class SkillQueries:
    """Queries for skills."""

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def skills(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        filters: inputs.SkillFiltersInput | None = None,
    ) -> CountableConnection[types.SkillType]:
        """Get skills with filters (authenticated users only)."""
        from .services import SkillQueriesService

        service = SkillQueriesService()
        return await service.get_skills(
            info=info,
            first=first,
            after=after,
            last=last,
            before=before,
            filters=filters,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def skill(
        self,
        info: strawberry.Info,
        skill_id: strawberry.ID | None = None,
        skill_uuid: strawberry.ID | None = None,
    ) -> types.SkillType | None:
        """Get a single skill by ID or UUID (authenticated users only)."""
        from .models import Skill

        if skill_id is None and skill_uuid is None:
            return None

        try:
            if skill_uuid is not None:

                @sync_to_async
                def get_by_uuid():
                    return Skill.objects.get(uuid=str(skill_uuid))

                skill = await get_by_uuid()
            else:
                skill = await Skill.objects._by_id(skill_id)
            return skill
        except (Skill.DoesNotExist, ValueError, TypeError):
            return None


@strawberry.type
class AmbassadorSkillQueries:
    """Queries for ambassador skills."""

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def ambassador_skills(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        filters: inputs.AmbassadorSkillFiltersInput | None = None,
    ) -> CountableConnection[types.AmbassadorSkillType]:
        """Get ambassador skills with filters (authenticated users only)."""
        from .services import AmbassadorSkillQueriesService

        service = AmbassadorSkillQueriesService()
        return await service.get_ambassador_skills(
            info=info,
            first=first,
            after=after,
            last=last,
            before=before,
            filters=filters,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def ambassador_skill(
        self,
        info: strawberry.Info,
        ambassador_skill_id: strawberry.ID,
    ) -> types.AmbassadorSkillType | None:
        """Get a single ambassador skill by ID (authenticated users only)."""
        from .models import AmbassadorSkill

        try:
            ambassador_skill = await AmbassadorSkill.objects._by_id(ambassador_skill_id)
            return ambassador_skill
        except (AmbassadorSkill.DoesNotExist, ValueError, TypeError):
            return None


class AttendanceTypeQueriesService(BaseQueriesService):
    """Service for attendance type queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.AttendanceType


class AttendanceStatusQueriesService(AmbassadorsTenantQueriesService):
    """Service for attendance status queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.AttendanceStatus


class SourceQueriesService(BaseQueriesService):
    """Service for source queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Source


class GroupTypeQueriesService(BaseQueriesService):
    """Service for group type queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.GroupType


class AmbassadorGroupQueriesService(BaseQueriesService):
    """Service for ambassador group queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.AmbassadorGroup

    def apply_filters(
        self,
        queryset: QuerySet,
        filters: inputs.AmbassadorGroupFiltersInput | None,
    ) -> QuerySet:
        """Apply ambassador group filters to queryset."""
        if not filters:
            return queryset

        job_id = getattr(filters, "job_id", None)
        if job_id:
            try:
                job_id = resolve_id_to_int(job_id)
                queryset = queryset.filter(job_links__job_id=job_id).distinct()
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid job ID.")
        job_uuid = getattr(filters, "job_uuid", None)
        if job_uuid:
            queryset = queryset.filter(job_links__job__uuid=job_uuid).distinct()

        return queryset


class AttendanceQueriesService(BaseQueriesService):
    """Service for attendance queries."""

    def get_model(self) -> Model:
        """Get the model for the service."""
        return models.Attendance

    def get_filtered_queryset(
        self, tenant_id: int | None = None, q: str | None = None
    ) -> QuerySet:
        """
        Override default filtering to avoid name__icontains lookups.

        Attendance has no name field, so we just return the base queryset.
        """
        return self.get_queryset()

    def apply_filters(
        self,
        queryset: QuerySet,
        filters: inputs.AttendanceFiltersInput | None,
    ) -> QuerySet:
        """Apply attendance filters to queryset."""
        if not filters:
            return queryset

        if filters.tenant_id:
            try:
                tenant_id = resolve_id_to_int(filters.tenant_id)
                queryset = queryset.filter(tenant_id=tenant_id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid tenant ID.")
        if filters.job_id:
            try:
                job_id = resolve_id_to_int(filters.job_id)
                queryset = queryset.filter(job_id=job_id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid job ID.")
        if filters.ambassador_job_id:
            try:
                ambassador_job_id = resolve_id_to_int(filters.ambassador_job_id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid ambassador job ID.")
            ambassador_job_match = job_models.AmbassadorJob.objects.filter(
                id=ambassador_job_id,
                ambassador_id=OuterRef("ambassador_id"),
                job_id=OuterRef("job_id"),
            )
            queryset = queryset.annotate(
                ambassador_job_match=Exists(ambassador_job_match)
            ).filter(ambassador_job_match=True)
        if filters.event_id:
            try:
                event_id = resolve_id_to_int(filters.event_id)
                queryset = queryset.filter(event_id=event_id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid event ID.")
        if filters.attendance_status_id:
            try:
                attendance_status_id = resolve_id_to_int(filters.attendance_status_id)
                queryset = queryset.filter(attendance_status_id=attendance_status_id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid attendance status ID.")
        if filters.source_id:
            try:
                source_id = resolve_id_to_int(filters.source_id)
                queryset = queryset.filter(source_id=source_id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid source ID.")
        if filters.attendace_type_id:
            try:
                attendace_type_id = resolve_id_to_int(filters.attendace_type_id)
                queryset = queryset.filter(attendace_type_id=attendace_type_id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid attendance type ID.")
        return queryset


@strawberry.type
class AttendanceQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def attendance_types(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.AttendanceType]:
        service = AttendanceTypeQueriesService()
        await service.get_user(info)
        return await service.get_connection(
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def attendance_type(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> types.AttendanceType | None:
        try:
            service = AttendanceTypeQueriesService()
            await service.get_user(info)
            return await service.get_record(id)
        except GraphQLError:
            return None

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def attendance_statuses(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.AttendanceStatus]:
        service = AttendanceStatusQueriesService()
        tenant_id = await service.resolve_query_tenant_id(info)
        return await service.get_connection(
            tenant_id=tenant_id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def attendance_status(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> types.AttendanceStatus | None:
        try:
            service = AttendanceStatusQueriesService()
            tenant_id = await service.resolve_query_tenant_id(info)
            return await service.get_record(id, tenant_id)
        except GraphQLError:
            return None

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def sources(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.Source]:
        service = SourceQueriesService()
        await service.get_user(info)
        return await service.get_connection(
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def source(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> types.Source | None:
        try:
            service = SourceQueriesService()
            await service.get_user(info)
            return await service.get_record(id)
        except GraphQLError:
            return None

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def attendances(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        filters: inputs.AttendanceFiltersInput | None = None,
    ) -> CountableConnection[types.Attendance]:
        service = AttendanceQueriesService()
        await service.get_user(info)
        queryset = service.get_queryset()
        queryset = service.apply_filters(queryset, filters)
        queryset = queryset.order_by(*service.ordering)

        return await service.get_connection(
            queryset=queryset,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def attendance(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> types.Attendance | None:
        try:
            service = AttendanceQueriesService()
            await service.get_user(info)
            return await service.get_record(id)
        except GraphQLError:
            return None


@strawberry.type
class GroupTypeQueries:
    """Queries for group types."""

    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def group_types(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        filters: inputs.GroupTypeFiltersInput | None = None,
    ) -> CountableConnection[types.GroupType]:
        """Get group types with filters (authenticated users only)."""
        service = GroupTypeQueriesService()
        await service.get_user(info)

        q = filters.search if filters else None
        queryset = service.get_ordered_queryset(q=q)

        return await service.get_connection(
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
            queryset=queryset,
        )

    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def group_type(
        self,
        info: strawberry.Info,
        group_type_id: strawberry.ID,
    ) -> types.GroupType | None:
        """Get a single group type by ID (authenticated users only)."""
        try:
            service = GroupTypeQueriesService()
            await service.get_user(info)
            return await service.get_record(group_type_id)
        except GraphQLError:
            return None


@strawberry.type
class AmbassadorGroupQueries:
    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def ambassador_groups(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        filters: inputs.AmbassadorGroupFiltersInput | None = None,
    ) -> CountableConnection[types.AmbassadorGroup]:
        """Get ambassador groups with filters (client/spark-admin only)."""
        service = AmbassadorGroupQueriesService()
        tenant_id = await service.resolve_query_tenant_id(info, filters=filters)
        await service.get_user(info)

        q = filters.search if filters else None
        queryset = service.get_ordered_queryset(tenant_id=tenant_id, q=q)
        queryset = service.apply_filters(queryset, filters)

        return await service.get_connection(
            tenant_id=tenant_id,
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
            queryset=queryset,
        )

    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def ambassador_group(
        self,
        info: strawberry.Info,
        group_id: strawberry.ID,
    ) -> types.AmbassadorGroup | None:
        """Get a single ambassador group by ID (client/spark-admin only)."""
        try:
            service = AmbassadorGroupQueriesService()
            tenant_id = await service.resolve_query_tenant_id(info)
            await service.get_user(info)
            return await service.get_record(id=group_id, tenant_id=tenant_id)
        except GraphQLError:
            return None


@strawberry.type
class AttendanceMobileQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def attendance_types(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> CountableConnection[types.AttendanceType]:
        service = AttendanceTypeQueriesService()
        await service.get_user(info)
        return await service.get_connection(
            q=q,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def attendance_type(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> types.AttendanceType | None:
        try:
            service = AttendanceTypeQueriesService()
            await service.get_user(info)
            return await service.get_record(id)
        except GraphQLError:
            return None

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def attendances_mobile(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        filters: inputs.AttendanceFiltersInput | None = None,
    ) -> CountableConnection[types.Attendance]:
        service = AttendanceQueriesService()
        user = await service.get_user(info)

        queryset = service.get_queryset().filter(ambassador__user=user)

        queryset = service.apply_filters(queryset, filters)
        queryset = queryset.order_by(*service.ordering)

        return await service.get_connection(
            queryset=queryset,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def attendance_mobile(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> types.Attendance | None:
        service = AttendanceQueriesService()
        user = await service.get_user(info)

        try:
            return await sync_to_async(service.get_model().objects.get)(
                id=id,
                ambassador__user=user,
            )
        except GraphQLError:
            return None
        except service.get_model().DoesNotExist:
            return None


@strawberry.type
class PendingExtensionItem:
    uuid: strawberry.ID
    ba_name: str
    ba_uuid: str | None = None
    event_uuid: str | None = None
    venue: str = ""
    minutes_requested: int = 0
    reason: str = ""
    created_at: str = ""


@strawberry.type
class ShiftExtensionAdminQueries:
    """Admin (Ignite) view of pending mid-shift extension requests — drives the
    notification center's actionable Approve / Decline list."""

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def pending_shift_extensions(
        self, info: strawberry.Info, limit: int = 50
    ) -> list[PendingExtensionItem]:
        from ambassadors.extensions import user_is_ignite_admin

        user = info.context.request.user
        if not user_is_ignite_admin(user):
            return []
        capped = max(1, min(int(limit or 50), 200))

        def _fetch() -> list:
            rows = list(
                models.ShiftExtensionRequest.objects.select_related(
                    "event", "ambassador", "ambassador__user"
                )
                .filter(status="pending")
                .order_by("-created_at")[:capped]
            )
            out: list = []
            for ext in rows:
                ba = ext.ambassador
                ba_user = getattr(ba, "user", None)
                ba_name = (
                    f"{getattr(ba_user, 'first_name', '') or ''} "
                    f"{getattr(ba_user, 'last_name', '') or ''}"
                ).strip() or "A BA"
                out.append(
                    PendingExtensionItem(
                        uuid=strawberry.ID(str(ext.uuid)),
                        ba_name=ba_name,
                        ba_uuid=str(getattr(ba, "uuid", "")) if ba else None,
                        event_uuid=(
                            str(getattr(ext.event, "uuid", ""))
                            if ext.event_id else None
                        ),
                        venue=getattr(ext.event, "name", None) or "their shift",
                        minutes_requested=ext.minutes_requested,
                        reason=ext.reason or "",
                        created_at=ext.created_at.isoformat() if ext.created_at else "",
                    )
                )
            return out

        return await sync_to_async(_fetch)()


@strawberry.type
class NotificationQueries:
    """Mobile Notifications inbox — the per-user log of pushes we've sent.

    Strictly self-scoped: every row is filtered to the JWT user, so there's no
    cross-user exposure. Powers the inbox list + the unread badge.
    """

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def my_notifications(
        self,
        info: strawberry.Info,
        limit: int = 50,
    ) -> list[types.NotificationItem]:
        import json as _json

        user = info.context.request.user
        if not getattr(user, "is_authenticated", False):
            return []
        capped = max(1, min(int(limit or 50), 100))

        def _fetch() -> list:
            rows = list(
                models.PushNotification.objects.filter(user=user).order_by(
                    "-created_at"
                )[:capped]
            )
            out: list = []
            for n in rows:
                out.append(
                    types.NotificationItem(
                        uuid=strawberry.ID(str(n.uuid)),
                        title=n.title or "",
                        body=n.body or "",
                        kind=n.kind or "",
                        data_json=(
                            _json.dumps(n.data) if n.data not in (None, {}) else None
                        ),
                        read=n.read_at is not None,
                        created_at=n.created_at.isoformat() if n.created_at else "",
                    )
                )
            return out

        return await sync_to_async(_fetch)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def my_unread_notification_count(self, info: strawberry.Info) -> int:
        user = info.context.request.user
        if not getattr(user, "is_authenticated", False):
            return 0
        return await sync_to_async(
            lambda: models.PushNotification.objects.filter(
                user=user, read_at__isnull=True
            ).count()
        )()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def my_push_preferences(
        self, info: strawberry.Info
    ) -> types.PushPreferences:
        """The signed-in BA's push opt-ins. A missing row = everything on."""
        user = info.context.request.user
        _defaults = types.PushPreferences(
            shift_offers=True, reminders=True, chat=True, pay=True, gigs=True
        )
        if not getattr(user, "is_authenticated", False):
            return _defaults

        def _fetch() -> types.PushPreferences:
            pref = models.PushPreference.objects.filter(user=user).first()
            if pref is None:
                return _defaults
            return types.PushPreferences(
                shift_offers=pref.shift_offers,
                reminders=pref.reminders,
                chat=pref.chat,
                pay=pref.pay,
                gigs=pref.gigs,
            )

        return await sync_to_async(_fetch)()


# ---------------------------------------------------------------------------
# BA referral program — "Invite friends" surface (mobile)
# ---------------------------------------------------------------------------

@strawberry.type
class ReferralEntry:
    """One friend the requesting BA referred, with their stage.

    ``status`` is "signed_up" until the friend completes (clocks out of)
    their first shift, then "completed". Names come from the referred user's
    profile; the email is included so the referrer can tell friends apart
    before they fill in their name.
    """

    name: str
    email: str
    status: str
    signed_up_at: str
    first_shift_completed_at: str | None = None


@strawberry.type
class ReferralQueries:
    """Self-scoped referral lookups for the signed-in BA."""

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def my_referral_code(self, info: strawberry.Info) -> str:
        """The caller's stable invite code (created on first ask)."""
        from ambassadors.referrals import get_or_create_code

        user = info.context.request.user
        return await sync_to_async(get_or_create_code)(user)

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def my_referrals(self, info: strawberry.Info) -> List[ReferralEntry]:
        """Everyone the caller referred, newest first."""
        user = info.context.request.user

        def _fetch() -> list[ReferralEntry]:
            rows = (
                models.AmbassadorReferral.objects.filter(referrer=user)
                .select_related("referred")
                .order_by("-signed_up_at")
            )
            out: list[ReferralEntry] = []
            for r in rows:
                referred = r.referred
                name = (
                    " ".join(
                        part
                        for part in (
                            (getattr(referred, "first_name", "") or "").strip(),
                            (getattr(referred, "last_name", "") or "").strip(),
                        )
                        if part
                    ).strip()
                    or (getattr(referred, "email", "") or "")
                )
                out.append(
                    ReferralEntry(
                        name=name,
                        email=getattr(referred, "email", "") or "",
                        status=(
                            "completed"
                            if r.first_shift_completed_at
                            else "signed_up"
                        ),
                        signed_up_at=r.signed_up_at.isoformat(),
                        first_shift_completed_at=(
                            r.first_shift_completed_at.isoformat()
                            if r.first_shift_completed_at
                            else None
                        ),
                    )
                )
            return out

        return await sync_to_async(_fetch)()
