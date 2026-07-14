"""Per-recap ACCOUNT / PRODUCT SPEND breakdown for one tenant — read-only.

Sums the dollar amount logged on each recap as account / corporate-card spend
— the "product spend" a BA (or the agency) put on the account card at an event:

  * CUSTOM recaps  -> ``recaps.types._account_spend_from_fields`` over the
                      recap's custom fields (matches "Account Spend", "Amount
                      Spent", "Product Spend", "Total Spend", "Corporate Card
                      ..."; a boolean "Corporate Card Used?" parses to None and
                      is skipped, so it never fakes a $ amount).
  * LEGACY recaps  -> ``Recap.account_spend_amount`` (typed column).

Because a name-matcher can mis-read a free-text field (Stone House Bread's
consumers audit once digit-mashed a demographics sentence into a bogus number),
this ALSO prints a "money-ish field census": every DISTINCT custom-field NAME
that could plausibly hold money (spend / spent / cost / amount / $ / paid /
price / budget / expense / invest) with how many recaps carry it and the sum it
parses to. A ``✓`` marks the fields actually counted toward the spend total, so
the true "product spend" field is visible at the FIELD level before anyone
trusts the grand total.

READ-ONLY. No writes, no email. Window defaults to ALL-TIME (every recap ever);
``--year N`` scopes to one calendar year.

Run via the ``/internal/cron/audit-tenant-account-spend`` endpoint (or the
``Audit tenant account spend`` GitHub Action) so it executes against prod.
"""

from __future__ import annotations

import re

from django.core.management.base import BaseCommand

from recaps.management.commands.audit_tenant_consumers import _resolve_tenant
from recaps.models import CustomFieldValue, Recap
from recaps.tenant_overview import _filter_event_window, _year_bounds
from recaps.types import (
    _ACCOUNT_SPEND_RE,
    _account_spend_from_fields,
    _parse_recap_money,
)

# Broad net for the census — anything whose NAME might hold a dollar amount, so
# a differently-named "product spend" field can't hide from us. Wider on
# purpose than the canonical _ACCOUNT_SPEND_RE (which is what actually sums).
_MONEYISH_RE = re.compile(
    r"spend|spent|\bcost\b|amount|\bpaid\b|\bprice\b|budget|expense|\$|dollar|invest",
    re.IGNORECASE,
)


class Command(BaseCommand):
    help = "Read-only per-recap account/product-spend breakdown for one tenant."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", required=True, help="id, request-url-name, or name")
        parser.add_argument("--year", type=int, default=None, help="scope to one calendar year")
        parser.add_argument(
            "--all-time", action="store_true",
            help="every recap ever (this is the default when --year is omitted)",
        )

    def handle(self, *args, **opts):
        tenant = _resolve_tenant(opts["tenant"])
        if opts["year"]:
            window = _year_bounds(opts["year"])
            label = str(opts["year"])
        else:
            window = None
            label = "all-time"

        w = self.stdout.write
        w(f"Account / product spend audit — {tenant.name} (id {tenant.id}) · {label}")
        w("=" * 72)

        # --- CUSTOM recaps: gather every (field-name, value) pair per recap.
        cfv = _filter_event_window(
            CustomFieldValue.objects.filter(custom_recap__tenant_id=tenant.id),
            "custom_recap__event__",
            window,
        ).values_list(
            "custom_recap_id", "custom_recap__name", "custom_field__name", "value",
        )
        by_recap: dict = {}
        for rid, rname, fname, val in cfv.iterator():
            row = by_recap.setdefault(rid, {"name": rname, "pairs": []})
            row["pairs"].append((fname, val))

        custom_total = 0.0
        custom_with_spend = 0
        # census: field-name -> {recaps, parsed_n, sum, samples}
        census: dict = {}
        w("\nCUSTOM recaps with a matched account/product-spend field:")
        for rid, row in sorted(by_recap.items()):
            spend = _account_spend_from_fields(row["pairs"])
            if spend is not None:
                custom_total += spend
                custom_with_spend += 1
                matched = "; ".join(
                    f"{fn!r}={v!r}"
                    for fn, v in row["pairs"]
                    if fn and _ACCOUNT_SPEND_RE.search(fn)
                    and _parse_recap_money(v) is not None
                )
                w(
                    f"  recap {rid} · {(row['name'] or '')[:32]:32} "
                    f"$ {spend:>10,.2f}  [{matched}]"
                )
            # Build the money-ish census from ALL fields (matched or not).
            for fn, v in row["pairs"]:
                if not fn or not _MONEYISH_RE.search(fn):
                    continue
                c = census.setdefault(
                    fn, {"recaps": 0, "parsed_n": 0, "sum": 0.0, "samples": []}
                )
                c["recaps"] += 1
                money = _parse_recap_money(v)
                if money is not None:
                    c["parsed_n"] += 1
                    c["sum"] += money
                if v and len(c["samples"]) < 3:
                    c["samples"].append(str(v)[:28])
        w(
            f"  -> {len(by_recap)} custom recaps · {custom_with_spend} with spend · "
            f"SUM = ${custom_total:,.2f}"
        )

        # --- Money-ish field census: catch mis-matches + differently-named fields.
        w("\nMONEY-ISH custom fields  [✓=counted · name · #recaps · #numeric · sum · samples]")
        if census:
            for fn, c in sorted(census.items(), key=lambda kv: -kv[1]["sum"]):
                canon = "✓" if _ACCOUNT_SPEND_RE.search(fn) else " "
                samp = ", ".join(c["samples"])
                w(
                    f"  {canon} {fn[:42]:42} {c['recaps']:>4} {c['parsed_n']:>4}  "
                    f"${c['sum']:>12,.2f}  [{samp}]"
                )
        else:
            w("  (no money-ish custom fields found)")

        # --- LEGACY recaps: typed account_spend_amount column.
        legacy_rows = list(
            _filter_event_window(
                Recap.objects.filter(event__tenant_id=tenant.id), "event__", window
            ).values_list("id", "name", "account_spend_amount")
        )
        legacy_total = 0.0
        legacy_with_spend = 0
        w("\nLEGACY recaps with account_spend_amount set:")
        for rid, rname, amt in legacy_rows:
            if amt:
                legacy_total += float(amt)
                legacy_with_spend += 1
                w(f"  recap {rid} · {(rname or '')[:32]:32} $ {float(amt):>10,.2f}")
        w(
            f"  -> {len(legacy_rows)} legacy recaps · {legacy_with_spend} with spend · "
            f"SUM = ${legacy_total:,.2f}"
        )

        # --- Grand total.
        grand = custom_total + legacy_total
        w("\n" + "=" * 72)
        w(
            f"GRAND TOTAL account/product spend "
            f"(custom ${custom_total:,.2f} + legacy ${legacy_total:,.2f}) = ${grand:,.2f}"
        )
        w(f"recaps carrying spend: {custom_with_spend + legacy_with_spend}")
