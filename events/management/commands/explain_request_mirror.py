"""Explain — step by step — why one Request did or didn't reach its tracker.

`sheets_mirror` is deliberately best-effort: `upsert_request_row` catches
`HttpError` AND bare `Exception`, logs a warning, and returns False, and the
post_save signal wraps that in ANOTHER try/except. So a request that fails to
mirror leaves no trace anywhere the app surfaces — and on Cloud Run the warning
is only readable via gcloud, which needs interactive reauth.

This replays the mirror's decision path for a single request, reporting each
precondition, and with --apply runs the real upsert with the swallowing removed
so the actual API error is raised and printed verbatim.

Reads only, unless --apply. With --apply it writes exactly ONE request's row —
the additive repair, as opposed to `sync_tenant_to_sheet` which rewrites every
row and would clobber correct times wherever a request's timezone is currently
wrong (see project_ld_tracker_sync_times).

Usage:
    python manage.py explain_request_mirror --request-id 1583
    python manage.py explain_request_mirror --request-id 1583 --apply
    python manage.py explain_request_mirror --request-id 1581,1582,1583 --apply
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from events.models import Request
from utils import sheets_mirror as _sm


class Command(BaseCommand):
    help = (
        "Explain why a Request did/didn't mirror to its tracker sheet; "
        "--apply writes just that request's row and surfaces the real error."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--request-id", type=str, required=True,
            help="Request pk, or a comma-separated list.",
        )
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually write the row(s), letting API errors surface.",
        )

    def handle(self, *args, **opts):
        ids: list[int] = []
        for tok in str(opts["request_id"]).split(","):
            tok = tok.strip()
            if tok:
                try:
                    ids.append(int(tok))
                except ValueError:
                    raise CommandError(f"Bad --request-id token {tok!r}")
        apply = opts["apply"]
        if not apply:
            self.stdout.write(self.style.WARNING(
                "DRY RUN — reporting preconditions only; pass --apply to write.\n"
            ))

        svc = _sm._service()
        if svc is None:
            raise CommandError("No Sheets credentials (ADC).")

        wrote = failed = 0
        for rid in ids:
            self.stdout.write("=" * 78)
            try:
                r = (
                    Request.objects.select_related(
                        "tenant", "timezone", "state", "retailer", "status"
                    ).get(pk=rid)
                )
            except Request.DoesNotExist:
                self.stdout.write(self.style.ERROR(f"REQ-{rid}: no such request"))
                failed += 1
                continue

            tenant = getattr(r, "tenant", None)
            self.stdout.write(
                f"REQ-{r.id}  uuid={r.uuid}\n"
                f"  tenant      : {getattr(tenant, 'name', None)!r} "
                f"(slug={getattr(tenant, 'slug', None)})\n"
                f"  status      : {getattr(getattr(r,'status',None),'slug','?')}\n"
                f"  deleted_at  : {r.deleted_at}\n"
                f"  date        : {r.date}"
            )
            if tenant is None:
                self.stdout.write(self.style.ERROR(
                    "  STOP: no tenant — _ld_retail_row returns None, mirror is a no-op"
                ))
                failed += 1
                continue

            layout = _sm._tenant_layout(tenant)
            sheet_url = getattr(tenant, "linked_sheet_url", "") or ""
            sheet_id = _sm.extract_sheet_id(sheet_url)
            tab = (getattr(tenant, "master_tracker_tab_name", "") or "").strip() or None
            by_date = bool(getattr(tenant, "master_tracker_insert_by_date", False))
            self.stdout.write(
                f"  layout      : {layout or '(generic)'}\n"
                f"  sheet_id    : {sheet_id or '(NONE — mirror cannot run)'}\n"
                f"  tab         : {tab or '(first worksheet)'}\n"
                f"  insert_by_date: {by_date}"
            )
            if not sheet_id:
                self.stdout.write(self.style.ERROR(
                    "  STOP: tenant has no linked_sheet_url"
                ))
                failed += 1
                continue

            row9 = _sm._ld_retail_row(r)
            if row9 is None:
                self.stdout.write(self.style.ERROR("  STOP: _ld_retail_row -> None"))
                failed += 1
                continue
            labels = ["A State", "B Weekday", "C Date", "D Store", "E Start",
                      "F End", "G Address", "H Notes", "I SKUs"]
            self.stdout.write("  row it would write:")
            for lbl, val in zip(labels, row9):
                self.stdout.write(f"    {lbl:<11} {str(val)[:62]!r}")

            # --- the reads the mirror does before writing ---
            try:
                gid = _sm._ld_ensure_grid(svc, sheet_id, tab)
                self.stdout.write(f"  _ld_ensure_grid -> gid={gid}")
            except Exception as exc:  # noqa: BLE001 — this is the diagnosis
                self.stdout.write(self.style.ERROR(
                    f"  FAILS HERE: _ld_ensure_grid raised {type(exc).__name__}: {exc}"
                ))
                failed += 1
                continue
            try:
                existing = _sm._ld_existing_rows(svc, sheet_id, tab)
                here = existing.get(str(r.uuid))
                self.stdout.write(
                    f"  _ld_existing_rows -> {len(existing)} keyed row(s); "
                    f"this uuid: {here if here else 'NOT PRESENT (would be a new row)'}"
                )
            except Exception as exc:  # noqa: BLE001
                self.stdout.write(self.style.ERROR(
                    f"  FAILS HERE: _ld_existing_rows raised {type(exc).__name__}: {exc}"
                ))
                failed += 1
                continue
            if here is None and by_date and tab and gid is not None:
                try:
                    at = _sm._date_descending_insert_index(
                        svc, sheet_id, tab, _sm._parse_sheet_date(row9[2])
                    )
                    self.stdout.write(
                        f"  _date_descending_insert_index -> {at}"
                        + ("" if at is not None else "  (None -> appends at bottom)")
                    )
                except Exception as exc:  # noqa: BLE001
                    self.stdout.write(self.style.ERROR(
                        "  FAILS HERE: _date_descending_insert_index raised "
                        f"{type(exc).__name__}: {exc}"
                    ))
                    failed += 1
                    continue

            if not apply:
                self.stdout.write(
                    "  all preconditions pass — pass --apply to write and, if it "
                    "still fails, see the real API error"
                )
                continue

            # Call the LD path DIRECTLY, not through upsert_request_row, so the
            # exception is not swallowed and we learn the actual cause.
            try:
                ok = _sm._ld_upsert_request_row(svc, sheet_id, tab, r)
                if ok:
                    after = _sm._ld_existing_rows(svc, sheet_id, tab).get(str(r.uuid))
                    self.stdout.write(self.style.SUCCESS(
                        f"  WROTE — now at sheet row {after}"
                    ))
                    wrote += 1
                else:
                    self.stdout.write(self.style.ERROR(
                        "  returned False without raising"
                    ))
                    failed += 1
            except Exception as exc:  # noqa: BLE001 — the whole point
                self.stdout.write(self.style.ERROR(
                    f"  RAISED {type(exc).__name__}: {exc}"
                ))
                failed += 1

        self.stdout.write("=" * 78)
        self.stdout.write(f"\nwrote={wrote} failed={failed} of {len(ids)} request(s)")
