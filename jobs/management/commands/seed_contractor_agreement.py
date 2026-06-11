"""Seed / bump the GLOBAL contractor agreement (tenant=None).

Idempotent on (version): re-running with the same version is a no-op;
a new version flips older active rows off and inserts the new one as
the active default. Per-tenant overrides are created from the admin UI,
not here.

    python manage.py seed_contractor_agreement
    python manage.py seed_contractor_agreement --version 2026-06 --file path.txt
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

_DEFAULT_VERSION = "2026-06"
_DEFAULT_BODY = """\
INDEPENDENT CONTRACTOR AGREEMENT — Ignite Productions

By accepting a shift, you confirm that:

1. INDEPENDENT CONTRACTOR. You are an independent contractor, not an
   employee of Ignite Productions or its clients. You control the manner
   and means of performing the work and are responsible for your own
   taxes.

2. RATE & PAYMENT. You agree to the hourly rate shown for the shift.
   Pay is calculated from your verified clock-in/clock-out times and is
   issued through Ignite's payment provider after the recap is filed and
   approved. Pre-approved expenses are reimbursed against an uploaded
   receipt.

3. ON-SITE CONDUCT. You will arrive on time, in the specified attire,
   represent the brand professionally, and follow the activation brief.

4. LOCATION VERIFICATION. You consent to location checks during the
   scheduled shift window for the sole purpose of verifying on-site
   presence and computing pay.

5. CANCELLATION. Notify Ignite as early as possible if you cannot make a
   confirmed shift. Repeated no-shows may affect future assignments.

This acceptance is recorded with the date, the agreement version, and
the confirmed rate.
"""


class Command(BaseCommand):
    help = "Create or bump the global contractor agreement (tenant=None)."

    def add_arguments(self, parser):
        parser.add_argument("--version", default=_DEFAULT_VERSION)
        parser.add_argument(
            "--file",
            help="Path to a UTF-8 text file with the agreement body "
            "(defaults to the built-in template).",
        )

    def handle(self, *args, **opts):
        from jobs.models import ContractorAgreement

        version = opts["version"].strip()
        body = _DEFAULT_BODY
        if opts.get("file"):
            with open(opts["file"], encoding="utf-8") as fh:
                body = fh.read()

        existing = ContractorAgreement.objects.filter(
            tenant__isnull=True, version=version
        ).first()
        if existing:
            changed = []
            if existing.body != body:
                existing.body = body
                changed.append("body")
            if not existing.is_active:
                existing.is_active = True
                changed.append("is_active")
            if changed:
                existing.save(update_fields=[*changed, "updated_at"])
                self.stdout.write(
                    f"Updated global agreement {version} ({', '.join(changed)})."
                )
            else:
                self.stdout.write(f"Global agreement {version} already current.")
            return

        # New version — retire older active global rows, insert this one.
        ContractorAgreement.objects.filter(
            tenant__isnull=True, is_active=True
        ).update(is_active=False)
        ContractorAgreement.objects.create(
            version=version, body=body, is_active=True, tenant=None
        )
        self.stdout.write(
            self.style.SUCCESS(f"Created global contractor agreement {version}.")
        )
