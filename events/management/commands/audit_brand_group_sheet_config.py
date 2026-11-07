"""READ-ONLY: brand-group tenant check-in codes + retail sheet ids vs constants.

Compares Tenant rows for famus-energy, alcohol-armor, wook-water, and
pivot-energy-drink against standing codes in events.brand_group_sheet.
Never remints or writes checkin_code — report only.

    python manage.py audit_brand_group_sheet_config
    python manage.py audit_brand_group_sheet_config --alert
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from events.brand_group_sheet import (
    ALCOHOL_ARMOR_CHECKIN_CODE,
    BRAND_GROUP_SHEET_ID,
    FAMUS_CHECKIN_CODE,
    PIVOT_CHECKIN_CODE,
    PIVOT_SHEET_ID,
    PIVOT_TENANT_SLUG,
    WOOK_WATER_CHECKIN_CODE,
    retail_sheet_id_for_tenant,
)
from tenants.models import Tenant

AUDIT_SLUGS: tuple[str, ...] = (
    "famus-energy",
    "alcohol-armor",
    "wook-water",
    PIVOT_TENANT_SLUG,
)

_STANDING_CHECKIN: dict[str, str] = {
    "famus-energy": FAMUS_CHECKIN_CODE,
    "alcohol-armor": ALCOHOL_ARMOR_CHECKIN_CODE,
    "wook-water": WOOK_WATER_CHECKIN_CODE,
    PIVOT_TENANT_SLUG: PIVOT_CHECKIN_CODE,
}

_EXPECTED_RETAIL_SHEET: dict[str, str] = {
    "famus-energy": BRAND_GROUP_SHEET_ID,
    "alcohol-armor": BRAND_GROUP_SHEET_ID,
    "wook-water": BRAND_GROUP_SHEET_ID,
    PIVOT_TENANT_SLUG: PIVOT_SHEET_ID,
}


class Command(BaseCommand):
    help = (
        "Report mismatches between brand-group tenants and standing "
        "check-in / retail sheet constants (no writes)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--alert",
            action="store_true",
            help="Email Ignite when mismatches are found (default: stdout only).",
        )

    def handle(self, *args, **opts):
        alert = bool(opts.get("alert"))
        w = self.stdout.write

        mismatches: list[str] = []
        for slug in AUDIT_SLUGS:
            tenant = (
                Tenant.objects.filter(slug=slug)
                .only("id", "slug", "checkin_code", "retail_sheet_id")
                .first()
            )
            if tenant is None:
                mismatches.append(f"{slug}: tenant row missing")
                w(f"MISSING tenant slug={slug}")
                continue

            expected_code = _STANDING_CHECKIN.get(slug, "")
            actual_code = (tenant.checkin_code or "").strip()
            if expected_code and actual_code != expected_code:
                mismatches.append(
                    f"{slug}: checkin_code={actual_code!r} expected {expected_code!r}"
                )
                w(
                    f"MISMATCH checkin_code slug={slug} "
                    f"db={actual_code!r} standing={expected_code!r}"
                )
            else:
                w(f"OK checkin_code slug={slug} code={actual_code!r}")

            expected_sheet = _EXPECTED_RETAIL_SHEET.get(slug, "")
            db_sheet = (tenant.retail_sheet_id or "").strip()
            resolved_sheet = retail_sheet_id_for_tenant(tenant)
            if expected_sheet and db_sheet and db_sheet != expected_sheet:
                mismatches.append(
                    f"{slug}: retail_sheet_id={db_sheet!r} expected {expected_sheet!r}"
                )
                w(
                    f"MISMATCH retail_sheet_id slug={slug} "
                    f"db={db_sheet!r} expected={expected_sheet!r}"
                )
            elif expected_sheet and not db_sheet:
                w(
                    f"WARN retail_sheet_id slug={slug} empty "
                    f"(resolved={resolved_sheet!r} expected={expected_sheet!r})"
                )
            else:
                w(
                    f"OK retail_sheet slug={slug} db={db_sheet!r} "
                    f"resolved={resolved_sheet!r}"
                )

        summary = (
            f"audit_brand_group_sheet_config: {len(mismatches)} mismatch(es)"
            if mismatches
            else "audit_brand_group_sheet_config: all aligned"
        )
        w(summary)
        if alert and mismatches:
            self._send_alert(summary, mismatches)

    def _send_alert(self, summary: str, mismatches: list[str]) -> None:
        try:
            from tenants.support import _resolve_ignite_recipients
            from utils.mailer import Envelope, Mailer

            body = "<br>".join(mismatches)
            recipients = _resolve_ignite_recipients()
            if not recipients:
                return

            class _Alert(Mailer):
                def envelope(self) -> Envelope:
                    return Envelope(
                        subject=f"[Spark] {summary}",
                        html=f"<p>{summary}</p><p>{body}</p>",
                        to_emails=recipients,
                    )

            _Alert().send()
        except Exception:
            self.stderr.write("alert email failed (see logs)")
