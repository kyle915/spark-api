"""Requests with no status, and tenants with no default status, across ALL tenants.

``WithDefaultAttribute.save()`` used to unflag ``is_default`` on every
tenant's rows whenever any tenant saved a default (fixed in #1249), so most
tenants lost their default RequestStatus / EventStatus. Requests created
without an explicit status then saved with ``status=NULL`` and never reached
Approvals, which lists ``pending`` only.

Report (always):
  * per tenant: request statuses, the flagged default (or none), the event
    status default, and the event type default (report only);
  * every live request with no status: code, created date, event date,
    source (plan / public spark-form / logged-in), and its events.

``--apply``:
  * flags each tenant's ``pending`` RequestStatus and EventStatus as the
    default when the tenant has none (``DEFAULT_STATUS_TEMPLATES`` seeds
    Pending as the default on every new tenant);
  * sets no-status requests whose event date is today or later (in the
    request's timezone) to Pending so they show in Approvals.
    Past and undated ones are listed and left alone.

Writes use queryset ``.update()`` (no post_save, no emails, no sheet mirror).
Nothing is approved. Dry-run unless ``--apply``.

    python manage.py audit_request_statuses [--tenant <slug>] [--apply]
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from events.demo_cancel import request_display_code
from events.models import (
    Event,
    EventStatus,
    EventType,
    FieldMarketingEvent,
    Request,
    RequestActivityLog,
    RequestStatus,
)
from tenants.dashboard.signals import _invalidate_dashboard_cache
from tenants.models import Tenant
from utils.tz import resolve_zoneinfo

DEFAULT_SLUG = "pending"
FALLBACK_ZONE = ZoneInfo("America/New_York")
MAX_LISTED = 300


def _resolve_tenant(raw: str) -> Tenant:
    for lookup in ("slug", "request_url_name"):
        tenant = Tenant.objects.filter(**{lookup: raw}).first()
        if tenant is not None:
            return tenant
    raise CommandError(f"No tenant with slug or request_url_name {raw!r}")


def event_day(request: Request) -> date | None:
    when = request.start_time or request.date
    if when is None:
        return None
    return when.astimezone(resolve_zoneinfo(request.timezone) or FALLBACK_ZONE).date()


def today_for(request: Request, now) -> date:
    return now.astimezone(resolve_zoneinfo(request.timezone) or FALLBACK_ZONE).date()


def request_source(request: Request, plan_request_ids: set[int]) -> str:
    if request.id in plan_request_ids:
        return "plan"
    if request.created_by_id is None:
        return "public spark-form"
    email = getattr(request.created_by, "email", "") or "?"
    return f"logged-in Create Request ({email})"


class Command(BaseCommand):
    help = "Report no-status requests + tenants without a default status; --apply restores Pending."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", default="", help="Tenant slug or request_url_name (blank = all)")
        parser.add_argument("--apply", action="store_true", help="Write (default: dry run).")

    def handle(self, *args, **opts):
        apply = bool(opts["apply"])
        tenant = _resolve_tenant(opts["tenant"]) if opts["tenant"] else None
        tenants = [tenant] if tenant else list(Tenant.objects.order_by("id"))
        now = timezone.now()
        w = self.stdout.write
        w("=" * 72)
        w(
            f"audit_request_statuses · {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'} · "
            f"scope={tenant.slug if tenant else 'ALL TENANTS'} · no emails"
        )
        w("=" * 72)

        restored = self._defaults(tenants, apply)
        moved = self._no_status(tenants, apply, now)

        w("\nSUMMARY")
        w(f"  Default statuses {'restored' if apply else 'to restore'}: {len(restored)}")
        for line in restored:
            w(f"    {line}")
        w(f"  Requests {'moved' if apply else 'to move'} to Pending: {moved}")
        if not apply:
            w("\nDRY-RUN — re-run with --apply to write.")

    # ── Defaults ──────────────────────────────────────────────────────

    def _defaults(self, tenants, apply: bool) -> list[str]:
        w = self.stdout.write
        w("\nDEFAULT STATUSES (per tenant)")
        restored: list[str] = []
        for t in tenants:
            statuses = list(RequestStatus.objects.filter(tenant=t).order_by("id"))
            req_default = next((s for s in statuses if s.is_default), None)
            ev_statuses = list(EventStatus.objects.filter(tenant=t).order_by("id"))
            ev_default = next((s for s in ev_statuses if s.is_default), None)
            type_default = EventType.objects.filter(tenant=t, is_default=True).first()
            w(
                f"  [{t.id}] {t.slug}: request statuses="
                f"{', '.join(s.slug or s.name for s in statuses) or '(none)'}"
                f" · request default={req_default.slug if req_default else 'NONE'}"
                f" · event status default={ev_default.slug if ev_default else 'NONE'}"
                f" · event type default={type_default.name if type_default else 'none (report only)'}"
            )
            for model, rows, current, label in (
                (RequestStatus, statuses, req_default, "request"),
                (EventStatus, ev_statuses, ev_default, "event"),
            ):
                if current is not None:
                    continue
                pending = next((s for s in rows if (s.slug or "").lower() == DEFAULT_SLUG), None)
                if pending is None:
                    w(f"      ! no '{DEFAULT_SLUG}' {label} status row — left without a default")
                    continue
                if apply:
                    model.objects.filter(pk=pending.pk).update(is_default=True)
                restored.append(f"[{t.id}] {t.slug}: {label} status default → {pending.slug} (#{pending.pk})")
        return restored

    # ── No-status requests ────────────────────────────────────────────

    def _no_status(self, tenants, apply: bool, now) -> int:
        w = self.stdout.write
        tenant_ids = [t.id for t in tenants]
        rows = list(
            Request.objects.filter(
                status__isnull=True, deleted_at__isnull=True, tenant_id__in=tenant_ids
            )
            .select_related("tenant", "timezone", "created_by")
            .order_by("tenant_id", "date", "id")
        )
        deleted = dict(
            Request.objects.filter(
                status__isnull=True, deleted_at__isnull=False, tenant_id__in=tenant_ids
            )
            .values("tenant_id")
            .annotate(n=Count("id"))
            .values_list("tenant_id", "n")
        )
        ids = [r.id for r in rows]
        plan_ids = set(
            FieldMarketingEvent.objects.filter(request_id__in=ids).values_list("request_id", flat=True)
        )
        events: dict[int, list[int]] = defaultdict(list)
        for rid, eid in Event.objects.filter(request_id__in=ids).values_list("request_id", "id"):
            events[rid].append(eid)

        by_tenant: dict[int, list[Request]] = defaultdict(list)
        for r in rows:
            by_tenant[r.tenant_id].append(r)

        w(f"\nREQUESTS WITH NO STATUS (live): {len(rows)}")
        moved = 0
        changed_tenants: set[int] = set()
        for t in tenants:
            items = by_tenant.get(t.id, [])
            if not items and not deleted.get(t.id):
                continue
            future = [r for r in items if (d := event_day(r)) is not None and d >= today_for(r, now)]
            past = [r for r in items if (d := event_day(r)) is not None and d < today_for(r, now)]
            undated = [r for r in items if event_day(r) is None]
            w(
                f"\n  [{t.id}] {t.slug}: {len(items)} live "
                f"({len(future)} today/future, {len(past)} past, {len(undated)} undated)"
                + (f" · {deleted[t.id]} soft-deleted (ignored)" if deleted.get(t.id) else "")
            )
            pending = RequestStatus.objects.get_by_slug(DEFAULT_SLUG, tenant=t.id)
            for label, group in (("TODAY/FUTURE → Pending", future), ("PAST → left as-is", past), ("UNDATED → left as-is", undated)):
                if not group:
                    continue
                w(f"    {label}:")
                for r in group[:MAX_LISTED]:
                    ev = events.get(r.id)
                    w(
                        f"      {request_display_code(r.id)} (#{r.id}) · {r.name!r} · "
                        f"created {timezone.localdate(r.created_at).isoformat()} · "
                        f"event date {event_day(r) or '-'} · {request_source(r, plan_ids)} · "
                        f"events={', '.join(map(str, ev)) if ev else 'none'}"
                    )
                if len(group) > MAX_LISTED:
                    w(f"      … {len(group) - MAX_LISTED} more")
            if not future:
                continue
            if pending is None:
                w(f"    ! no '{DEFAULT_SLUG}' request status — {len(future)} future request(s) left as-is")
                continue
            if not apply:
                moved += len(future)
                continue
            with transaction.atomic():
                for r in future:
                    if Request.objects.filter(pk=r.pk, status__isnull=True).update(status_id=pending.pk):
                        moved += 1
                        RequestActivityLog.objects.create(
                            tenant_id=t.id,
                            request_id=r.pk,
                            kind=RequestActivityLog.KIND_STATUS_CHANGED,
                            actor_user=None,
                            summary="Status restored to Pending (request had no status)",
                            metadata={"from": None, "to": DEFAULT_SLUG, "by": "audit_request_statuses"},
                        )
                        changed_tenants.add(t.id)
        for tid in changed_tenants:
            _invalidate_dashboard_cache(tid, ["request_stats", "request_time_series"])
        return moved
