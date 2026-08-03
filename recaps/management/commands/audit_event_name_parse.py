"""Audit which of a tenant's events actually parse into a market — the
read-only diagnostic behind "why is the Field Sampling Report dropping
volume?"

Every per-market rollup in :mod:`recaps.field_sampling_report` (and the
metro-week grid in :mod:`recaps.tenant_overview`) resolves an event's
market by PARSING ``Event.name`` for the ``"<Market> — <Corridor> ·
<date>"`` convention. An event whose name doesn't match that shape yields
``market=None`` and falls out of every per-market total silently — the
unfiltered ``overall`` still counts it, so the only visible symptom is
``overall`` exceeding the sum of the listed markets.

This command explains that gap instead of leaving it to be guessed at. It
splits the gap into its TWO distinct causes, which need different fixes:

  * **unparseable names** — no `` — `` separator at all (e.g. the standing
    check-in link's ``"8/2/2026 - Austin"``). Grouped by a digit-redacted
    shape signature so a new naming convention shows up as one row rather
    than 3,000 near-identical strings.
  * **a market label that parses fine but isn't in the caller's list** —
    e.g. an event named ``"Tampa — ..."`` when the report asks for
    ``"Tampa / St. Pete"``. Reported under ``markets`` with
    ``in_requested_list: false``.

Both the parser and the window filter are IMPORTED, never re-implemented,
so this audit can't drift from the report it's diagnosing. Sample volume
is counted on the same ``CustomRecapProductSample.quantity`` basis
:func:`recaps.field_sampling_report.sku_breakdown` uses in ``"quantity"``
mode, so the per-bucket numbers here reconcile against that report's
``overall``.

STRICTLY READ-ONLY — no writes, no email, no side effects. Prod:
``/internal/cron/audit-event-name-parse`` + the ``audit-event-name-parse``
workflow.
"""

from __future__ import annotations

import datetime
import json
import re

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Sum
from django.utils import timezone

# Digit runs collapse to "#", keeping the words: readable enough to eyeball
# the convention ("#/#/# - Austin").
_DIGITS_RE = re.compile(r"\d+")
# The GROUPING key. Every run of non-separator characters collapses to "<t>",
# leaving only the punctuation the parser actually keys on. Digit redaction
# alone is not enough: it still yields one row per market ("#/#/# - Austin",
# "#/#/# - Miami") and, for address-mode walk-ins, one row per street address
# — hundreds of rows that bury the single convention they all share. Both
# collapse to "<t>/<t>/<t>-<t>" here, while the canonical
# "<Market> — <Corridor> · <date>" stays distinct as "<t>—<t>·<t>/<t>".
_TOKEN_RE = re.compile(r"[^—·\-/|,()]+")

MAX_EXAMPLES = 8
MAX_VARIANTS = 8
MAX_SHAPES = 25
MAX_CREATORS = 10


def _shape(name: str) -> str:
    """Digit-redacted name — the human-readable variant label."""
    return _DIGITS_RE.sub("#", name or "")


def _coarse_shape(name: str) -> str:
    """Separator-only signature — the grouping key (see :data:`_TOKEN_RE`)."""
    return _TOKEN_RE.sub("<t>", name or "").strip()


class Command(BaseCommand):
    help = (
        "Explain the Field Sampling Report's market gap: which events parse "
        "into a market, which don't, and which parse to an unrequested "
        "label. Read-only JSON."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            default="feel-free",
            help="tenant slug or numeric id (default: feel-free)",
        )
        parser.add_argument(
            "--start",
            default="",
            help="YYYY-MM-DD inclusive window start (default: Jan 1 this year)",
        )
        parser.add_argument(
            "--end",
            default="",
            help="YYYY-MM-DD inclusive window end (default: today)",
        )
        parser.add_argument(
            "--markets",
            default="",
            help=(
                "comma-separated market labels to check against (blank = the "
                "same five metros dump_field_sampling defaults to)"
            ),
        )
        parser.add_argument(
            "--event-type",
            default="",
            help="restrict to one event-type name (e.g. 'Field Sampling')",
        )

    def handle(self, *args, **opts):
        from events.models import EventType
        from recaps.field_sampling_report import _parse_event_name
        from recaps.management.commands.dump_field_sampling import DEFAULT_MARKETS
        from recaps.models import CustomRecap, CustomRecapProductSample
        from recaps.tenant_overview import _filter_event_window
        from tenants.models import Tenant

        ident = str(opts["tenant"]).strip()
        tenant = (
            Tenant.objects.filter(id=int(ident)).first()
            if ident.isdigit()
            else Tenant.objects.filter(slug=ident).first()
        )
        if tenant is None:
            names = ", ".join(f"{t.id}:{t.slug}" for t in Tenant.objects.order_by("id"))
            raise CommandError(f"No tenant matches {ident!r}. Known: {names}")

        requested = [
            m.strip() for m in str(opts["markets"]).split(",") if m.strip()
        ] or list(DEFAULT_MARKETS)

        def _date(raw: str, field: str) -> datetime.date | None:
            raw = str(raw or "").strip()
            if not raw:
                return None
            try:
                return datetime.date.fromisoformat(raw)
            except ValueError as exc:
                raise CommandError(f"Bad --{field} {raw!r}: {exc}")

        start_d = _date(opts["start"], "start")
        end_d = _date(opts["end"], "end")
        today = timezone.localdate()
        if start_d is None:
            start_d = today.replace(month=1, day=1)
        if end_d is None:
            end_d = today
        if end_d < start_d:
            raise CommandError(f"--end {end_d} precedes --start {start_d}")

        def _aware_midnight(d: datetime.date) -> datetime.datetime:
            return timezone.make_aware(datetime.datetime.combine(d, datetime.time.min))

        # Half-open [start, end+1day) so the --end calendar day is INCLUSIVE,
        # matching dump_field_sampling's `window` block.
        w_start = _aware_midnight(start_d)
        w_end = _aware_midnight(end_d + datetime.timedelta(days=1))

        event_type_id = None
        et_name = str(opts["event_type"]).strip()
        if et_name:
            et = EventType.objects.filter(
                tenant_id=tenant.id, name__iexact=et_name
            ).first() or EventType.objects.filter(name__iexact=et_name).first()
            if et is None:
                raise CommandError(f"No event type named {et_name!r}")
            event_type_id = et.id

        # Audit the SAME row set the report aggregates: tenant CustomRecaps
        # windowed on the effective event date. Anything this filter drops
        # (soft-deleted request, exclude_from_dashboard) is absent from the
        # report too, so the reconciliation stays honest.
        base = CustomRecap.objects.filter(tenant_id=tenant.id)
        if event_type_id is not None:
            base = base.filter(event__event_type_id=event_type_id)
        windowed = _filter_event_window(base, "event__", (w_start, w_end))

        rows = list(
            windowed.values_list(
                "id",
                "event_id",
                "event__name",
                "event__address",
                "event__request_id",
                "event__created_at",
                "event__created_by__email",
                "event__event_type__name",
            )
        )

        # Structured per-SKU quantities, the basis sku_breakdown sums in
        # "quantity" mode — fetched once and joined in Python rather than
        # per-bucket queries.
        qty_by_recap: dict[int, int] = dict(
            CustomRecapProductSample.objects.filter(
                custom_recap_id__in=[r[0] for r in rows]
            )
            .values_list("custom_recap_id")
            .annotate(total=Sum("quantity"))
            .values_list("custom_recap_id", "total")
        )

        requested_lc = {m.lower() for m in requested}
        markets: dict[str, dict] = {}
        shapes: dict[str, dict] = {}
        totals = {
            "recaps": len(rows),
            "events": len({r[1] for r in rows if r[1] is not None}),
            "samples": 0,
            "parsed_recaps": 0,
            "parsed_samples": 0,
            "unparsed_recaps": 0,
            "unparsed_samples": 0,
            "unrequested_market_recaps": 0,
            "unrequested_market_samples": 0,
        }

        for (
            recap_id,
            event_id,
            name,
            address,
            request_id,
            created_at,
            created_by,
            et,
        ) in rows:
            qty = int(qty_by_recap.get(recap_id) or 0)
            totals["samples"] += qty
            market, _corridor = _parse_event_name(name)

            if market is None:
                totals["unparsed_recaps"] += 1
                totals["unparsed_samples"] += qty
                sig = _coarse_shape(name or "")
                b = shapes.setdefault(
                    sig,
                    {
                        "shape": sig,
                        "recaps": 0,
                        "samples": 0,
                        "event_ids": set(),
                        "variants": {},
                        "examples": [],
                        "addresses": set(),
                        "event_types": set(),
                        "created_by": {},
                        "via_request": 0,
                        "standalone": 0,
                        "first_created_at": None,
                        "last_created_at": None,
                    },
                )
                b["recaps"] += 1
                b["samples"] += qty
                if event_id is not None:
                    b["event_ids"].add(event_id)
                variant = _shape(name or "")
                b["variants"][variant] = b["variants"].get(variant, 0) + 1
                if name and len(b["examples"]) < MAX_EXAMPLES and name not in b["examples"]:
                    b["examples"].append(name)
                if address:
                    b["addresses"].add(str(address)[:80])
                if et:
                    b["event_types"].add(et)
                who = created_by or "(none)"
                b["created_by"][who] = b["created_by"].get(who, 0) + 1
                if request_id is None:
                    b["standalone"] += 1
                else:
                    b["via_request"] += 1
                if created_at is not None:
                    iso = created_at.isoformat()
                    if b["first_created_at"] is None or iso < b["first_created_at"]:
                        b["first_created_at"] = iso
                    if b["last_created_at"] is None or iso > b["last_created_at"]:
                        b["last_created_at"] = iso
                continue

            totals["parsed_recaps"] += 1
            totals["parsed_samples"] += qty
            m = markets.setdefault(
                market,
                {
                    "market": market,
                    "recaps": 0,
                    "samples": 0,
                    "event_ids": set(),
                    "in_requested_list": market in requested,
                    # A label differing only by case still misses an
                    # exact-match market filter — call that out separately
                    # from a genuinely new market.
                    "case_insensitive_match": market.lower() in requested_lc,
                },
            )
            m["recaps"] += 1
            m["samples"] += qty
            if event_id is not None:
                m["event_ids"].add(event_id)
            if not m["in_requested_list"]:
                totals["unrequested_market_recaps"] += 1
                totals["unrequested_market_samples"] += qty

        # The gap a caller sees as `overall` minus the sum of their markets.
        totals["gap_samples"] = (
            totals["unparsed_samples"] + totals["unrequested_market_samples"]
        )

        market_rows = sorted(
            (
                {
                    **{k: v for k, v in m.items() if k != "event_ids"},
                    "events": len(m["event_ids"]),
                }
                for m in markets.values()
            ),
            key=lambda r: (-r["samples"], r["market"]),
        )
        shape_rows = sorted(
            (
                {
                    **{
                        k: v
                        for k, v in b.items()
                        if k
                        not in (
                            "event_ids",
                            "addresses",
                            "event_types",
                            "created_by",
                            "variants",
                        )
                    },
                    "events": len(b["event_ids"]),
                    "variants": sorted(
                        b["variants"].items(), key=lambda kv: -kv[1]
                    )[:MAX_VARIANTS],
                    "sample_addresses": sorted(b["addresses"])[:MAX_EXAMPLES],
                    "event_types": sorted(b["event_types"]),
                    "created_by": sorted(
                        b["created_by"].items(), key=lambda kv: -kv[1]
                    )[:MAX_CREATORS],
                }
                for b in shapes.values()
            ),
            key=lambda r: (-r["samples"], r["shape"]),
        )

        out = {
            "tenant": {"id": tenant.id, "slug": tenant.slug, "name": tenant.name},
            "window": {"start": start_d.isoformat(), "end": end_d.isoformat()},
            "event_type": et_name or None,
            "requested_markets": requested,
            "totals": totals,
            "markets": market_rows,
            "unparsed_shapes": shape_rows[:MAX_SHAPES],
            "unparsed_shape_count": len(shape_rows),
        }

        self.stdout.write("ENPARSE_JSON_START")
        self.stdout.write(json.dumps(out, default=str))
        self.stdout.write("ENPARSE_JSON_END")
