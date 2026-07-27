"""Dump every product-spend RECEIPT (image/file) for one tenant — read-only.

The sibling of :mod:`audit_tenant_account_spend`: that command answers "how much
product spend did we log?", this one answers "show me the receipts backing it".
For each recap it emits the spend amount PLUS every attached file with a signed
GCS download URL, so the images can be pulled down and handed to a client.

Deliberately dumps EVERY file, not just ones filed under a receipt-looking
category, and reports each file's category verbatim with an ``is_receipt`` hint.
Recap files have a history of landing in the wrong category (a receipt uploaded
to "Upload Receipt" once filed itself under "Table setup"), so filtering by
category server-side would silently hide receipts. Same break-down-before-you-
trust-the-total discipline as the consumers/spend audits: show the raw
categories, let the caller judge.

HEIC handling: receipts shot on an iPhone are stored as .heic, which browsers
and most viewers won't render. Each blob is resolved through
``recaps.heic_conversion.display_blob_name`` so the URL points at the converted
JPG when one exists (falling back to the original blob otherwise).

READ-ONLY. No writes, no email. Run via the
``/internal/cron/dump-tenant-receipts`` endpoint (or the "Dump tenant receipts"
GitHub Action) so it executes against prod.
"""

from __future__ import annotations

import json
import re

from django.core.management.base import BaseCommand

from recaps.heic_conversion import display_blob_name
from recaps.management.commands.audit_tenant_consumers import _resolve_tenant
from recaps.models import CustomFieldValue, CustomRecap, Recap
from recaps.types import _account_spend_from_fields
from utils.gcs import extract_blob_name_from_url, generate_download_url

# A file is *probably* a receipt if its category or filename says so. This is a
# HINT surfaced in the output, never a filter — see the module docstring.
_RECEIPT_RE = re.compile(r"receipt|invoice|purchase|expense", re.IGNORECASE)


def _ba_name(recap) -> str | None:
    amb = getattr(recap, "ambassador", None)
    if amb is not None:
        name = " ".join(
            filter(
                None,
                [
                    (getattr(amb, "first_name", "") or "").strip(),
                    (getattr(amb, "last_name", "") or "").strip(),
                ],
            )
        ).strip()
        if name:
            return name
        user = getattr(amb, "user", None)
        if user is not None:
            return (getattr(user, "email", None) or "").strip() or None
    return (getattr(recap, "external_ba_name", None) or "").strip() or None


def _event_date(recap):
    ev = getattr(recap, "event", None)
    if ev is None:
        return None
    for attr in ("date", "start_time"):
        val = getattr(ev, attr, None)
        if val:
            return val.date().isoformat() if hasattr(val, "date") else str(val)
    return None


class Command(BaseCommand):
    help = "Read-only: dump each recap's product spend + signed receipt-file URLs."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", required=True, help="id, request-url-name, or name")
        parser.add_argument(
            "--expire-minutes",
            type=int,
            default=720,
            help="signed-URL lifetime (default 720 = 12h)",
        )
        parser.add_argument(
            "--spend-only",
            action="store_true",
            help="only include recaps that carry a product-spend amount",
        )

    def handle(self, *args, **opts):
        tenant = _resolve_tenant(opts["tenant"])
        expire = int(opts["expire_minutes"])
        w = self.stdout.write

        # --- spend per custom recap (same matcher as audit_tenant_account_spend)
        pairs_by_recap: dict = {}
        for rid, fname, val in CustomFieldValue.objects.filter(
            custom_recap__tenant_id=tenant.id
        ).values_list("custom_recap_id", "custom_field__name", "value").iterator():
            pairs_by_recap.setdefault(rid, []).append((fname, val))

        recaps = (
            CustomRecap.objects.filter(tenant_id=tenant.id)
            .select_related("event", "ambassador")
            .prefetch_related("custom_recap_files__file_recap_category")
            .order_by("id")
        )

        rows = []
        total_spend = 0.0
        n_files = 0
        n_receipt_files = 0
        missing_receipt = []
        for r in recaps:
            spend = _account_spend_from_fields(pairs_by_recap.get(r.id, []))
            if opts["spend_only"] and spend is None:
                continue
            files = []
            for f in r.custom_recap_files.all():
                raw = getattr(f.url, "name", None) or None
                blob = extract_blob_name_from_url(raw)
                if not blob:
                    continue
                serve_blob = display_blob_name(blob) or blob
                cat = getattr(f.file_recap_category, "name", None)
                label = f"{cat or ''} {f.name or ''} {blob}"
                try:
                    signed = generate_download_url(serve_blob, expiration_minutes=expire)
                except Exception as exc:  # never abort the dump on one bad blob
                    signed = None
                    self.stderr.write(f"  ! sign failed for {serve_blob}: {exc}")
                is_receipt = bool(_RECEIPT_RE.search(label))
                n_files += 1
                n_receipt_files += 1 if is_receipt else 0
                files.append(
                    {
                        "file_id": f.id,
                        "name": f.name,
                        "category": cat,
                        "is_receipt": is_receipt,
                        "blob": blob,
                        "serve_blob": serve_blob,
                        "heic_converted": serve_blob != blob,
                        "url": signed,
                    }
                )
            if spend is not None and not any(x["is_receipt"] for x in files):
                missing_receipt.append(r.id)
            total_spend += spend or 0.0
            rows.append(
                {
                    "recap_id": r.id,
                    "name": r.name,
                    "event_date": _event_date(r),
                    "ba": _ba_name(r),
                    "approved": bool(r.approved),
                    "spend": spend,
                    "files": files,
                }
            )

        # --- legacy recaps (typed column + RecapFile); usually empty for custom tenants
        legacy = []
        for lr in (
            Recap.objects.filter(event__tenant_id=tenant.id)
            .select_related("event")
            .prefetch_related("recap_files__file_recap_category")
            .order_by("id")
        ):
            lfiles = []
            for f in lr.recap_files.all():
                raw = getattr(f.file, "name", None) or None
                blob = extract_blob_name_from_url(raw)
                if not blob:
                    continue
                serve_blob = display_blob_name(blob) or blob
                cat = getattr(f.file_recap_category, "name", None)
                try:
                    signed = generate_download_url(serve_blob, expiration_minutes=expire)
                except Exception:
                    signed = None
                lfiles.append(
                    {
                        "file_id": f.id,
                        "name": f.name,
                        "category": cat,
                        "is_receipt": bool(
                            _RECEIPT_RE.search(f"{cat or ''} {f.name or ''} {blob}")
                        ),
                        "blob": blob,
                        "serve_blob": serve_blob,
                        "url": signed,
                    }
                )
            if lr.account_spend_amount or lfiles:
                legacy.append(
                    {
                        "recap_id": lr.id,
                        "name": lr.name,
                        "spend": float(lr.account_spend_amount or 0) or None,
                        "files": lfiles,
                    }
                )

        report = {
            "tenant": {"id": tenant.id, "name": tenant.name},
            "expire_minutes": expire,
            "custom_recaps": len(rows),
            "recaps_with_spend": sum(1 for r in rows if r["spend"] is not None),
            "total_spend": round(total_spend, 2),
            "files_total": n_files,
            "files_receiptish": n_receipt_files,
            "recaps_with_spend_but_no_receipt_file": missing_receipt,
            "rows": rows,
            "legacy": legacy,
        }

        w("")
        w(f"Receipts dump — {tenant.name} (id {tenant.id})")
        w("=" * 72)
        for r in rows:
            amt = f"${r['spend']:,.2f}" if r["spend"] is not None else "—"
            rc = sum(1 for f in r["files"] if f["is_receipt"])
            w(
                f"  recap {r['recap_id']:>4} · {(r['name'] or '')[:34]:34} "
                f"{amt:>10}  files={len(r['files'])} receipt-ish={rc}"
            )
        w("")
        w(f"TOTAL spend ${total_spend:,.2f} across "
          f"{report['recaps_with_spend']} recaps · {n_files} files "
          f"({n_receipt_files} receipt-ish)")
        if missing_receipt:
            w(f"SPEND WITH NO RECEIPT-ISH FILE: recaps {missing_receipt}")
        w("")
        w("JSON_RESULT: " + json.dumps(report, default=str))
