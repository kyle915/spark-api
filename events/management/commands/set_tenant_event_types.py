"""Standardize a tenant's (or every tenant's) recap/request EventType list.

Kyle standardized the event types Ignite runs (2026-06-13) to three —
Retail Sampling / On-Premise Sampling / Event — replacing the old generic
stock set (Sampling / Promotion / Launch / Special Event) that every tenant
got seeded with at creation and that nobody uses. Nevena flagged it: the
recap-template builder's EVENT TYPE picker for Stone House Bread showed the
dead defaults and was missing "Retail Sampling".

This command brings existing tenants in line WITHOUT clobbering intentional
config (the "swap stock, keep custom" policy Kyle picked):

  - ENSURE the --keep types exist (get_or_create) — Retail Sampling,
    On-Premise Sampling, Event.
  - RETIRE only the named legacy stock types (--retire: Sampling, Promotion,
    Launch, Special Event). Any OTHER type a tenant has — e.g. Liquid Death's
    "Event Activation" / "Direct Event Sampling" — is left untouched.
  - Before deleting a retired type, REPOINT its Events + CustomRecapTemplates
    onto --repoint-to (Retail Sampling) so nothing is orphaned. EventType FKs
    are on_delete=RESTRICT, so a still-referenced type can't be deleted — the
    repoint clears that.
  - FIX the default: if the tenant's current default was one of the retired
    types (the seed makes "Sampling" default, so this is the common case) or
    it has no default, set --default-type (Retail Sampling) as is_default. A
    custom default a tenant deliberately set is preserved.
  - EXCLUDE tenants by name (--exclude, default "Jeeter").

SAFE — DRY-RUN IS THE DEFAULT. Without --commit nothing is written: the
command reads each tenant's current types, counts what references the retired
ones, and prints exactly what a real run WOULD create / repoint / delete /
keep. Re-runnable + idempotent: a second --commit run is a no-op once a tenant
already matches. Run it through the secret-gated cron endpoint
(digest.cron_views.SetTenantEventTypesView) + the set-tenant-event-types
GitHub workflow.

Examples:
    # Preview the whole fleet (except Jeeter):
    set_tenant_event_types --all-tenants
    # Commit one tenant:
    set_tenant_event_types --tenant-name "Stone House Bread" --commit
    # Commit the fleet:
    set_tenant_event_types --all-tenants --commit
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

from events.models import Event, EventType
from recaps.models import CustomRecapTemplate
from tenants.models import Tenant

User = get_user_model()

DEFAULT_KEEP = ["Retail Sampling", "On-Premise Sampling", "Event"]
DEFAULT_RETIRE = ["Sampling", "Promotion", "Launch", "Special Event"]


def _split(raw: str | None) -> list[str]:
    """Comma-separated CLI arg → trimmed, de-duped, order-preserving list."""
    out: list[str] = []
    seen: set[str] = set()
    for piece in (raw or "").split(","):
        name = piece.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


class Command(BaseCommand):
    help = (
        "Standardize tenant EventTypes to Retail Sampling / On-Premise "
        "Sampling / Event, retiring legacy stock types (keeping custom ones). "
        "Dry-run by default; pass --commit to write."
    )

    def add_arguments(self, parser):
        scope = parser.add_mutually_exclusive_group(required=True)
        scope.add_argument(
            "--tenant-name",
            default=None,
            help="Process a single tenant by name (case-insensitive).",
        )
        scope.add_argument(
            "--all-tenants",
            action="store_true",
            help="Process every tenant (minus --exclude).",
        )
        parser.add_argument(
            "--exclude",
            default="Jeeter",
            help="Comma-separated tenant names to skip (default: Jeeter).",
        )
        parser.add_argument(
            "--keep",
            default=",".join(DEFAULT_KEEP),
            help="Comma-separated event types to ensure exist.",
        )
        parser.add_argument(
            "--retire",
            default=",".join(DEFAULT_RETIRE),
            help="Comma-separated legacy event types to remove (repointed first).",
        )
        parser.add_argument(
            "--default-type",
            default="Retail Sampling",
            help="Which kept type becomes is_default (when the default was retired).",
        )
        parser.add_argument(
            "--repoint-to",
            default="Retail Sampling",
            help="Kept type that adopts events/templates off retired types.",
        )
        parser.add_argument(
            "--owner-email",
            default="kyle@igniteproductions.co",
            help="User recorded as created_by on any new types.",
        )
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Actually write. Without this flag it's a dry-run.",
        )

    def handle(self, *args, **opts):
        commit = bool(opts["commit"])
        keep = _split(opts["keep"])
        retire = _split(opts["retire"])
        excludes = _split(opts["exclude"])
        default_type = opts["default_type"].strip()
        repoint_to = opts["repoint_to"].strip()

        if not keep:
            raise CommandError("--keep cannot be empty.")
        keep_lower = {k.lower() for k in keep}
        retire_lower = {r.lower() for r in retire}
        overlap = keep_lower & retire_lower
        if overlap:
            raise CommandError(
                f"--keep and --retire overlap: {sorted(overlap)}. "
                "A type can't be both kept and retired."
            )
        if default_type.lower() not in keep_lower:
            raise CommandError(
                f"--default-type {default_type!r} must be one of --keep ({keep})."
            )
        if repoint_to and repoint_to.lower() not in keep_lower:
            raise CommandError(
                f"--repoint-to {repoint_to!r} must be one of --keep ({keep})."
            )

        owner = (
            User.objects.filter(email__iexact=opts["owner_email"])
            .order_by("id")
            .first()
        )
        if not owner:
            raise CommandError(f"Owner user not found: {opts['owner_email']}")

        # ---- Resolve tenant scope -----------------------------------------
        if opts["all_tenants"]:
            tenants = list(Tenant.objects.order_by("name"))
        else:
            tenants = list(
                Tenant.objects.filter(name__iexact=opts["tenant_name"].strip())
                .order_by("id")
            )
            if not tenants:
                candidates = list(
                    Tenant.objects.order_by("name").values_list("name", flat=True)[:60]
                )
                raise CommandError(
                    f"Tenant not found by name {opts['tenant_name']!r}. "
                    f"Existing: {', '.join(candidates) or '(none)'}"
                )

        excluded = [t for t in tenants if t.name.strip().lower() in {e.lower() for e in excludes}]
        targets = [t for t in tenants if t.name.strip().lower() not in {e.lower() for e in excludes}]

        w = self.stdout.write
        w("")
        w(self.style.MIGRATE_HEADING("Standardize tenant event types"))
        w(f"  mode        : {'COMMIT (writing)' if commit else 'DRY-RUN (no writes)'}")
        w(f"  keep        : {keep}")
        w(f"  retire      : {retire}")
        w(f"  default     : {default_type}   repoint→ {repoint_to or '(none — skip referenced)'}")
        w(f"  owner       : {owner.id} ({owner.email})")
        w(f"  tenants     : {len(targets)} target(s)"
          + (f", {len(excluded)} excluded ({', '.join(t.name for t in excluded)})" if excluded else ""))
        w("")

        totals = {"created": 0, "retired": 0, "repointed_events": 0,
                  "repointed_templates": 0, "kept_custom": 0, "errors": 0}

        for tenant in targets:
            try:
                self._process_tenant(
                    tenant=tenant,
                    keep=keep,
                    keep_lower=keep_lower,
                    retire_lower=retire_lower,
                    default_type=default_type,
                    repoint_to=repoint_to,
                    owner=owner,
                    commit=commit,
                    totals=totals,
                    w=w,
                )
            except Exception as exc:  # noqa: BLE001 — report + continue per-tenant
                totals["errors"] += 1
                w(self.style.ERROR(f"  ! {tenant.name}: {exc}"))

        w("")
        w(self.style.SUCCESS("Totals"))
        w(f"  types {'created' if commit else 'to create'}        : {totals['created']}")
        w(f"  types {'retired' if commit else 'to retire'}        : {totals['retired']}")
        w(f"  events {'repointed' if commit else 'to repoint'}     : {totals['repointed_events']}")
        w(f"  templates {'repointed' if commit else 'to repoint'}  : {totals['repointed_templates']}")
        w(f"  custom types kept       : {totals['kept_custom']}")
        if totals["errors"]:
            w(self.style.WARNING(f"  tenants with errors     : {totals['errors']}"))
        if not commit:
            w("")
            w(self.style.MIGRATE_LABEL(
                "DRY-RUN complete — nothing written. Re-run with --commit "
                "(execute=true) to apply."
            ))

    def _process_tenant(self, *, tenant, keep, keep_lower, retire_lower,
                         default_type, repoint_to, owner, commit, totals, w):
        existing = list(EventType.objects.filter(tenant=tenant))
        by_lower = {t.name.strip().lower(): t for t in existing}

        to_create = [k for k in keep if k.lower() not in by_lower]
        to_retire = [t for t in existing if t.name.strip().lower() in retire_lower]
        kept_custom = [
            t for t in existing
            if t.name.strip().lower() not in keep_lower
            and t.name.strip().lower() not in retire_lower
        ]

        # Per-retire reference counts (what blocks a plain delete).
        retire_plan = []
        for t in to_retire:
            n_events = Event.objects.filter(event_type=t).count()
            n_templates = CustomRecapTemplate.objects.filter(event_type=t).count()
            retire_plan.append((t, n_events, n_templates))

        cur_default = next((t for t in existing if t.is_default), None)
        default_was_retired = bool(
            cur_default and cur_default.name.strip().lower() in retire_lower
        )
        need_default_fix = (cur_default is None) or default_was_retired

        # ---- Report this tenant -------------------------------------------
        head = f"  {tenant.name} (id {tenant.id})"
        w(self.style.HTTP_INFO(head))
        w(f"     current : {', '.join(t.name for t in existing) or '(none)'}")
        if to_create:
            w(f"     {'create ' if commit else 'would create '}: {', '.join(to_create)}")
        for t, ne, nt in retire_plan:
            refs = []
            if ne:
                refs.append(f"{ne} event(s)→{repoint_to}")
            if nt:
                refs.append(f"{nt} template(s)→{repoint_to}")
            ref_txt = f"  [{', '.join(refs)}]" if refs else "  [unused]"
            blocked = (ne or nt) and not repoint_to
            verb = "retire" if commit else "would retire"
            if blocked:
                w(self.style.WARNING(
                    f"     SKIP {t.name}{ref_txt} — referenced and no --repoint-to"))
            else:
                w(f"     {verb} {t.name}{ref_txt}")
        if kept_custom:
            w(f"     keep    : {', '.join(t.name for t in kept_custom)}")
        if need_default_fix:
            w(f"     default : {(cur_default.name if cur_default else '(none)')} → {default_type}")

        totals["created"] += len(to_create)
        totals["kept_custom"] += len(kept_custom)

        if not commit:
            for t, ne, nt in retire_plan:
                if not ((ne or nt) and not repoint_to):
                    totals["retired"] += 1
                    totals["repointed_events"] += ne
                    totals["repointed_templates"] += nt
            return

        # ---- Apply (atomic per tenant) ------------------------------------
        with transaction.atomic():
            # 1) ensure kept types exist
            for name in keep:
                obj, created = EventType.objects.get_or_create(
                    tenant=tenant,
                    name=name,
                    defaults={
                        "slug": slugify(name),
                        "is_default": False,
                        "created_by": owner,
                    },
                )
            target = None
            if repoint_to:
                target = (
                    EventType.objects.filter(tenant=tenant, name__iexact=repoint_to)
                    .order_by("id")
                    .first()
                )

            # 2) repoint + retire
            for t, ne, nt in retire_plan:
                if (ne or nt) and not target:
                    # referenced but nowhere to move them — leave it (RESTRICT
                    # would block delete anyway). Reported as SKIP above.
                    continue
                if target:
                    moved_e = Event.objects.filter(event_type=t).update(event_type=target)
                    moved_t = CustomRecapTemplate.objects.filter(
                        event_type=t
                    ).update(event_type=target)
                    totals["repointed_events"] += moved_e
                    totals["repointed_templates"] += moved_t
                t.delete()
                totals["retired"] += 1

            # 3) fix the default if it was retired / missing
            if need_default_fix:
                dflt = (
                    EventType.objects.filter(tenant=tenant, name__iexact=default_type)
                    .order_by("id")
                    .first()
                )
                if dflt:
                    EventType.objects.filter(tenant=tenant, is_default=True).exclude(
                        id=dflt.id
                    ).update(is_default=False)
                    if not dflt.is_default:
                        dflt.is_default = True
                        dflt.save(update_fields=["is_default"])
