"""
Backfill: assign the territory RMM (and stamp the state) for requests that
were created WITHOUT routing — the "SCHEDULED" / internally-created events.

Why this exists (and why it's NOT a migration):
    RMM auto-routing (``events/routing.py:assign_rmm_for_request``) historically
    only ran on the PUBLIC-FORM submission path (`/spark-form/:slug`). Requests
    created INTERNALLY (the admin "create event with request" flow, bulk
    uploads, the missing-event escape hatch) never called it, so they were
    saved with ``rmm_asigned = NULL`` and often no structured ``state``. That
    produced the two symptoms ops reported:

      * the Master Tracker "Market" column is blank (Market is derived from
        ``retailer.location.state`` OR ``request.state`` — neither was set,
        even though the city/state sits in the address text), and
      * the row never routes to a territory owner, so it's absent from that
        RMM's filtered linked-sheet view ("visible in Spark, missing from my
        sheet").

    The forward fix (calling ``route_request_sync`` on the internal create
    path) is handled in ``events/mutations.py:create_event_with_request``.
    This command repairs the EXISTING backlog.

    It is NOT a migration because the target rows are tenant data and the work
    re-syncs the linked Google Sheet per row (a network round-trip); a one-shot
    migration would run once at deploy across all environments, whereas this
    can be previewed (dry-run, no writes/network), scoped per tenant, capped
    per invocation, and re-run idempotently against prod separately or from
    GitHub Actions. Mirrors the backfill_*/repair_* command family.

How it fills each request (no Photon, no geocode — all from data we already
have):
    1. STATE — parsed from the request's address (``extract_state_code``) with
       the same fallback chain the public form uses (address → request.state →
       location.state → retailer.location.state). Stamped onto ``request.state``
       when blank, so "Market" renders.
    2. RMM — the territory owner for that state per the per-tenant map
       (Liquid Death today), or the tenant's ``default_external_rmm`` override.
       Assignment only — NO territory email is sent (an admin/import created
       these; the public-form notify flow doesn't apply).

    Persisted via ``route_request_sync`` (a queryset ``.update()``, so it does
    NOT fire the Request post_save signal), then the linked Sheet row is
    re-synced exactly once per changed row via ``upsert_request_row``.

Count-only DRY-RUN:
    Dry-run writes NOTHING and makes NO Sheets calls. It counts the candidates
    and, for up to ``--limit`` of them, previews how many WOULD get an RMM /
    a state. Returns fast.

EXECUTE is CAPPED at ``--limit`` per invocation (default 100):
    Each repaired row costs ~2 Google Sheets API calls (find + write), so the
    cap keeps a single run well under the Sheets per-minute quota and the Cloud
    Run timeout. The report says how many still REMAIN so the operator re-runs
    until 0.

Idempotent:
    Only BLANK fields are filled — an already-set ``state``/``rmm_asigned`` is
    left untouched, and the candidate queryset only selects ``rmm_asigned IS
    NULL`` rows, so a second run only drains whatever's left. Each row is
    savepointed so one failure doesn't poison the run.

Flags:
    --dry-run        Report counts only (no writes, no Sheets calls). DEFAULT: ON.
    --execute        Actually assign + stamp + re-sync (turns OFF dry-run).
    --tenant <x>     Scope to a single tenant (slug or numeric id). Default: all
                     routable tenants (territory-mapped OR with a default RMM).
    --limit <n>      Max requests to REPAIR per invocation. DEFAULT: 100.

Usage:
    python manage.py backfill_request_rmm_routing                       # DRY RUN
    python manage.py backfill_request_rmm_routing --tenant liquid-death # dry run, one tenant
    python manage.py backfill_request_rmm_routing --execute             # APPLY (cap 100)
    python manage.py backfill_request_rmm_routing --execute --limit 200
"""

from __future__ import annotations

import logging
import traceback

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from events.models import Request
from events.routing import (
    ROUTED_TENANT_SLUGS,
    compute_request_routing,
    route_request_sync,
)
from tenants.models import Tenant
from utils.sheets_mirror import upsert_request_row

logger = logging.getLogger(__name__)

# Default cap on how many requests we REPAIR per invocation. Each repaired row
# does ~2 Google Sheets API calls (find row + write), so this keeps a single
# run under the Sheets per-minute quota and the Cloud Run 300s timeout. The
# report says how many REMAIN so the operator re-runs until 0.
DEFAULT_LIMIT = 100

# select_related set used everywhere we touch a request, so routing + the sheet
# upsert never trigger a sync-query in an unexpected place.
_REQUEST_RELATIONS = (
    "tenant",
    "tenant__default_external_rmm",
    "state",
    "rmm_asigned",
    "location__state",
    "retailer__location__state",
    "request_type",
    "timezone",
)


def _format_exc(exc: BaseException) -> str:
    """Concise, log-safe one-liner for a failed row (mirrors the geocode
    backfill helper): exception type + message + the last traceback frame."""
    type_name = type(exc).__name__
    message = " ".join(str(exc).split())
    if len(message) > 300:
        message = message[:297] + "..."
    frame = ""
    tb = exc.__traceback__
    if tb is not None:
        last = traceback.extract_tb(tb)[-1]
        filename = last.filename.split("/")[-1]
        frame = f" [{filename}:{last.lineno} in {last.name}]"
    return f"{type_name}: {message}{frame}"


class Command(BaseCommand):
    help = (
        "Backfill rmm_asigned (and request.state) for requests created without "
        "routing — the internally-created/SCHEDULED rows that show a blank "
        "Market and never reach the right RMM's sheet view. Assignment only, no "
        "email; re-syncs each changed row's linked Sheet. DRY-RUN by default "
        "(counts only, no writes/Sheets calls) — pass --execute. Supports "
        "--tenant and --limit (default 100)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", default=True)
        parser.add_argument("--execute", action="store_true", default=False)
        parser.add_argument("--tenant", type=str, default=None)
        parser.add_argument("--limit", type=int, default=None)
        # Network fallback: for candidates the regex/relations can't resolve a
        # state for, geocode the address via Photon to derive it (Photon
        # already geocoded these for the coordinate backfill). Off by default
        # (it makes per-row network calls); the operator opts in.
        parser.add_argument("--geocode-state", action="store_true", default=False)
        # Manual force path: set an explicit state on a known list of request
        # IDs, then assign the territory RMM from it + re-sync the Sheet. For
        # the genuinely-incomplete rows (venue-only / city-only addresses) that
        # neither the parser nor Photon can resolve — and where guessing via
        # geocode is unsafe — but a human knows the answer (e.g. "Madison
        # Square Garden" is NY). Both must be passed together; --execute still
        # gates writes. Honors --tenant as an extra safety scope.
        parser.add_argument("--ids", type=str, default=None)
        parser.add_argument("--force-state", type=str, default=None)

    # ── candidate selection ────────────────────────────────────────────
    def _candidates(self, tenant_arg: str | None):
        """Requests missing an RMM *or* a state, scoped to routable tenants.

        We include `state IS NULL` (not just `rmm_asigned IS NULL`) because the
        RMMs filter their sheets by the Market/State column — a request that was
        assigned an RMM but never had its state stamped (e.g. the old public-
        form path set only the RMM) still shows a blank Market and falls out of
        their view. route_request_sync fills whichever blank is missing.

        Routable = the tenant's public-form slug is in the territory map, OR
        the tenant has a ``default_external_rmm`` override. An explicit
        --tenant scopes to just that tenant (and trusts the operator)."""
        qs = Request.objects.filter(
            Q(rmm_asigned__isnull=True) | Q(state__isnull=True)
        )

        if tenant_arg:
            tenant = self._resolve_tenant(tenant_arg)
            return qs.filter(tenant_id=tenant.id)

        routable = (
            Q(tenant__request_url_name__in=ROUTED_TENANT_SLUGS)
            | Q(tenant__slug__in=ROUTED_TENANT_SLUGS)
            | Q(tenant__default_external_rmm__isnull=False)
        )
        return qs.filter(routable)

    def _resolve_tenant(self, tenant_arg: str) -> Tenant:
        tenant = (
            Tenant.objects.filter(slug=tenant_arg).first()
            or Tenant.objects.filter(request_url_name=tenant_arg).first()
        )
        if tenant is None and tenant_arg.isdigit():
            tenant = Tenant.objects.filter(id=int(tenant_arg)).first()
        if tenant is None:
            raise CommandError(f"No tenant matched '{tenant_arg}' (slug or id).")
        return tenant

    # ── entry point ────────────────────────────────────────────────────
    def handle(self, *args, **opts):
        execute = bool(opts.get("execute"))
        dry_run = not execute
        tenant_arg = opts.get("tenant")
        limit = opts.get("limit") or DEFAULT_LIMIT

        # Manual force path takes precedence over the candidate scan: when the
        # operator passes an explicit ID list + state, we set that state on
        # exactly those rows (no parser, no geocode) and route the RMM from it.
        force_state = opts.get("force_state")
        ids_raw = opts.get("ids")
        if force_state or ids_raw:
            if not (force_state and ids_raw):
                raise CommandError(
                    "--ids and --force-state must be used together "
                    "(e.g. --ids 267,268,291 --force-state NY)."
                )
            ids = [int(x) for x in str(ids_raw).split(",") if x.strip().isdigit()]
            if not ids:
                raise CommandError(f"--ids parsed to an empty list from {ids_raw!r}.")
            return self._force_state(
                ids=ids, code=force_state, tenant_arg=tenant_arg, execute=execute
            )

        base_qs = self._candidates(tenant_arg).select_related(*_REQUEST_RELATIONS)
        total = base_qs.count()

        scope = f"tenant={tenant_arg}" if tenant_arg else "all routable tenants"
        self.stdout.write(
            f"Request RMM routing backfill — {scope}; "
            f"{total} request(s) with no RMM assigned."
        )

        geocode_state = bool(opts.get("geocode_state"))
        if dry_run:
            return self._dry_run(base_qs, total, limit)
        return self._execute(base_qs, total, limit, geocode_state=geocode_state)

    # ── dry-run: counts only, no writes, no Sheets calls ───────────────
    def _dry_run(self, base_qs, total: int, limit: int):
        self.stdout.write(self.style.WARNING("DRY RUN — no writes, no Sheets calls."))
        would_rmm = 0
        would_state = 0
        sampled = 0
        unroutable: list[tuple] = []
        for request in base_qs.order_by("id")[:limit]:
            sampled += 1
            assigned, _state_code, state_obj = compute_request_routing(request)
            if assigned is not None:
                would_rmm += 1
            else:
                unroutable.append(
                    (request.id, request.name, getattr(request, "address", "") or "")
                )
            if not request.state_id and state_obj is not None:
                would_state += 1

        self.stdout.write(
            f"Of the first {sampled} candidate(s) (limit={limit}): "
            f"would assign an RMM to {would_rmm}, "
            f"would stamp a state on {would_state}."
        )
        if unroutable:
            self.stdout.write(
                f"{len(unroutable)} have NO resolvable state (address won't "
                "parse + no location relation). Add a city/state to the "
                "address, OR run --execute --geocode-state to let Photon "
                "derive it:"
            )
            for rid, rname, raddr in unroutable[:40]:
                self.stdout.write(
                    f"  • REQ-{rid}  {rname}  —  {raddr or '(no address)'}"
                )
        if total > sampled:
            self.stdout.write(
                f"{total - sampled} more candidate(s) beyond this preview — "
                f"run --execute (repeatedly, cap {limit}) to drain them all."
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"Would route up to {would_rmm} request(s) to an RMM. "
                f"Re-run with --execute to apply."
            )
        )
        self.stdout.write(
            f"RESULT mode=dry-run candidates={total} sampled={sampled} "
            f"would_rmm={would_rmm} would_state={would_state} "
            f"unroutable={len(unroutable)} limit={limit}"
        )
        return None

    # ── execute: assign + stamp + re-sync, capped at limit ─────────────
    def _execute(self, base_qs, total: int, limit: int, *, geocode_state: bool = False):
        import time as _time

        from django.utils import timezone as _djtz
        from events.models import State as _State
        from utils.geocoding import photon_state_for_address

        assigned_n = stated_n = synced_n = failed_n = geocoded_n = processed = 0
        unroutable: list[tuple] = []

        for request in base_qs.order_by("id")[:limit]:
            processed += 1
            try:
                had_rmm = bool(request.rmm_asigned_id)
                had_state = bool(request.state_id)
                assigned, _state_code, changed = route_request_sync(request)

                # Geocode fallback: the address had no parseable state and the
                # relations gave none, but Photon can often derive one (it
                # geocoded these for the coordinate backfill). Map the returned
                # state NAME to a State row, stamp it, and re-route. Only US
                # states exist in the table, so a bad/international result just
                # doesn't match — no mis-routing.
                if assigned is None and geocode_state and not request.state_id:
                    name = photon_state_for_address(getattr(request, "address", None))
                    state = (
                        _State.objects.filter(name__iexact=name).order_by("id").first()
                        if name
                        else None
                    )
                    if state is not None:
                        Request.objects.filter(pk=request.pk).update(
                            state_id=state.id, updated_at=_djtz.now()
                        )
                        request = (
                            Request.objects.select_related(*_REQUEST_RELATIONS)
                            .filter(pk=request.pk)
                            .first()
                        )
                        geocoded_n += 1
                        assigned, _state_code, changed = route_request_sync(request)
                    _time.sleep(1.0)  # polite to the free Photon service

                # Count what actually got filled this run.
                if not had_rmm and getattr(request, "rmm_asigned_id", None):
                    assigned_n += 1
                if not had_state and getattr(request, "state_id", None):
                    stated_n += 1
                # Genuinely unroutable only when NOTHING resolved — still no
                # RMM and no state (e.g. a venue-name-only address).
                if not getattr(request, "rmm_asigned_id", None) and not getattr(
                    request, "state_id", None
                ):
                    unroutable.append(
                        (
                            getattr(request, "id", None),
                            getattr(request, "name", ""),
                            getattr(request, "address", "") or "",
                        )
                    )

                if changed:
                    # Re-fetch with fresh relations so the sheet row reflects
                    # the new state + RMM, then sync once (route_request_sync
                    # used .update(), so the post_save signal did NOT fire).
                    fresh = (
                        Request.objects.select_related(*_REQUEST_RELATIONS)
                        .filter(pk=request.pk)
                        .first()
                    )
                    if fresh is not None and upsert_request_row(fresh):
                        synced_n += 1
            except Exception as exc:  # noqa: BLE001 — best-effort per row
                failed_n += 1
                logger.warning(
                    "RMM routing backfill failed for request=%s: %s",
                    getattr(request, "id", None),
                    _format_exc(exc),
                )

        # "Remaining" = candidates beyond this run's cap — re-run to drain
        # them. The genuinely-stuck rows are reported separately as `unroutable`
        # (still no RMM and no state — e.g. a venue-name-only address).
        remaining = max(0, total - processed)
        self.stdout.write(
            self.style.SUCCESS(
                f"Updated: {processed} request(s) processed "
                f"(assigned RMM={assigned_n}, stamped state={stated_n}, "
                f"geocoded state={geocoded_n}, sheet re-synced={synced_n}, "
                f"failed={failed_n})."
            )
        )
        self.stdout.write(f"Remaining to process (re-run to drain): {remaining}")
        if unroutable:
            self.stdout.write(
                "Still unroutable (no state from address or geocode — add a "
                "city/state to the address, then re-run):"
            )
            for rid, rname, raddr in unroutable[:40]:
                self.stdout.write(
                    f"  • REQ-{rid}  {rname}  —  {raddr or '(no address)'}"
                )
            if len(unroutable) > 40:
                self.stdout.write(f"  …and {len(unroutable) - 40} more.")
        self.stdout.write(
            f"RESULT mode=execute candidates={total} processed={processed} "
            f"assigned={assigned_n} stated={stated_n} geocoded={geocoded_n} "
            f"synced={synced_n} failed={failed_n} remaining={remaining} "
            f"unroutable={len(unroutable)} limit={limit}"
        )
        return None

    # ── force path: set an explicit state on a known ID list ───────────
    def _force_state(self, *, ids: list[int], code: str, tenant_arg, execute: bool):
        """Stamp ``code`` as the state on exactly ``ids``, then assign the
        territory RMM from it and re-sync each Sheet row.

        For the genuinely-incomplete rows the parser AND Photon can't resolve
        (venue-only / city-only addresses) but a human knows the answer
        ("Madison Square Garden" → NY). No guessing: the operator supplies the
        ids + state. We still route the RMM via the same territory map (so a
        forced NY row lands on the NY owner's sheet) and re-sync once per row.
        DRY-RUN by default — only writes when ``execute``.
        """
        from django.utils import timezone as _djtz
        from events.models import State as _State

        code = (code or "").strip()
        state = (
            _State.objects.filter(code__iexact=code).order_by("id").first()
            or _State.objects.filter(name__iexact=code).order_by("id").first()
        )
        if state is None:
            raise CommandError(f"No State row matched '{code}' (2-letter code or name).")

        qs = Request.objects.filter(pk__in=ids).select_related(*_REQUEST_RELATIONS)
        if tenant_arg:
            tenant = self._resolve_tenant(tenant_arg)
            qs = qs.filter(tenant_id=tenant.id)
        found = {r.id: r for r in qs}
        missing = [i for i in ids if i not in found]

        self.stdout.write(
            f"Force-state {state.code} ({state.name}) on {len(found)} of "
            f"{len(ids)} request(s)"
            + (f" — not found / out of scope: {missing}" if missing else "")
        )

        if not execute:
            for rid in ids:
                r = found.get(rid)
                if r is None:
                    continue
                cur = getattr(r.rmm_asigned, "email", None)
                self.stdout.write(
                    f"  • REQ-{r.id} {r.name} — would set state={state.code}, "
                    f"current state={getattr(r.state, 'code', None)}, rmm={cur}"
                )
            self.stdout.write(
                self.style.WARNING("DRY RUN — no writes. Re-run with --execute.")
            )
            self.stdout.write(
                f"RESULT mode=dry-run force_state={state.code} "
                f"matched={len(found)} missing={len(missing)} ids={len(ids)}"
            )
            return None

        forced = assigned_n = rerouted_n = synced = failed = 0
        for rid in ids:
            request = found.get(rid)
            if request is None:
                continue
            try:
                had_rmm_id = request.rmm_asigned_id
                if request.state_id != state.id:
                    Request.objects.filter(pk=request.pk).update(
                        state_id=state.id, updated_at=_djtz.now()
                    )
                    forced += 1
                    request = (
                        Request.objects.select_related(*_REQUEST_RELATIONS)
                        .filter(pk=request.pk)
                        .first()
                    )
                # Derive the territory owner for the ASSERTED state and set it,
                # OVERWRITING any existing RMM. Unlike the candidate scan (which
                # only fills blanks), the force path CORRECTS a wrong owner: a
                # row stuck on the West-region RMM that the operator now asserts
                # is FL must move to the FL owner's sheet. compute_request_routing
                # is read-only; we persist the assignment explicitly.
                owner, _code, _obj = compute_request_routing(request)
                if owner is not None and request.rmm_asigned_id != owner.id:
                    Request.objects.filter(pk=request.pk).update(
                        rmm_asigned_id=owner.id, updated_at=_djtz.now()
                    )
                    if had_rmm_id:
                        rerouted_n += 1  # corrected an existing (wrong) RMM
                    else:
                        assigned_n += 1
                    request = (
                        Request.objects.select_related(*_REQUEST_RELATIONS)
                        .filter(pk=request.pk)
                        .first()
                    )
                # Re-sync the Sheet once — we forced a state (and maybe moved the
                # RMM), so the row needs to reflect the new Market/State + owner.
                if request is not None and upsert_request_row(request):
                    synced += 1
            except Exception as exc:  # noqa: BLE001 — best-effort per row
                failed += 1
                logger.warning(
                    "force-state failed for request=%s: %s", rid, _format_exc(exc)
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Force-state {state.code}: state set on {forced} row(s), "
                f"RMM newly assigned to {assigned_n}, RMM corrected on "
                f"{rerouted_n}, sheet re-synced={synced}, failed={failed}."
            )
        )
        self.stdout.write(
            f"RESULT mode=execute force_state={state.code} matched={len(found)} "
            f"forced={forced} assigned={assigned_n} rerouted={rerouted_n} "
            f"synced={synced} failed={failed} missing={len(missing)} ids={len(ids)}"
        )
        return None
