"""Report recap product pickers whose options aren't the tenant's own products.

READ-ONLY — this command never writes. For every tenant, compares each
recap template's product-choice fields (Products Sampled & aliases, or any
choice field whose options are catalog products) against that tenant's own
Product names, and lists mismatches — especially options that are ANOTHER
tenant's products (a template cloned from a donor brand that kept the
donor's SKUs). See ``recaps/product_options_audit.py`` for the rules.

Usage::

    python manage.py audit_recap_product_options
    python manage.py audit_recap_product_options --tenant-slug coho-imports --include-ok
    python manage.py audit_recap_product_options --json
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from recaps.product_options_audit import audit_product_choice_options, format_report


class Command(BaseCommand):
    help = (
        "Read-only: list recap product-choice options that don't match the "
        "tenant's own Product catalog (e.g. another tenant's products)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--tenant-slug", default=None, help="Exact slug; default all tenants.")
        parser.add_argument(
            "--include-ok", action="store_true", help="Also list product fields with no issues."
        )
        parser.add_argument(
            "--include-archived", action="store_true", help="Include [ARCHIVED] tenants."
        )
        parser.add_argument(
            "--json", action="store_true", help="Print the full result as JSON instead of text."
        )

    def handle(self, *args, **opts):
        result = audit_product_choice_options(
            tenant_slug=opts["tenant_slug"], include_archived=bool(opts["include_archived"])
        )
        if opts["json"]:
            self.stdout.write(json.dumps(result, indent=2, default=str))
            return
        self.stdout.write(format_report(result, include_ok=bool(opts["include_ok"])))
