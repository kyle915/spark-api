"""Self-healing sweep: mirror any request whose tracker row never landed.

Mirroring a Request to its tenant's tracker is best-effort by design and fails
SILENTLY — `upsert_request_row` catches HttpError and bare Exception, and the
post_save signal that calls it swallows too. On top of that, the two explicit
re-sync calls in events/mutations.py are both gated on `if _routed:`, so a
request whose routing didn't stamp a state never gets the belt-and-braces
second attempt. Net effect observed on Liquid Death 2026-07-29: six approved
requests since Jul 1 simply absent from MASTER_Tracker, with no signal anywhere.

Rather than chase every branch that can drop a row, this reconciles: find
requests the sheet has no key for and mirror them. Run it on a schedule and the
tracker converges no matter which path failed or why.

DESIGN CHOICES that keep this safe to run unattended:

  * ADDITIVE ONLY. Rows already keyed in the sheet are never rewritten. That
    matters because some existing rows are correct while the request's current
    timezone would render them WRONG (see project_ld_tracker_sync_times) — a
    blanket resync would corrupt them. This only ever adds what is missing.

  * SKIPS THE CLIENT'S HAND-TYPED TWIN. LD's RMMs type into the tracker by hand
    when Spark's row doesn't show up, so a "missing" request is often already
    on the sheet under a different store name. Adding it would duplicate a live
    activation. Any unkeyed row matching on date + start time + address prefix
    is treated as that request's twin: reported, not duplicated.

  * WINDOWED. Only requests dated within --days-back..--days-ahead, so a years
    -old backlog can't suddenly flood a client's sheet.

Usage:
    python manage.py reconcile_tracker_rows                      # dry, all tenants
    python manage.py reconcile_tracker_rows --tenant-slug liquid-death
    python manage.py reconcile_tracker_rows --apply
"""
from __future__ import annotations

import re
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone as djtz

from events.models import Request
from tenants.models import Tenant
from utils import sheets_mirror as _sm


def _norm_addr(value: str) -> str:
    """Street-number + street-name key, so '550 N State St, Chicago, IL 60654'
    and a hand-typed '550 N State St, Chicago' match."""
    v = (value or "").strip().lower()
    v = re.sub(r"[.,]", " ", v)
    v = re.sub(r"\s+", " ", v)
    return " ".join(v.split()[:4])


class Command(BaseCommand):
    help = (
        "Mirror any request whose tracker row is missing. Additive only, skips "
        "rows the client hand-typed, windowed by date. Dry-run default."
    )

    def add_arguments(self, parser):
        parser.add_argument("--tenant-slug", type=str, default="")
        parser.add_argument("--days-back", type=int, default=14)
        parser.add_argument("--days-ahead", type=int, default=120)
        parser.add_argument("--limit", type=int, default=200)
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually write the missing rows.",
        )

    def handle(self, *args, **opts):
        apply = opts["apply"]
        if not apply:
            self.stdout.write(self.style.WARNING(
                "DRY RUN — pass --apply to write the missing rows.\n"
            ))
        tenants = Tenant.objects.exclude(linked_sheet_url="").exclude(
            linked_sheet_url__isnull=True
        )
        if opts["tenant_slug"]:
            tenants = tenants.filter(slug=opts["tenant_slug"])
        tenants = list(tenants.order_by("id"))
        if not tenants:
            self.stdout.write("No tenants with a linked_sheet_url.")
            return

        svc = _sm._service()
        if svc is None:
            raise CommandError("No Sheets credentials (ADC).")

        now = djtz.now()
        lo = now - timedelta(days=opts["days_back"])
        hi = now + timedelta(days=opts["days_ahead"])
        total_missing = total_written = total_twin = total_failed = 0

        for tenant in tenants:
            sheet_id = _sm.extract_sheet_id(getattr(tenant, "linked_sheet_url", "") or "")
            if not sheet_id:
                continue
            layout = _sm._tenant_layout(tenant)
            if layout != _sm.LD_RETAIL_LAYOUT:
                # The generic layout keys column A and has its own append path;
                # reconciling it needs a different reader, so stay out rather
                # than half-handle it.
                self.stdout.write(
                    f"[{tenant.slug}] layout={layout or 'generic'} — skipped "
                    "(reconciler currently covers the ld_retail layout only)"
                )
                continue
            tab = (getattr(tenant, "master_tracker_tab_name", "") or "").strip() or None

            qs = (
                Request.objects.filter(
                    tenant=tenant, deleted_at__isnull=True,
                    date__gte=lo, date__lte=hi,
                )
                .select_related("timezone", "state", "retailer", "status")
                .order_by("date", "id")[: opts["limit"]]
            )
            requests = list(qs)
            keyed = _sm._ld_existing_rows(svc, sheet_id, tab)
            missing = [r for r in requests if str(r.uuid) not in keyed]
            self.stdout.write(
                f"\n[{tenant.slug}] {len(requests)} request(s) in window, "
                f"{len(keyed)} keyed row(s), {len(missing)} missing"
            )
            if not missing:
                continue

            # Read the unkeyed rows once so we can spot hand-typed twins.
            twins: dict[tuple, int] = {}
            try:
                resp = (
                    svc.spreadsheets().values()
                    .get(spreadsheetId=sheet_id,
                         range=_sm._qualify(tab, "A2:I400"))
                    .execute()
                )
                keyed_rows = set(keyed.values())
                for i, row in enumerate(resp.get("values") or [], start=2):
                    if i in keyed_rows or len(row) < 5:
                        continue
                    date_c = str(row[2]).strip() if len(row) > 2 else ""
                    start_e = str(row[4]).strip() if len(row) > 4 else ""
                    addr_g = str(row[6]).strip() if len(row) > 6 else ""
                    if date_c and start_e:
                        twins[(date_c, start_e, _norm_addr(addr_g))] = i
            except Exception as exc:  # noqa: BLE001 — twin check is advisory
                self.stdout.write(self.style.ERROR(
                    f"  unkeyed-row read failed ({exc}) — proceeding WITHOUT "
                    "the duplicate guard is unsafe, so skipping this tenant"
                ))
                continue

            for r in missing:
                total_missing += 1
                row9 = _sm._ld_retail_row(r)
                if row9 is None:
                    self.stdout.write(self.style.ERROR(
                        f"  REQ-{r.id}: _ld_retail_row -> None, skipped"
                    ))
                    total_failed += 1
                    continue
                key = (str(row9[2]).strip(), str(row9[4]).strip(), _norm_addr(row9[6]))
                twin = twins.get(key)
                if twin:
                    self.stdout.write(self.style.WARNING(
                        f"  REQ-{r.id}: already on the sheet by hand at row "
                        f"{twin} ({row9[2]} {row9[4]} {str(row9[6])[:38]}) — "
                        "NOT duplicating"
                    ))
                    total_twin += 1
                    continue
                if not apply:
                    self.stdout.write(
                        f"  + REQ-{r.id}: would add ({row9[2]} {row9[4]}-"
                        f"{row9[5]} {str(row9[3] or row9[6])[:38]})"
                    )
                    continue
                try:
                    _sm._ld_upsert_request_row(svc, sheet_id, tab, r)
                    at = _sm._ld_existing_rows(svc, sheet_id, tab).get(str(r.uuid))
                    self.stdout.write(self.style.SUCCESS(
                        f"  + REQ-{r.id}: added at row {at}"
                    ))
                    total_written += 1
                except Exception as exc:  # noqa: BLE001 — report, keep going
                    self.stdout.write(self.style.ERROR(
                        f"  ! REQ-{r.id}: {type(exc).__name__}: {exc}"
                    ))
                    total_failed += 1

        self.stdout.write(
            f"\nmissing={total_missing} written={total_written} "
            f"already-hand-entered={total_twin} failed={total_failed}"
        )
        # Emit a machine-readable tail so the cron endpoint can surface counts.
        self.stdout.write(
            f"JSON_RESULT:{{\"missing\": {total_missing}, "
            f"\"written\": {total_written}, \"twins\": {total_twin}, "
            f"\"failed\": {total_failed}}}"
        )
