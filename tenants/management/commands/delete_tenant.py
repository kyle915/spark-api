"""Delete a tenant row and its dependent scaffolding. DESTRUCTIVE.

Written for the duplicate-Sipli case: two Tenant rows shared a name AND a slug
(``Tenant.slug`` has no unique constraint), leaving every name-based tool
ambiguous. One row held the client's real work; the other was an empty shell
that onboarding had seeded with statuses and types and nothing else.

WHY THIS TAKES AN ID, NOT A NAME
    The whole failure being cleaned up is that the name is ambiguous. A tool
    that resolves a delete target by name could resolve to the wrong row — the
    one with the client's recaps on it. ``--tenant-id`` only.

WHY A FIXPOINT LOOP AND NOT ``tenant.delete()``
    Tenant FKs across this schema are ``on_delete=RESTRICT`` (RequestStatus,
    RequestType, ProductType, Client, Distributor, Retailer, BillingEntity …),
    so Django raises rather than cascading. Children are deleted first, looping
    until nothing is left, so inter-child dependencies resolve without anyone
    hand-maintaining a delete order that would rot the moment a model is added.

TWO GUARDS, BOTH ON BY DEFAULT
    1. Refuses if the tenant holds real client work — events, requests, recaps,
       templates, products, jobs. Scaffolding is disposable; field data is not,
       and no flag on this command overrides that. If you genuinely need to drop
       a tenant with work on it, that is a different, considered job.
    2. Refuses if any user's ONLY membership is this tenant, which would strip
       their access with nothing to fall back to. ``--allow-orphan-users`` opts
       out once you've read the list it prints.

DRY-RUN by default. ``--apply`` writes. Prints a full inventory either way, so
the log is the record of what was removed.

Usage::

    python manage.py delete_tenant --tenant-id 16
    python manage.py delete_tenant --tenant-id 16 --apply
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

# Relations that mean "this tenant has real client work". Scaffolding (statuses,
# types, categories) is recreated by onboarding; these are not.
WORK_MODELS = {
    ("events", "Event"),
    ("events", "Request"),
    ("recaps", "CustomRecap"),
    ("recaps", "CustomRecapTemplate"),
    ("events", "Product"),
    ("jobs", "Job"),
}


class Command(BaseCommand):
    help = (
        "DESTRUCTIVE. Delete a tenant by id along with its dependent rows. "
        "Refuses if the tenant holds client work or would orphan a user. "
        "Dry-run by default; --apply writes."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-id",
            dest="tenant_id",
            type=int,
            required=True,
            help="EXACT tenant id. Deliberately not a name — the name is the "
                 "ambiguous thing this exists to clean up.",
        )
        parser.add_argument(
            "--allow-orphan-users",
            dest="allow_orphan_users",
            action="store_true",
            help="proceed even if some user's only membership is this tenant "
                 "(read the printed list first).",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="actually delete (omit for a dry run that changes nothing).",
        )

    def handle(self, *args, **opts):
        from tenants.models import Tenant

        apply = bool(opts["apply"])
        tid = opts["tenant_id"]

        tenant = Tenant.objects.filter(id=tid).first()
        if tenant is None:
            raise CommandError(f"No tenant with id={tid}.")

        self.stdout.write("=" * 72)
        self.stdout.write(
            f"TARGET: [{tenant.id}] {tenant.name!r} slug={tenant.slug!r} "
            f"request_url_name={getattr(tenant, 'request_url_name', None)!r}"
        )
        self.stdout.write(
            f"MODE  : {'APPLY (deleting)' if apply else 'DRY-RUN (no writes)'}"
        )
        self.stdout.write("=" * 72)

        # ---- inventory -------------------------------------------------
        rows: list[tuple[str, str, int]] = []
        work_hits: list[tuple[str, int]] = []
        for rel in Tenant._meta.related_objects:
            model = rel.related_model
            key = (model._meta.app_label, model.__name__)
            label = f"{model._meta.app_label}.{model.__name__}"
            accessor = rel.get_accessor_name()
            try:
                mgr = getattr(tenant, accessor, None)
                if mgr is None:
                    continue
                n = mgr.count() if hasattr(mgr, "count") else 1
            except Exception as exc:  # noqa: BLE001
                self.stdout.write(self.style.WARNING(f"  ! {label}: {exc}"))
                continue
            if n:
                rows.append((label, accessor, n))
                if key in WORK_MODELS:
                    work_hits.append((label, n))

        rows.sort(key=lambda r: (-r[2], r[0]))
        self.stdout.write("\nCONTENTS")
        for label, _, n in rows:
            self.stdout.write(f"  {n:>7}  {label}")
        total = sum(n for _, _, n in rows)
        self.stdout.write(f"\n  TOTAL related rows: {total}")

        # ---- guard 1: real client work ---------------------------------
        if work_hits:
            detail = ", ".join(f"{lbl}={n}" for lbl, n in work_hits)
            raise CommandError(
                f"REFUSING: tenant {tid} holds client work ({detail}). "
                "This command only removes empty duplicates. Deleting a tenant "
                "with field data on it is a different, considered job."
            )
        self.stdout.write(
            self.style.SUCCESS("\n  guard: no client work on this tenant — OK")
        )

        # ---- guard 2: users left with no tenant ------------------------
        orphans = self._orphan_users(tenant)
        if orphans:
            self.stdout.write(
                self.style.WARNING(
                    f"\n  {len(orphans)} user(s) whose ONLY membership is this "
                    "tenant — deleting it strips their access:"
                )
            )
            for label in orphans:
                self.stdout.write(f"    - {label}")
            if not opts["allow_orphan_users"]:
                raise CommandError(
                    "REFUSING: the users above would be left with no tenant. "
                    "Move them to the surviving tenant first, or re-run with "
                    "--allow-orphan-users once you've read that list."
                )
            self.stdout.write(
                self.style.WARNING("  --allow-orphan-users given — proceeding.")
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    "  guard: every member also belongs to another tenant — OK"
                )
            )

        if not apply:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    f"DRY-RUN — would delete {total} related row(s) and then "
                    f"tenant [{tid}] itself. Re-run with --apply to write."
                )
            )
            return

        # ---- delete ----------------------------------------------------
        with transaction.atomic():
            deleted_total = 0
            for sweep in range(1, 11):
                remaining = 0
                progressed = False
                for rel in Tenant._meta.related_objects:
                    accessor = rel.get_accessor_name()
                    mgr = getattr(tenant, accessor, None)
                    if mgr is None or not hasattr(mgr, "all"):
                        continue
                    qs = mgr.all()
                    n = qs.count()
                    if not n:
                        continue
                    try:
                        count, _ = qs.delete()
                        deleted_total += count
                        progressed = True
                    except Exception:  # noqa: BLE001 — blocked this sweep
                        remaining += n
                if remaining == 0:
                    break
                if not progressed:
                    raise CommandError(
                        f"Stuck after sweep {sweep}: {remaining} row(s) could "
                        "not be deleted (a RESTRICT chain this loop can't "
                        "resolve). Nothing was committed."
                    )

            name = tenant.name
            tenant.delete()

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted tenant [{tid}] {name!r} and {deleted_total} "
                "dependent row(s)."
            )
        )

    # ------------------------------------------------------------------

    def _orphan_users(self, tenant) -> list[str]:
        """Users whose only tenant membership is this one."""
        try:
            from tenants.models import TenantedUser
        except Exception:  # noqa: BLE001
            return []

        out: list[str] = []
        rows = TenantedUser.objects.filter(tenant=tenant).select_related("user")
        for row in rows:
            user = getattr(row, "user", None)
            if user is None:
                continue
            others = (
                TenantedUser.objects.filter(user=user)
                .exclude(tenant=tenant)
                .count()
            )
            if others == 0:
                out.append(
                    f"{getattr(user, 'email', None) or getattr(user, 'id', '?')}"
                )
        return out
