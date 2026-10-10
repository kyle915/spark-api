"""Mint a brand's CLIENT-TEAM recap link (``Tenant.checkin_team_code``).

The brand's own people file recaps from ``/checkin/<code>`` with no time
clock: name → event type (same picker as the BA link) → the matching recap
form. Unlike the 3rd-party agency link (``checkin_recap_code``) these recaps
COUNT in totals and carry ``CustomRecap.source_label`` (e.g. "Submitted by
Torch team") so admin and client can tell who filed them.

Idempotent and never re-mints: an existing team code is kept; only the label
and Walk-ups title are refreshed. The BA clock link and the agency link are
never touched. DRY-RUN unless ``--apply``.

    python manage.py setup_team_recap_link                 # Torch, dry run
    python manage.py setup_team_recap_link --apply
"""

from __future__ import annotations

import secrets

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from recaps.management.commands.setup_tenant_checkin import ALPHABET, CODE_BODY_LENGTH
from tenants.models import Tenant

TORCH_SLUG = "torch-thc"
TORCH_FORM_SLUG = "keee-torch-thc"
DEFAULT_PREFIX = "TH"
DEFAULT_LABEL = "Submitted by Torch team"
DEFAULT_TITLE = "Torch THC · Client team recaps (no clock)"
BASE_URL = "https://client.igniteproductions.co"


class Command(BaseCommand):
    help = (
        "Mint/keep a tenant's client-team recap link (no clock, event-type "
        "picker, counts in totals). Dry-run by default; --apply to write."
    )

    def add_arguments(self, parser):
        parser.add_argument("--tenant", default=TORCH_SLUG, help="Exact slug, else request_url_name.")
        parser.add_argument("--code-prefix", dest="code_prefix", default=DEFAULT_PREFIX)
        parser.add_argument("--label", default=DEFAULT_LABEL, help="Recap source label.")
        parser.add_argument("--title", default=DEFAULT_TITLE, help="Admin Walk-ups name.")
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

    def _mint_code(self, prefix: str) -> str:
        for _ in range(50):
            body = "".join(secrets.choice(ALPHABET) for _ in range(CODE_BODY_LENGTH))
            candidate = f"{prefix}-{body}"
            taken = Tenant.objects.filter(
                Q(checkin_code__iexact=candidate)
                | Q(checkin_recap_code__iexact=candidate)
                | Q(checkin_team_code__iexact=candidate)
            ).exists()
            if not taken:
                return candidate
        raise CommandError("Could not mint an unused check-in code.")

    def _report_programs(self, tenant) -> None:
        from ambassadors import checkin_web
        from recaps.models import CustomRecapTemplate

        programs, title = checkin_web.checkin_program_picker(tenant)
        self.stdout.write(f"\n  Event type picker ({title or 'default title'}):")
        if len(programs) < 2:
            self.stdout.write(
                self.style.WARNING(
                    "    Fewer than two programs — the page won't ask for an event type."
                )
            )
        for opt in programs:
            tpl = (
                CustomRecapTemplate.objects.filter(tenant=tenant, event_type_id=int(opt["id"]))
                .order_by("id")
                .first()
            )
            form = f"[{tpl.id}] {tpl.name!r}" if tpl else "NO TEMPLATE"
            self.stdout.write(f"    {opt.get('label') or opt['name']:<9} -> {opt['name']!r} -> {form}")

        resources = checkin_web.build_checkin_resources(tenant)
        self.stdout.write("\n  Resources on the page (same as the BA link):")
        for row in resources:
            self.stdout.write(f"    - {row.get('label')}  ({row.get('kind')})")
        if not resources:
            self.stdout.write("    (none)")

    def handle(self, *args, **opts):
        apply = bool(opts["apply"])
        tenant = self._resolve_tenant(opts["tenant"])
        prefix = (opts["code_prefix"] or DEFAULT_PREFIX).strip().upper().rstrip("-")
        label = (opts["label"] or "").strip()[:80]
        title = (opts["title"] or "").strip()[:120]
        if not label:
            raise CommandError("--label is required (it's what admin and client see).")

        existing = (tenant.checkin_team_code or "").strip()
        code = existing or (self._mint_code(prefix) if apply else f"{prefix}-XXXXXX")

        self.stdout.write("=" * 72)
        self.stdout.write(
            f"TENANT      : [{tenant.id}] {tenant.name!r} / {tenant.slug!r}\n"
            f"CLOCK CODE  : {tenant.checkin_code or '(none)'}  (untouched)\n"
            f"AGENCY CODE : {tenant.checkin_recap_code or '(none)'}  (untouched)\n"
            f"TEAM CODE   : {existing or '(none yet)'}"
            + ("  (KEEPING — never re-minted)" if existing else "")
            + "\n"
            f"LABEL       : {tenant.checkin_team_label or '(none)'} -> {label}\n"
            f"TITLE       : {tenant.checkin_team_title or '(none)'} -> {title}\n"
            f"MODE        : {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'}"
        )
        self.stdout.write("=" * 72)
        self._report_programs(tenant)

        if not apply:
            self.stdout.write(
                f"\nDRY-RUN — would serve {BASE_URL}/checkin/{code}  "
                "(no time clock, counts in totals)\nRe-run with --apply to write."
            )
            return

        changed = []
        if not existing:
            tenant.checkin_team_code = code
            changed.append("checkin_team_code")
        if tenant.checkin_team_label != label:
            tenant.checkin_team_label = label
            changed.append("checkin_team_label")
        if tenant.checkin_team_title != title:
            tenant.checkin_team_title = title
            changed.append("checkin_team_title")
        if changed:
            tenant.save(update_fields=changed)

        self.stdout.write("")
        self.stdout.write("=" * 72)
        self.stdout.write(
            self.style.SUCCESS(
                f"CHECKIN_TEAM_CODE: {tenant.checkin_team_code}"
                + ("" if changed else "  (no changes)")
            )
        )
        self.stdout.write(f"CHECKIN_TEAM_URL: {BASE_URL}/checkin/{tenant.checkin_team_code}")
        self.stdout.write("NO TIME CLOCK. Name + event type + recap. Counts in totals.")
        self.stdout.write("=" * 72)
