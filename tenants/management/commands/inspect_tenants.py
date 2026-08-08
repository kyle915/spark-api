"""Read-only inventory for every tenant matching a name/slug substring.

Written for the duplicate-Sipli case: two Tenant rows share a name AND a slug
(``Tenant.slug`` has no unique constraint), so every name-based tool is
ambiguous — ``setup_feel_free_checkin`` refuses to run, and you cannot tell
which row holds the client's real data by looking at the tenant list.

Rather than hard-code a list of related models (and silently miss whichever one
matters), this walks ``Tenant._meta.related_objects`` and counts EVERY reverse
relation Django knows about. If a future model gains a tenant FK it shows up
here without anyone remembering to add it.

STRICTLY READ ONLY — no writes, no deletes. Safe against prod at any time, and
safe to run before a destructive decision, which is the whole point: you should
never remove a tenant row you haven't inventoried.

Usage::

    python manage.py inspect_tenants --name sipli
    python manage.py inspect_tenants --name sipli --all-relations
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

# Fields worth printing verbatim — the ones that decide whether a row is the
# "live" tenant or an empty duplicate.
SCALARS = (
    "id",
    "name",
    "slug",
    "request_url_name",
    "checkin_code",
    "checkin_location_mode",
    "default_track_mileage",
    "linked_sheet_url",
)


class Command(BaseCommand):
    help = (
        "Read-only: per-tenant counts of every related row, for tenants "
        "matching a name/slug substring. Use before any destructive tenant work."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--name",
            required=True,
            help="name/slug substring, case-insensitive (e.g. 'sipli').",
        )
        parser.add_argument(
            "--all-relations",
            action="store_true",
            help=(
                "also list relations whose count is zero (default hides them "
                "so a genuinely empty tenant is obvious at a glance)."
            ),
        )

    def handle(self, *args, **opts):
        from tenants.models import Tenant

        needle = (opts["name"] or "").strip()
        if not needle:
            raise CommandError("--name is required.")

        tenants = list(
            Tenant.objects.filter(
                Q(name__icontains=needle) | Q(slug__icontains=needle)
            ).order_by("id")
        )
        if not tenants:
            raise CommandError(f"No tenant matches {needle!r}.")

        self.stdout.write("=" * 72)
        self.stdout.write(f"Tenants matching {needle!r}: {len(tenants)}")
        self.stdout.write("=" * 72)

        totals: dict[int, int] = {}

        for tenant in tenants:
            self.stdout.write("")
            self.stdout.write("-" * 72)
            for field in SCALARS:
                self.stdout.write(f"  {field:24s}: {getattr(tenant, field, None)!r}")
            self.stdout.write("-" * 72)

            grand = 0
            rows: list[tuple[str, str, int]] = []

            for rel in Tenant._meta.related_objects:
                model = rel.related_model
                label = f"{model._meta.app_label}.{model.__name__}"
                accessor = rel.get_accessor_name()
                try:
                    manager = getattr(tenant, accessor, None)
                    if manager is None:
                        continue
                    # one-to-one reverse gives the object itself, not a manager
                    if not hasattr(manager, "count"):
                        rows.append((label, accessor, 1))
                        grand += 1
                        continue
                    n = manager.count()
                except Exception as exc:  # noqa: BLE001
                    rows.append((label, accessor, -1))
                    self.stdout.write(
                        self.style.WARNING(f"  ! {label}.{accessor}: {exc}")
                    )
                    continue
                rows.append((label, accessor, n))
                grand += max(n, 0)

            rows.sort(key=lambda r: (-r[2], r[0]))
            for label, accessor, n in rows:
                if n == 0 and not opts["all_relations"]:
                    continue
                flag = " (count failed)" if n < 0 else ""
                self.stdout.write(f"  {n:>7}  {label}.{accessor}{flag}")

            shown = sum(1 for _, _, n in rows if n > 0)
            self.stdout.write("")
            self.stdout.write(
                f"  TOTAL related rows: {grand}  "
                f"(across {shown} non-empty relation(s) of {len(rows)} checked)"
            )
            totals[tenant.id] = grand

        self.stdout.write("")
        self.stdout.write("=" * 72)
        self.stdout.write("SUMMARY")
        for tenant in tenants:
            n = totals.get(tenant.id, 0)
            verdict = "EMPTY" if n == 0 else "HAS DATA"
            self.stdout.write(
                f"  [{tenant.id}] {tenant.name!r} slug={tenant.slug!r} "
                f"-> {n} related row(s)  {verdict}"
            )
        self.stdout.write("=" * 72)
        self.stdout.write(
            "\nRead-only. Nothing was modified. Note the house convention for "
            "retiring a tenant is renaming it '[ARCHIVED] <name>' (see WERNSA, "
            "Carbliss) — a hard delete is blocked by RESTRICT FKs the moment "
            "any related row exists, and is unrecoverable when it isn't."
        )
