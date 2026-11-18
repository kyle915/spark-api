"""Fill in each recap's retailer, store number, city and state.

Reads what a tenant's recaps already carry plus their location text (event
and request addresses, typed store, the recap's own store / city / state /
address answers) and proposes, per recap, a canonical retailer, store number,
city and state. The AI text model is asked only about what is still blank,
and is given location text only. See :mod:`recaps.recap_location`.

DRY-RUN BY DEFAULT: prints counts and every recap's before -> after without
writing. ``--apply`` writes. Only blanks are filled, plus a retailer that is
the same chain spelled another way ("heb" -> "H-E-B"); a retailer set on the
recap by hand is never replaced. Idempotent: a second run proposes nothing new.

    python manage.py normalize_recap_locations --tenant torch-thc
    python manage.py normalize_recap_locations --tenant 17 --since 2026-06-01 --limit 200
    python manage.py normalize_recap_locations --tenant girl-beer --apply
"""

from __future__ import annotations

import json
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from recaps.recap_location import DEFAULT_MAX_AI_CALLS, resolve_tenant, run

JSON_START = "RECAPLOCATION_JSON_START"
JSON_END = "RECAPLOCATION_JSON_END"


def parse_since(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise CommandError(f"bad --since (expected YYYY-MM-DD): {value}") from exc


def format_report(report: dict, *, show: int = 500) -> list[str]:
    """Human-readable lines for a run report (no people data in it)."""
    lines = [
        f"tenant={report['tenant']['slug'] or report['tenant']['id']} "
        f"applied={report['applied']} scanned={report['scanned']} "
        f"with_changes={report['recaps_with_changes']} written={report['recaps_written']}",
        "fills: " + ", ".join(f"{k}={v}" for k, v in report["fills"].items()),
        "blank before: " + ", ".join(f"{k}={v}" for k, v in report["blank_before"].items()),
        "blank after:  " + ", ".join(f"{k}={v}" for k, v in report["blank_after"].items()),
        "by source: " + (", ".join(f"{k}={v}" for k, v in report["by_source"].items()) or "-"),
        f"ai: {report['ai']['status']} (calls={report['ai']['calls']})",
    ]
    if report.get("created_retailers"):
        lines.append("created retailers: " + ", ".join(report["created_retailers"]))
    if report.get("created_locations"):
        lines.append("created cities: " + ", ".join(report["created_locations"]))
    for row in report["rows"][:show]:
        tag = "SET" if report["applied"] and row.get("written") else ("WOULD-SET" if row["changes"] else "NOTE")
        diff = "; ".join(
            f"{f}: {row['before'].get(f) or '-'!s} -> {v} [{row['sources'].get(f, '')}/{row['confidence'].get(f, '')}]"
            for f, v in row["changes"].items()
        )
        lines.append(
            f"[{tag}] recap {row['recap_id']} ({row['event_date'] or 'no date'}) {diff}".rstrip()
        )
        for note in row["notes"]:
            lines.append(f"    note: {note}")
    if len(report["rows"]) > show:
        lines.append(f"... {len(report['rows']) - show} more rows")
    return lines


class Command(BaseCommand):
    help = "Propose (dry-run) or write (--apply) each recap's retailer, store number, city and state."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", required=True, help="tenant id, slug or name")
        parser.add_argument("--since", default=None, help="only recaps whose EVENT date is on/after YYYY-MM-DD")
        parser.add_argument("--limit", type=int, default=None, help="at most this many recaps (oldest first)")
        parser.add_argument("--apply", action="store_true", help="write the changes (default: dry-run)")
        parser.add_argument("--no-ai", action="store_true", help="deterministic only; never call the AI model")
        parser.add_argument("--max-ai-calls", type=int, default=DEFAULT_MAX_AI_CALLS)
        parser.add_argument("--json", action="store_true", dest="as_json", help="print the report as JSON")

    def handle(self, *args, **opts):
        try:
            tenant = resolve_tenant(opts["tenant"])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        report = run(
            tenant,
            since=parse_since(opts.get("since")),
            limit=opts.get("limit"),
            apply=bool(opts.get("apply")),
            use_ai=not opts.get("no_ai"),
            max_ai_calls=opts.get("max_ai_calls") or DEFAULT_MAX_AI_CALLS,
        )
        if opts.get("as_json"):
            self.stdout.write(JSON_START)
            self.stdout.write(json.dumps(report, default=str))
            self.stdout.write(JSON_END)
            return
        for line in format_report(report):
            self.stdout.write(line)
