"""Bulk-create custom recaps (+ their standalone events) for a tenant from a
committed JSON data file.

Built for one-off historical / internal-demo loads where the data arrives as a
spreadsheet, not through the app (e.g. Girl Beer's internal make-good demos at
H-E-B). Each row becomes:

  * a standalone APPROVED ``Event`` — no client ``Request``, so internal /
    non-billable make-goods stay OFF the client Master Tracker (which is
    request-driven) while still feeding the dashboard KPIs, the /recaps list,
    geo, and the Google-Sheet export (all recap/event-driven); and
  * a ``CustomRecap`` on the tenant's recap template, with the row's columns
    mapped to template fields BY NAME (normalized), plus the BA credited via
    ``external_ba_name`` (no ambassador account is created).

Because these recaps are ORM-created (not through the GraphQL mutation) they
skip the submit-time data-quality guard + approval notifications by design —
this is a back-load of already-completed activations, not a live submission.

DRY-RUN IS THE DEFAULT. Without ``--apply`` nothing is written; the report
prints the resolved tenant / template / event_type / timezone / state, the
exact column→field mapping (matched fields with their types, unmapped columns,
and template fields left unfilled), and a per-row preview. Run it through the
secret-gated cron endpoint (``digest.cron_views.ImportDemoRecapsView``) + the
``import-demo-recaps`` GitHub workflow, because prod's DB isn't reachable
locally.

Idempotent: re-running skips any row whose event (same tenant + name + date)
already exists, so an --apply after a dry-run — or a re-dispatch — never
double-creates.

Data files live in ``recaps/management/commands/data/<key>.json``:

    {
      "tenant_slug" | "tenant_name": "...",
      "template_name": null,          # optional; else the tenant's sole template
      "event_type_name": null,        # optional; else template.event_type
      "event_status_slug": "approved",
      "timezone_code": "CDT",
      "state_code": "TX",
      "external_ba_name": "Internal",
      "event_name_prefix": "H-E-B (Internal Demo)",
      "date_key": "Date",             # row key → event date (YYYY-MM-DD)
      "address_key": "Store/Location",# row key → event address
      "local_hour": 12,               # wall-clock hour for the event datetime
      "approved": true,               # mark the recap approved
      "engagements_field": "...",     # optional field name → recap.total_engagements
      "rows": [{"<column>": value, ...}, ...]
    }
"""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone as dj_tz

from events.models import Event, EventStatus, EventType, State, TimeZone
from recaps.models import (
    CustomField,
    CustomFieldValue,
    CustomRecap,
    CustomRecapTemplate,
)
from tenants.models import Tenant

User = get_user_model()

_DATA_DIR = Path(__file__).resolve().parent / "data"
# Only [a-z0-9_] dataset keys — the key maps straight to a filename, so this
# guards against path traversal (../) reaching outside data/.
_KEY_RE = re.compile(r"^[a-z0-9_]+$")


def _norm(s) -> str:
    """Normalize a header / field name for matching: lowercase, collapse
    whitespace, strip."""
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def _type_token(custom_field) -> str:
    return (
        getattr(getattr(custom_field, "custom_field_type", None), "name", "") or "text"
    ).strip().lower()


def _is_multiselect(token: str) -> bool:
    return token.replace("-", "").replace("_", "").replace(" ", "") in {
        "multiselect",
        "multi",
        "multichoice",
        "multipleselect",
        "checkbox",
        "checkboxes",
    }


def _is_number(token: str) -> bool:
    return token in {"number", "int", "integer", "decimal", "float", "numeric"}


def _format_value(custom_field, raw):
    """Return ``(value_str_or_None, note)`` for a CustomFieldValue.

    Numbers are cleaned (3.0 → "3"); multiselect is stored as a JSON array
    (matched against the field's options case-insensitively, with a note for
    any dropped values); everything else is stored as a trimmed string.
    """
    if raw is None:
        return None, None
    token = _type_token(custom_field)

    if _is_number(token):
        if isinstance(raw, bool):
            return ("1" if raw else "0"), None
        if isinstance(raw, float) and raw.is_integer():
            return str(int(raw)), None
        return str(raw).strip(), None

    if _is_multiselect(token):
        parts = [p.strip() for p in re.split(r"[,\n;]", str(raw)) if p.strip()]
        options = list(custom_field.options or [])
        if options:
            by_norm = {_norm(o): o for o in options}
            matched, dropped = [], []
            for p in parts:
                canon = by_norm.get(_norm(p))
                (matched if canon else dropped).append(canon or p)
            note = (
                f"dropped off-list option(s): {', '.join(dropped)}" if dropped else None
            )
            return json.dumps([m for m in matched if m in options]), note
        return json.dumps(parts), None

    text = str(raw).strip()
    return (text or None), None


class Command(BaseCommand):
    help = (
        "Bulk-create custom recaps (+ standalone approved events) for a tenant "
        "from a committed JSON data file. Dry-run by default; pass --apply to "
        "write."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dataset",
            required=True,
            help="Dataset key → recaps/management/commands/data/<key>.json",
        )
        parser.add_argument(
            "--owner-email",
            default="kyle@igniteproductions.co",
            help="User recorded as created_by on the events + recaps.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually write. Without this flag the import is a dry-run.",
        )

    def handle(self, *args, **opts):
        apply = bool(opts["apply"])
        key = (opts["dataset"] or "").strip().lower()
        if not _KEY_RE.match(key):
            raise CommandError(f"Invalid --dataset key: {key!r}")
        path = _DATA_DIR / f"{key}.json"
        if not path.exists():
            raise CommandError(f"Dataset file not found: {path}")

        spec = json.loads(path.read_text())
        rows = spec.get("rows") or []
        if not rows:
            raise CommandError(f"No rows in {path}")

        report = self._run(spec, rows, apply, opts["owner_email"])
        # Emit a machine-readable JSON summary last so the cron endpoint can
        # surface it verbatim in the workflow log.
        self.stdout.write("")
        self.stdout.write("JSON_RESULT: " + json.dumps(report, default=str))
        return

    # ------------------------------------------------------------------ core
    def _run(self, spec, rows, apply, owner_email) -> dict:
        w = self.stdout.write
        report: dict = {"mode": "APPLY" if apply else "DRY_RUN", "rows_in": len(rows)}

        owner = (
            User.objects.filter(email__iexact=owner_email).order_by("id").first()
        )
        if not owner:
            raise CommandError(f"Owner user not found: {owner_email}")

        # ---- Resolve tenant -------------------------------------------------
        tenant = None
        if spec.get("tenant_slug"):
            tenant = Tenant.objects.filter(slug__iexact=spec["tenant_slug"]).first()
        if not tenant and spec.get("tenant_name"):
            tenant = (
                Tenant.objects.filter(name__iexact=spec["tenant_name"])
                .order_by("id")
                .first()
            )
        if not tenant:
            raise CommandError(
                f"Tenant not found (slug={spec.get('tenant_slug')!r} "
                f"name={spec.get('tenant_name')!r})."
            )

        # ---- Resolve template ----------------------------------------------
        tpl_qs = CustomRecapTemplate.objects.filter(tenant=tenant)
        if spec.get("template_name"):
            template = tpl_qs.filter(name__iexact=spec["template_name"]).first()
            if not template:
                raise CommandError(
                    f"Template {spec['template_name']!r} not found for tenant "
                    f"{tenant.name!r}. Have: "
                    f"{list(tpl_qs.values_list('name', flat=True))}"
                )
        else:
            templates = list(tpl_qs.order_by("id"))
            if len(templates) != 1:
                raise CommandError(
                    "template_name is required — tenant has "
                    f"{len(templates)} templates: "
                    f"{[t.name for t in templates]}"
                )
            template = templates[0]

        # ---- Resolve event type / status / timezone / state ----------------
        event_type = None
        if spec.get("event_type_name"):
            event_type = EventType.objects.filter(
                tenant=tenant, name__iexact=spec["event_type_name"]
            ).first()
        if not event_type:
            event_type = getattr(template, "event_type", None)
        if not event_type:
            raise CommandError(
                "No event type resolved (template has none and no "
                "event_type_name override)."
            )

        status_slug = (spec.get("event_status_slug") or "approved").strip()
        event_status = EventStatus.objects.filter(
            tenant=tenant, slug=status_slug
        ).first()
        if not event_status:
            raise CommandError(
                f"EventStatus slug={status_slug!r} not found for tenant "
                f"{tenant.name!r}."
            )

        tz_code = (spec.get("timezone_code") or "").strip()
        timezone_row = (
            TimeZone.objects.filter(code__iexact=tz_code).order_by("id").first()
            if tz_code
            else None
        )
        if tz_code and not timezone_row:
            raise CommandError(f"TimeZone code={tz_code!r} not found.")
        tz_offset = getattr(timezone_row, "offset", 0) or 0

        state_code = (spec.get("state_code") or "").strip()
        state = (
            State.objects.filter(code__iexact=state_code).order_by("id").first()
            if state_code
            else None
        )
        if state_code and not state:
            # Some deployments store the full name; try that before failing.
            state = State.objects.filter(name__iexact=state_code).first()
        if state_code and not state:
            raise CommandError(f"State code/name={state_code!r} not found.")

        # ---- Build the column → field map ----------------------------------
        fields = list(
            CustomField.objects.filter(custom_recap_template=template).select_related(
                "custom_field_type"
            )
        )
        field_by_norm = {_norm(f.name): f for f in fields}

        date_key = spec.get("date_key") or "Date"
        address_key = spec.get("address_key") or "Store/Location"
        reserved = {_norm(date_key), _norm(address_key)}

        all_columns = []
        for r in rows:
            for k in r.keys():
                if k not in all_columns:
                    all_columns.append(k)

        mapped, unmapped_cols = {}, []
        for col in all_columns:
            if _norm(col) in reserved:
                continue
            f = field_by_norm.get(_norm(col))
            if f:
                mapped[col] = f
            else:
                unmapped_cols.append(col)
        used_field_ids = {f.id for f in mapped.values()}
        unfilled_fields = [f.name for f in fields if f.id not in used_field_ids]

        report.update(
            {
                "tenant": {"id": tenant.id, "name": tenant.name, "slug": tenant.slug},
                "template": {"id": template.id, "name": template.name},
                "event_type": {"id": event_type.id, "name": event_type.name},
                "event_status": event_status.slug,
                "timezone": {"code": tz_code, "offset_min": tz_offset},
                "state": (state.code if state else None),
                "external_ba_name": spec.get("external_ba_name"),
                "fields_total": len(fields),
                "columns_mapped": {c: mapped[c].name for c in mapped},
                "columns_unmapped": unmapped_cols,
                "template_fields_unfilled": unfilled_fields,
            }
        )

        # ---- Report header --------------------------------------------------
        w("")
        w(f"import_demo_recaps  [{report['mode']}]")
        w(f"  tenant       : {tenant.id} {tenant.name} (slug={tenant.slug})")
        w(f"  template     : {template.id} {template.name}  ({len(fields)} fields)")
        w(f"  event type   : {event_type.id} {event_type.name}")
        w(f"  event status : {event_status.slug}")
        w(f"  timezone     : {tz_code} (offset {tz_offset} min)")
        w(f"  state        : {state.code if state else '(none)'}")
        w(f"  BA credit    : {spec.get('external_ba_name')!r}")
        w(f"  rows         : {len(rows)}")
        w("")
        w(f"  MAPPED columns → fields ({len(mapped)}):")
        for c in mapped:
            f = mapped[c]
            w(f"    • {c!r} → {f.name!r}  [{_type_token(f)}]")
        if unmapped_cols:
            w("")
            w(self.style.WARNING(f"  UNMAPPED columns ({len(unmapped_cols)}) — no matching field, will be SKIPPED:"))
            for c in unmapped_cols:
                w(f"    • {c!r}")
        if unfilled_fields:
            w("")
            w(f"  Template fields left blank ({len(unfilled_fields)}): {', '.join(unfilled_fields)}")
        w("")

        # ---- Per-row processing --------------------------------------------
        results = []
        engagements_field_norm = _norm(spec.get("engagements_field") or "")
        prefix = (spec.get("event_name_prefix") or "Demo").strip()
        local_hour = int(spec.get("local_hour") or 12)
        approved = bool(spec.get("approved", True))
        ba_name = (spec.get("external_ba_name") or "").strip() or None

        for i, row in enumerate(rows, start=1):
            date_raw = row.get(date_key)
            address = (row.get(address_key) or "").strip()
            row_res = {"index": i, "address": address, "date": str(date_raw)}
            try:
                event_dt = self._build_event_dt(date_raw, local_hour, tz_offset)
            except Exception as e:  # noqa: BLE001
                row_res["status"] = "error"
                row_res["error"] = f"bad date {date_raw!r}: {e}"
                results.append(row_res)
                w(self.style.ERROR(f"  row {i}: {row_res['error']}"))
                continue

            city = self._city_from_address(address)
            event_name = f"{prefix} — {city}" if city else prefix

            # Count the field values we'd write (for the preview).
            n_vals, notes = 0, []
            for col, f in mapped.items():
                if col not in row:
                    continue
                val, note = _format_value(f, row.get(col))
                if val is None or val == "":
                    continue
                n_vals += 1
                if note:
                    notes.append(f"{f.name}: {note}")

            row_res.update(
                {"event_name": event_name, "field_values": n_vals, "notes": notes}
            )

            existing = Event.objects.filter(
                tenant=tenant, name=event_name, date=event_dt
            ).first()
            if existing:
                row_res["status"] = "skipped_exists"
                row_res["event_id"] = existing.id
                results.append(row_res)
                w(f"  row {i}: SKIP (event exists id={existing.id}) — {event_name}")
                continue

            if not apply:
                row_res["status"] = "would_create"
                results.append(row_res)
                w(f"  row {i}: would create — {event_name}  ({n_vals} field values)")
                for n in notes:
                    w(self.style.WARNING(f"       note: {n}"))
                continue

            # ---- APPLY: create event + recap + values in one transaction ----
            with transaction.atomic():
                event = Event.objects.create(
                    name=event_name,
                    date=event_dt,
                    tenant=tenant,
                    event_type=event_type,
                    status=event_status,
                    address=address,
                    state=state,
                    timezone=timezone_row,
                    custom_recap_template=template,
                    created_by=owner,
                )

                engagements = None
                if engagements_field_norm:
                    for col, f in mapped.items():
                        if _norm(f.name) == engagements_field_norm and col in row:
                            try:
                                engagements = int(float(row[col]))
                            except (TypeError, ValueError):
                                engagements = None
                            break

                recap = CustomRecap.objects.create(
                    name=event_name,
                    submitted_at=event_dt,
                    total_engagements=engagements,
                    approved=approved,
                    event=event,
                    timezone=timezone_row,
                    ambassador=None,
                    external_ba_name=ba_name,
                    retailer=None,
                    location=None,
                    state=state,
                    tenant=tenant,
                    custom_recap_template=template,
                    created_by=owner,
                )

                written = 0
                for col, f in mapped.items():
                    if col not in row:
                        continue
                    val, _note = _format_value(f, row.get(col))
                    if val is None or val == "":
                        continue
                    CustomFieldValue.objects.create(
                        custom_recap=recap,
                        custom_field=f,
                        value=val,
                        created_by=owner,
                    )
                    written += 1

            row_res.update(
                {
                    "status": "created",
                    "event_id": event.id,
                    "recap_id": recap.id,
                    "field_values_written": written,
                    "total_engagements": engagements,
                }
            )
            results.append(row_res)
            w(
                self.style.SUCCESS(
                    f"  row {i}: CREATED event={event.id} recap={recap.id} "
                    f"({written} values) — {event_name}"
                )
            )

        report["results"] = results
        report["created"] = sum(1 for r in results if r.get("status") == "created")
        report["skipped"] = sum(
            1 for r in results if r.get("status") == "skipped_exists"
        )
        report["would_create"] = sum(
            1 for r in results if r.get("status") == "would_create"
        )
        report["errors"] = sum(1 for r in results if r.get("status") == "error")

        w("")
        w(
            f"  SUMMARY: created={report['created']} "
            f"would_create={report['would_create']} "
            f"skipped={report['skipped']} errors={report['errors']}"
        )
        if not apply:
            w("  (dry-run — nothing written. Re-run with --apply to commit.)")
        return report

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _build_event_dt(date_raw, local_hour: int, offset_min: int):
        """Parse ``YYYY-MM-DD`` (or an ISO datetime) and return an offset-aware
        datetime at ``local_hour`` in the venue's fixed offset, so Django stores
        the intended calendar day everywhere in the US."""
        if isinstance(date_raw, datetime.datetime):
            d = date_raw.date()
        elif isinstance(date_raw, datetime.date):
            d = date_raw
        else:
            s = str(date_raw).strip()
            d = datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).date() if "T" in s else datetime.date.fromisoformat(s)
        tzinfo = datetime.timezone(datetime.timedelta(minutes=offset_min))
        return datetime.datetime(d.year, d.month, d.day, local_hour, 0, 0, tzinfo=tzinfo)

    @staticmethod
    def _city_from_address(address: str) -> str:
        parts = [p.strip() for p in (address or "").split(",") if p.strip()]
        # ".., <City>, <ST ZIP>" → the City segment.
        if len(parts) >= 2:
            return parts[-2]
        return parts[0] if parts else ""
