"""Read-only audit: are a tenant's upcoming requests on the LD MASTER_Tracker,
and did their times land correctly?

Two questions this answers, both raised against Liquid Death:

  1. COVERAGE — every non-deleted request from `--since` forward, and whether the
     sheet carries a keyed row for it (the mirror stashes the request UUID in
     column BR). A request with no keyed row never mirrored.

  2. TIMES — for rows that ARE present, the sheet's Start/End (cols E/F) next to
     what the request actually holds. Dumps the RAW stored value including its
     tzinfo plus the venue tz offset, because the whole class of bug here is a
     naive-vs-aware mixup and you cannot tell the difference from a rendered
     "7:30a". Also shows what `_fmt_time_ld` produces today versus the venue
     wall-clock, so a double-shift is visible as a fixed hour delta.

Writes nothing. Safe to run against a client-owned sheet at any time.

Usage:
    python manage.py audit_ld_tracker_sync --tenant-slug liquid-death
    python manage.py audit_ld_tracker_sync --tenant-slug liquid-death --since 2026-07-01
"""
from __future__ import annotations

from datetime import datetime, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count
from django.utils import timezone as djtz

from events.models import Request
from tenants.models import Tenant
from utils import sheets_mirror as _sm


def _raw(dt) -> str:
    """The stored value with its tzinfo made explicit — 'naive' when there is
    none, since that is precisely the distinction that causes the bug."""
    if dt is None:
        return "None"
    tz = getattr(dt, "tzinfo", None)
    if tz is None:
        return f"{dt.strftime('%Y-%m-%d %H:%M')} naive"
    off = tz.utcoffset(dt.replace(tzinfo=None) if hasattr(dt, "replace") else None)
    try:
        off = dt.utcoffset()
    except Exception:
        pass
    mins = int(off.total_seconds() // 60) if off is not None else 0
    sign = "+" if mins >= 0 else "-"
    return (
        f"{dt.strftime('%Y-%m-%d %H:%M')} {sign}{abs(mins)//60:02d}:{abs(mins)%60:02d}"
    )


def _wallclock(dt) -> str:
    """What the submitter typed, as stored — no shifting applied."""
    return dt.strftime("%-I:%M%p").lower().replace("m", "") if dt else ""


def _effective_offset_minutes(request) -> int:
    """The offset the mirror actually applies to this request's start_time.

    Reported for diagnosis only. It is DST-aware, so it will differ from the
    static `TimeZone.offset` column by 60 during DST — that gap IS the bug
    every pre-fix row on the sheet was written under."""
    from utils.tz import offset_minutes_for

    tz_row = getattr(request, "timezone", None)
    when = getattr(request, "start_time", None) or getattr(request, "date", None)
    if tz_row is None:
        return 0
    return offset_minutes_for(tz_row, at=when)


class Command(BaseCommand):
    help = (
        "Read-only: audit whether a tenant's upcoming requests reached the LD "
        "MASTER_Tracker and whether their Start/End times are right."
    )

    def add_arguments(self, parser):
        parser.add_argument("--tenant-slug", type=str, default="liquid-death")
        parser.add_argument(
            "--since", type=str, default="",
            help="ISO date; default = today (audits upcoming requests).",
        )
        parser.add_argument("--limit", type=int, default=400)

    def handle(self, *args, **opts):
        slug = opts["tenant_slug"]
        try:
            tenant = Tenant.objects.get(slug=slug)
        except Tenant.DoesNotExist:
            raise CommandError(f"No tenant with slug {slug!r}.")

        layout = _sm._tenant_layout(tenant)
        sheet_url = getattr(tenant, "linked_sheet_url", "") or ""
        sheet_id = _sm.extract_sheet_id(sheet_url)
        tab = (getattr(tenant, "master_tracker_tab_name", "") or "").strip() or None
        self.stdout.write(
            f"tenant={tenant.name!r} slug={slug} layout={layout or '(generic)'}\n"
            f"  sheet_id={sheet_id or '(none)'} tab={tab or '(first worksheet)'}\n"
            f"  insert_by_date={getattr(tenant, 'master_tracker_insert_by_date', None)}\n"
        )
        if not sheet_id:
            raise CommandError("Tenant has no linked_sheet_url — nothing to audit.")

        if opts["since"]:
            try:
                since = datetime.fromisoformat(opts["since"])
            except ValueError:
                raise CommandError("--since must be an ISO date, e.g. 2026-07-01")
        else:
            since = djtz.now().replace(hour=0, minute=0, second=0, microsecond=0)
        if djtz.is_naive(since):
            since = djtz.make_aware(since)

        qs = (
            Request.objects.filter(tenant=tenant, deleted_at__isnull=True, date__gte=since)
            .select_related("timezone", "state", "retailer", "status")
            .order_by("date", "id")[: opts["limit"]]
        )
        requests = list(qs)
        self.stdout.write(
            f"{len(requests)} non-deleted request(s) dated >= {since.date().isoformat()}\n"
        )
        if not requests:
            return

        # The Timezone table is a FIXED offset per row, which cannot express DST.
        # Dump it: duplicate rows for one zone, or a single row whose offset is
        # only right half the year, both render wrong clock times downstream.
        try:
            from events.models import Request as _R
            tz_model = _R._meta.get_field("timezone").related_model
            self.stdout.write("Timezone rows (id · name · offset min · usage):")
            counts = {
                row["timezone"]: row["n"]
                for row in Request.objects.filter(tenant=tenant)
                .values("timezone").annotate(n=Count("id"))
            }
            for tz in tz_model.objects.all().order_by("name", "id"):
                off = getattr(tz, "offset", None)
                hrs = f"{off/60:+.1f}h" if isinstance(off, (int, float)) else "?"
                self.stdout.write(
                    f"  [{tz.id}] {getattr(tz,'name','?'):<22} {str(off):>6} "
                    f"({hrs})   used by {counts.get(tz.id, 0)} {slug} request(s)"
                )
            self.stdout.write("")
        except Exception as exc:  # noqa: BLE001 — diagnostic only
            self.stdout.write(self.style.ERROR(f"timezone dump failed: {exc}\n"))

        svc = _sm._service()
        if svc is None:
            raise CommandError("No Sheets credentials (ADC).")
        present = _sm._ld_existing_rows(svc, sheet_id, tab)
        self.stdout.write(f"sheet carries {len(present)} Spark-keyed row(s) total\n")

        # Pull the sheet's A:I for just the rows we care about, one batched read.
        rows_needed = sorted({present[str(r.uuid)] for r in requests
                              if str(r.uuid) in present})
        sheet_rows: dict[int, list] = {}
        if rows_needed:
            lo, hi = rows_needed[0], rows_needed[-1]
            try:
                resp = (
                    svc.spreadsheets().values()
                    .get(spreadsheetId=sheet_id, range=_sm._qualify(tab, f"A{lo}:I{hi}"))
                    .execute()
                )
                for i, row in enumerate(resp.get("values") or [], start=lo):
                    sheet_rows[i] = row
            except Exception as exc:  # noqa: BLE001 — read-only, report and continue
                self.stdout.write(self.style.ERROR(f"sheet A:I read failed: {exc}"))

        missing: list = []
        time_mismatch: list = []
        self.stdout.write("=" * 100)
        for r in requests:
            uuid = str(r.uuid)
            row_idx = present.get(uuid)
            tz = _sm._tz_for_request(r)
            off = _effective_offset_minutes(r)
            tzname = getattr(getattr(r, "timezone", None), "name", "") or "(no tz)"
            status = getattr(getattr(r, "status", None), "slug", "") or "?"
            store = (
                getattr(getattr(r, "retailer", None), "name", "")
                or getattr(r, "retailer_name", "")
                or ""
            )
            head = (
                f"REQ-{r.id}  {r.date.date().isoformat() if r.date else '(no date)'}  "
                f"{status:<10} {store[:28]:<28}"
            )
            if row_idx is None:
                missing.append(r)
                self.stdout.write(self.style.ERROR(f"{head}  NOT ON SHEET"))
                continue
            srow = sheet_rows.get(row_idx, [])
            sheet_start = str(srow[4]).strip() if len(srow) > 4 else ""
            sheet_end = str(srow[5]).strip() if len(srow) > 5 else ""
            # The mirror's contract is stored-UTC converted to the venue's LOCAL
            # wall-clock, DST-aware (utils.tz). Compare the sheet against THAT: a
            # disagreement means the row was written under a different offset than
            # the request resolves to now — which is what every row mirrored before
            # the DST fix looks like (an hour early, and a day early when the date
            # column crossed local midnight with it).
            expect_start = _sm._fmt_time_ld(r.start_time, tz)
            expect_end = _sm._fmt_time_ld(r.end_time, tz)
            # Column C too: the date rolls back a day whenever the wrong offset
            # pushes local midnight over the boundary, so a row can be right on
            # E/F and still be filed under the wrong day.
            sheet_date = str(srow[2]).strip() if len(srow) > 2 else ""
            expect_date = _sm._fmt_date(r.date, tz)
            stale = (
                sheet_start != expect_start
                or sheet_end != expect_end
                or sheet_date != expect_date
            )
            if stale:
                time_mismatch.append((r, f"{sheet_date} {sheet_start}-{sheet_end}",
                                      f"{expect_date} {expect_start}-{expect_end}"))
            # A venue offset that disagrees with the address's state is the other
            # failure mode — the sheet then faithfully renders a wrong number.
            addr = (getattr(r, "address", "") or "").strip()
            self.stdout.write(
                f"{head}  row {row_idx}\n"
                f"     stored (UTC) : {_raw(r.start_time)} -> {_raw(r.end_time)}\n"
                f"     venue tz     : {tzname} effective offset {off} min "
                f"(static column: {getattr(getattr(r,'timezone',None),'offset','?')})\n"
                f"     address      : {addr[:70]}\n"
                f"     sheet C/E/F  : {sheet_date!r} / {sheet_start!r} / {sheet_end!r}\n"
                f"     expected     : {expect_date!r} / {expect_start!r} / "
                f"{expect_end!r}  (stored UTC -> venue local, DST-aware)"
                + ("  <-- SHEET IS STALE" if stale else "  ok")
            )

        self.stdout.write("=" * 100)
        self.stdout.write(
            f"\nSUMMARY\n  requests audited : {len(requests)}\n"
            f"  NOT on the sheet : {len(missing)}\n"
            f"  time mismatches  : {len(time_mismatch)}"
        )
        if missing:
            self.stdout.write("\nmissing from the sheet:")
            for r in missing:
                self.stdout.write(
                    f"  REQ-{r.id}  {r.date.date().isoformat() if r.date else '?'}  "
                    f"{getattr(getattr(r,'status',None),'slug','?')}  uuid={r.uuid}"
                )
        if time_mismatch:
            self.stdout.write("\ntime mismatches (sheet vs as-submitted):")
            for r, got, want in time_mismatch:
                self.stdout.write(f"  REQ-{r.id}: sheet {got!r}  should be {want!r}")
