"""Month-end BA expense receipts export.

Field teams buy product / supplies on shift (Girl Beer corpo-card runs
are the canonical case); the receipt lands in the recap as a
``CustomRecapFile`` in a "Receipt(s)"-named :class:`FileRecapCategory`,
and the spend amount as an "Account Spend Amount"-style custom field.
Month-end bookkeeping then meant opening recaps one by one to hunt
photos and retype amounts.

This module collects everything for a tenant + date range into:

  * row dicts (one per recap with receipt files OR a spend amount,
    BOTH recap families — legacy ``Recap.account_spend_amount`` rows
    count too) → the CSV the mutation hands back, and
  * a WeasyPrint PDF bundle — cover with totals, then one captioned
    section per recap with every receipt image embedded — uploaded to
    GCS by the caller (same pattern as the campaign report).

Date scoping uses the EVENT date (bookkeeping wants the period the
spend belongs to), falling back to the recap's ``created_at`` when the
event has no date.
"""

from __future__ import annotations

import base64
import re
from datetime import date

from django.db.models import Q

# Category + field-label matchers. Receipt categories in the wild:
# "Receipts", "Receipt", "Upload Receipt" (see backfill_girlbeer_receipts),
# and "Product Spend" — Torch THC's bucket, which holds itemized store
# receipts but contains no form of the word "receipt".
#
# That last one is why this is a pattern and not a fixed list. Torch had 16
# real receipts attached across Sept 4-6 and the export reported ZERO, because
# the tenant named their bucket sensibly and the matcher was too narrow. The
# failure is silent in the worst way: spend still totals correctly, so an
# invoice looks complete while every line is unsubstantiated.
#
# Kept deliberately tight. A bare "spend" would swallow any category with the
# word in it; "product spend" is unambiguous.
_RECEIPT_CATEGORY_RE = r"receipt|product\s*spend"
# Spend labels: GB "Account Spend Amount"; legacy column is separate.
_AMOUNT_FIELD_RE = re.compile(r"spend|amount", re.IGNORECASE)

_MONEY_RE = re.compile(r"-?\d[\d,]*\.?\d*")


def _parse_money(value) -> float | None:
    """"$1,234.50" / "43.2" / "about 60" → float, else None."""
    if value is None:
        return None
    m = _MONEY_RE.search(str(value))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _ba_label(user) -> tuple[str, str]:
    if user is None:
        return ("(external)", "")
    full = f"{(user.first_name or '').strip()} {(user.last_name or '').strip()}".strip()
    email = user.email or ""
    return (full or email or "(unnamed)", email)


def collect_expense_rows(
    tenant_id: int, start: date, end: date
) -> list[dict]:
    """One row per recap carrying expense evidence in [start, end].

    Custom recaps qualify with receipt-category files OR a parseable
    spend field; legacy recaps with ``account_spend_amount > 0``. Rows
    are plain dicts (no ORM objects) so the PDF render can run on a
    non-thread-sensitive worker without touching the connection.
    """
    from utils.gcs import extract_blob_name_from_url, public_url

    from recaps.models import CustomFieldValue, CustomRecap, Recap

    in_window = Q(
        event__date__date__gte=start, event__date__date__lte=end
    ) | Q(
        event__date__isnull=True,
        created_at__date__gte=start,
        created_at__date__lte=end,
    )

    rows: list[dict] = []

    customs = (
        CustomRecap.objects.filter(tenant_id=tenant_id)
        .filter(in_window)
        .select_related("event", "ambassador__user")
        .prefetch_related("custom_recap_files__file_recap_category")
        .order_by("event__date", "id")
    )

    # Spend values for all matched recaps in one query.
    amount_by_recap: dict[int, float] = {}
    value_rows = CustomFieldValue.objects.filter(
        custom_recap__tenant_id=tenant_id,
        custom_field__name__iregex=_AMOUNT_FIELD_RE.pattern,
    ).values_list("custom_recap_id", "custom_field__name", "value")
    for recap_id, _name, value in value_rows:
        parsed = _parse_money(value)
        if parsed is not None and recap_id not in amount_by_recap:
            amount_by_recap[recap_id] = parsed

    for recap in customs:
        files = []
        for f in recap.custom_recap_files.all():
            cat = f.file_recap_category
            if not cat or not re.search(
                _RECEIPT_CATEGORY_RE, cat.name or "", re.IGNORECASE
            ):
                continue
            blob = extract_blob_name_from_url(str(f.url or ""))
            if not blob:
                continue
            files.append(
                {
                    "name": f.name or blob.rsplit("/", 1)[-1],
                    "blob": blob,
                    "url": public_url(blob) or "",
                }
            )
        amount = amount_by_recap.get(recap.id)
        if not files and amount is None:
            continue
        ev = recap.event
        ba_name, ba_email = _ba_label(
            recap.ambassador.user if recap.ambassador else None
        )
        rows.append(
            {
                "kind": "custom",
                "recap_id": recap.id,
                "recap_uuid": str(recap.uuid),
                "event_uuid": str(ev.uuid) if ev else "",
                "ba_name": ba_name,
                "ba_email": ba_email,
                "event_name": getattr(ev, "name", None) or "(no event)",
                "event_date": (
                    ev.date.date().isoformat()
                    if getattr(ev, "date", None)
                    else recap.created_at.date().isoformat()
                ),
                "address": getattr(ev, "address", None) or "",
                "amount": amount,
                "corpo_card": bool(getattr(recap, "used_corpo_card", False)),
                "files": files,
            }
        )

    legacy = (
        Recap.objects.filter(
            event__tenant_id=tenant_id, account_spend_amount__gt=0
        )
        .filter(in_window)
        .select_related("event", "ambassador__user")
        .order_by("event__date", "id")
    )
    for recap in legacy:
        ev = recap.event
        ba_name, ba_email = _ba_label(
            recap.ambassador.user if recap.ambassador else None
        )
        rows.append(
            {
                "kind": "legacy",
                "recap_id": recap.id,
                "recap_uuid": str(recap.uuid),
                "event_uuid": str(ev.uuid) if ev else "",
                "ba_name": ba_name,
                "ba_email": ba_email,
                "event_name": getattr(ev, "name", None) or "(no event)",
                "event_date": (
                    ev.date.date().isoformat()
                    if getattr(ev, "date", None)
                    else recap.created_at.date().isoformat()
                ),
                "address": getattr(ev, "address", None) or "",
                "amount": float(recap.account_spend_amount),
                # Legacy form has no corpo-card flag or receipt category.
                "corpo_card": False,
                "files": [],
            }
        )

    rows.sort(key=lambda r: (r["event_date"], r["ba_name"]))
    return rows


def apply_amount_overrides(
    rows: list[dict],
    overrides: dict,
    *,
    note: str = "",
) -> tuple[int, list[str]]:
    """Reconcile recap spend to what the receipts actually total.

    BAs type the spend amount by hand, so the recorded number drifts from the
    paper: a transposed cent, a receipt left out of the sum. On a packet that
    goes to a client for BILLING that matters — the invoice total should be
    what the receipts prove, not what someone typed on a phone.

    This does NOT touch the recap. The custom-field value in Spark stays as the
    BA entered it (changing it would move dashboard KPIs and client reporting
    for a bookkeeping correction), and the adjustment lives only in the
    rendered document.

    Because this makes a financial document disagree with the system of record,
    the override is never silent: the row keeps ``amount_recorded``, and both
    the PDF and the CSV print the original beside the corrected figure so the
    reader can see exactly what was changed and by how much. An override that
    matches the recorded amount is dropped rather than annotated as a no-op.

    ``overrides`` maps recap id -> corrected amount (keys may be str or int,
    since they usually arrive as JSON). Returns ``(n_applied, problems)``;
    problems name ids that matched no row, so a typo'd id fails loudly instead
    of quietly leaving the total wrong.
    """
    by_id: dict[int, dict] = {
        r["recap_id"]: r for r in rows if r.get("recap_id") is not None
    }
    applied = 0
    problems: list[str] = []

    for raw_key, raw_val in (overrides or {}).items():
        try:
            recap_id = int(str(raw_key).strip())
        except (TypeError, ValueError):
            problems.append(f"{raw_key!r} is not a recap id")
            continue
        try:
            amount = round(float(raw_val), 2)
        except (TypeError, ValueError):
            problems.append(f"recap {recap_id}: {raw_val!r} is not an amount")
            continue

        row = by_id.get(recap_id)
        if row is None:
            problems.append(
                f"recap {recap_id} is not in this window — override ignored"
            )
            continue

        recorded = row.get("amount")
        if recorded is not None and abs(float(recorded) - amount) < 0.005:
            continue  # already agrees; nothing to annotate

        row["amount_recorded"] = recorded
        row["amount"] = amount
        row["amount_adjusted"] = True
        if note:
            row["adjust_note"] = note
        applied += 1

    return applied, problems


def _recorded_total(rows: list[dict]) -> float:
    """Sum of the spend as FIELD STAFF ENTERED IT, ignoring adjustments.

    An adjusted row reports ``amount_recorded``; a row that carried no spend
    figure at all contributes nothing, so overriding a blank line can't
    invent an "as entered" number that was never there.
    """
    total = 0.0
    for r in rows:
        if r.get("amount_adjusted"):
            recorded = r.get("amount_recorded")
            total += float(recorded) if recorded is not None else 0.0
        elif r.get("amount") is not None:
            total += float(r["amount"])
    return total


def build_expense_rows_csv(rows: list[dict]) -> str:
    """Bookkeeping CSV — one line per recap, file links joined."""
    import csv
    import io

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(
        [
            "Event date",
            "Ambassador",
            "Email",
            "Event",
            "Address",
            "Amount",
            "As entered",
            "Adjusted",
            "Corpo card",
            "Receipt count",
            "Receipt links",
            "Recap",
        ]
    )
    for r in rows:
        w.writerow(
            [
                r["event_date"],
                r["ba_name"],
                r["ba_email"],
                r["event_name"],
                r["address"],
                f"{r['amount']:.2f}" if r["amount"] is not None else "",
                (
                    f"{r['amount_recorded']:.2f}"
                    if r.get("amount_adjusted")
                    and r.get("amount_recorded") is not None
                    else ""
                ),
                "yes" if r.get("amount_adjusted") else "",
                "yes" if r["corpo_card"] else "",
                len(r["files"]),
                " ".join(f["url"] for f in r["files"]),
                r["recap_uuid"],
            ]
        )
    total = sum(r["amount"] for r in rows if r["amount"] is not None)
    w.writerow([])
    recorded_total = _recorded_total(rows)
    w.writerow(
        [
            "TOTAL",
            "", "", "", "",
            f"{total:.2f}",
            (
                f"{recorded_total:.2f}"
                if abs(recorded_total - total) >= 0.005
                else ""
            ),
            "", "", "", "", "",
        ]
    )
    return out.getvalue()


def _img_data_uri(name: str, data: bytes) -> str:
    ext = (name.rsplit(".", 1)[-1] if "." in name else "").lower()
    mime = {
        "png": "image/png",
        "webp": "image/webp",
        "gif": "image/gif",
    }.get(ext, "image/jpeg")
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def build_receipts_bundle_pdf(
    *,
    tenant_name: str,
    start: date,
    end: date,
    rows: list[dict],
    images_by_blob: dict[str, bytes],
) -> bytes:
    """Cover (totals) + one captioned section per recap with its receipt
    images. Pure-dict input — safe to render on a worker thread with no
    DB connection (mirrors build_campaign_report_pdf's posture)."""
    from weasyprint import CSS, HTML

    def safe(s) -> str:
        return (
            str(s or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    total = sum(r["amount"] for r in rows if r["amount"] is not None)
    receipt_count = sum(len(r["files"]) for r in rows)

    # Reconciliation banner: only rendered when something was actually
    # adjusted, so an ordinary export looks exactly as it always has.
    adjusted = [r for r in rows if r.get("amount_adjusted")]
    recorded_total = _recorded_total(rows)
    adjust_note = next(
        (r["adjust_note"] for r in adjusted if r.get("adjust_note")), ""
    )

    sections: list[str] = []
    for r in rows:
        amount = (
            f"${r['amount']:,.2f}" if r["amount"] is not None else "—"
        )
        was = ""
        if r.get("amount_adjusted") and r.get("amount_recorded") is not None:
            was = (
                ' · <span class="was">adjusted from '
                f"${r['amount_recorded']:,.2f}</span>"
            )
        corpo = (
            ' · <span class="corpo">CORPO CARD</span>'
            if r["corpo_card"]
            else ""
        )
        imgs = "".join(
            f'<img src="{_img_data_uri(f["name"], images_by_blob[f["blob"]])}" />'
            for f in r["files"]
            if f["blob"] in images_by_blob
        ) or '<p class="noimg">No receipt image attached.</p>'
        sections.append(
            f"""
      <section class="receipt">
        <h2>{safe(r['ba_name'])} — {amount}{was}{corpo}</h2>
        <p class="meta">{safe(r['event_name'])} · {safe(r['event_date'])}
          {('· ' + safe(r['address'])) if r['address'] else ''}</p>
        {imgs}
      </section>"""
        )

    recon_html = ""
    if adjusted:
        n = len(adjusted)
        recon_html = (
            '<p class="recon">Reconciled to receipt totals — '
            f"${recorded_total:,.2f} as entered by field staff, "
            f"{n} line{'s' if n != 1 else ''} adjusted to match the "
            "attached receipts."
            + (f" {safe(adjust_note)}" if adjust_note else "")
            + "</p>"
        )

    html_doc = f"""
<!doctype html>
<html><head><meta charset="utf-8"><title>Expense receipts</title></head>
<body>
  <section class="cover">
    <p class="eyebrow">EXPENSE RECEIPTS</p>
    <h1>{safe(tenant_name)}</h1>
    <p class="range">{start.isoformat()} → {end.isoformat()}</p>
    <div class="stats">
      <div><span>Recaps</span><strong>{len(rows)}</strong></div>
      <div><span>Receipts</span><strong>{receipt_count}</strong></div>
      <div><span>Total spend</span><strong>${total:,.2f}</strong></div>
    </div>
    {recon_html}
  </section>
  {''.join(sections)}
</body></html>
"""
    css = CSS(
        string="""
      @page { size: Letter; margin: 14mm; }
      body { font-family: Helvetica, Arial, sans-serif; color: #111; }
      .cover { text-align: center; padding-top: 60mm; }
      .eyebrow { letter-spacing: 0.3em; font-size: 10px; color: #7a8a2a; }
      .cover h1 { font-size: 30px; margin: 6px 0; }
      .range { color: #666; font-size: 12px; }
      .stats { display: flex; justify-content: center; gap: 28px;
               margin-top: 18px; }
      .stats span { display: block; font-size: 9px; color: #888;
                    letter-spacing: 0.14em; }
      .stats strong { font-size: 18px; }
      .receipt { page-break-before: always; }
      .receipt h2 { font-size: 15px; margin-bottom: 2px; }
      .receipt .meta { color: #666; font-size: 11px; margin: 0 0 8px; }
      .receipt img { max-width: 100%; max-height: 200mm; display: block;
                     margin: 0 0 8px; border: 1px solid #ddd; }
      .corpo { color: #7a8a2a; font-size: 10px; letter-spacing: 0.1em; }
      .noimg { color: #999; font-size: 11px; }
      .recon { margin: 14px auto 0; max-width: 118mm; font-size: 10px;
               line-height: 1.5; color: #555; }
      .was { color: #8a6d1a; font-size: 10px; letter-spacing: 0.02em; }
    """
    )
    return HTML(string=html_doc).write_pdf(stylesheets=[css])
