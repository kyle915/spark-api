"""One-shot append of specific Torch public-form requests to the retail Sheet.

Does NOT walk every Torch request (the 98 Binny bulk rows would flood the
workbook). Pass explicit Spark request PKs — Kyle's live public-form test
rows were REQ-1980 and REQ-1981.

Usage:
    python manage.py backfill_torch_public_form_sheet --ids 1980,1981
    python manage.py backfill_torch_public_form_sheet --ids 1980,1981 --apply
"""
from django.core.management.base import BaseCommand, CommandError

from events.models import Request
from events.torch_portal import is_torch_tenant
from utils.torch_public_form_sheet import append_torch_request_row


class Command(BaseCommand):
    help = (
        "Append specific Torch public spark-form requests to the retail "
        "schedule Google Sheet. Requires --ids so bulk imports stay off."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--ids",
            type=str,
            required=True,
            help="Comma-separated Request primary keys (e.g. 1980,1981).",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write to the Sheet. Without this, print the mapping only.",
        )

    def handle(self, *args, **options):
        raw_ids = (options.get("ids") or "").strip()
        ids: list[int] = []
        for part in raw_ids.split(","):
            part = part.strip()
            if not part:
                continue
            if not part.isdigit():
                raise CommandError(f"Not a request id: {part!r}")
            ids.append(int(part))
        if not ids:
            raise CommandError("Pass at least one id via --ids")

        qs = (
            Request.objects.filter(id__in=ids, deleted_at__isnull=True)
            .select_related(
                "tenant",
                "timezone",
                "request_type",
                "state",
                "retailer",
            )
            .prefetch_related("request_product__product")
        )
        found = {r.id: r for r in qs}
        apply = bool(options.get("apply"))
        written = 0
        for rid in ids:
            req = found.get(rid)
            if req is None:
                self.stderr.write(self.style.ERROR(f"REQ-{rid}: not found"))
                continue
            if not is_torch_tenant(req.tenant):
                self.stderr.write(
                    self.style.ERROR(f"REQ-{rid}: not a Torch request — skip")
                )
                continue
            self.stdout.write(
                f"REQ-{rid}  {req.name}  {req.address}  "
                f"requestor={req.requestor_email or req.client_email or '—'}"
            )
            if not apply:
                continue
            if append_torch_request_row(req):
                written += 1
                self.stdout.write(self.style.SUCCESS(f"  wrote REQ-{rid}"))
            else:
                self.stdout.write(
                    f"  skipped REQ-{rid} — already on the sheet, OR the "
                    "write failed silently. Run diagnose_torch_sheet "
                    f"--ids {rid} to tell which."
                )
        if not apply:
            self.stdout.write("Dry run. Re-run with --apply to write.")
        else:
            self.stdout.write(self.style.SUCCESS(f"Wrote {written} row(s)."))
