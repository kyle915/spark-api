"""Mint (once) a brand's public BA job board link and report what it lists.

The token is the link and never changes: an existing board is kept as is.
The admin Plans panel also creates the board on first open; this command is
the ops path to get the link without logging in. DRY-RUN unless ``--apply``.

    python manage.py setup_job_board                 # Torch, dry run
    python manage.py setup_job_board --apply
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from jobboard.config import job_board_config_for
from jobboard.gigs import date_label, list_gigs, pending_plans, time_label
from jobboard.models import JobBoard
from tenants.models import Tenant

TORCH_SLUG = "torch-thc"
TORCH_FORM_SLUG = "keee-torch-thc"
BASE_URL = "https://client.igniteproductions.co"


class Command(BaseCommand):
    help = "Mint/keep a brand's public BA job board link. Dry-run by default; --apply to write."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", default=TORCH_SLUG, help="Exact slug, else request_url_name.")
        parser.add_argument("--apply", action="store_true", help="Write (default: dry run).")

    def _resolve_tenant(self, needle: str) -> Tenant:
        needle = (needle or "").strip()
        candidates = [needle]
        if needle == TORCH_SLUG:
            candidates.append(TORCH_FORM_SLUG)
        for candidate in candidates:
            for lookup in ("slug__iexact", "request_url_name__iexact"):
                matches = list(Tenant.objects.filter(**{lookup: candidate}).order_by("id"))
                if len(matches) == 1:
                    return matches[0]
                if len(matches) > 1:
                    ids = ", ".join(str(t.id) for t in matches)
                    raise CommandError(f"{len(matches)} tenants match {candidate!r} ({ids}).")
        raise CommandError(f"tenant-not-found: {needle}")

    def _report_gigs(self, tenant) -> None:
        gigs = list_gigs(tenant)
        on_board = [g for g in gigs if g.on_board]
        self.stdout.write(f"\n  Upcoming plan gigs: {len(gigs)} ({len(on_board)} on the board)")
        for g in gigs:
            state = "LISTED" if g.on_board else ("HIDDEN" if not g.listed else "0 SHIFTS")
            self.stdout.write(
                f"    [{g.event.id}] {date_label(g)} {time_label(g)} · {g.type_label} · "
                f"{g.venue!r} · {g.area or '-'} · {g.booked}/{g.open_shifts} booked · {state}"
            )
        pending = pending_plans(tenant)
        self.stdout.write(f"\n  Plans waiting on approval (not on the board yet): {len(pending)}")
        for p in pending:
            self.stdout.write(f"    {p.starts_on or '-'} · {p.name!r} · {p.market or '-'}")

    def handle(self, *args, **opts):
        apply = bool(opts["apply"])
        tenant = self._resolve_tenant(opts["tenant"])
        config = job_board_config_for(tenant)
        if config is None:
            raise CommandError(
                f"Job board isn't enabled for [{tenant.id}] {tenant.slug!r} (see jobboard/config.py)."
            )
        existing = JobBoard.objects.filter(tenant=tenant).first()

        self.stdout.write("=" * 72)
        self.stdout.write(
            f"TENANT : [{tenant.id}] {tenant.name!r} / {tenant.slug!r}\n"
            f"BRAND  : {config.brand_label}\n"
            f"NOTIFY : {', '.join(config.internal_emails)}\n"
            f"BOARD  : {'exists (KEEPING — never re-minted)' if existing else '(none yet)'}\n"
            f"MODE   : {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'}"
        )
        self.stdout.write("=" * 72)
        self._report_gigs(tenant)

        if existing is None and not apply:
            self.stdout.write(f"\nDRY-RUN — would mint {BASE_URL}/jobs/<token>\nRe-run with --apply to write.")
            return
        board = existing or JobBoard.objects.get_or_create(tenant=tenant)[0]
        self.stdout.write("")
        self.stdout.write("=" * 72)
        self.stdout.write(
            self.style.SUCCESS(f"JOB_BOARD_URL: {BASE_URL}/jobs/{board.token}")
            + ("" if existing is None else "  (no changes)")
        )
        self.stdout.write("=" * 72)
