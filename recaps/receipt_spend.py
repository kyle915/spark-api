"""Spend from receipt readings, ready for billing. STAFF ONLY.

Turns ``ReceiptReading`` rows into receipts and dollars:

* One receipt counts once. Readings that share a duplicate key (same
  store + ticket + date + total, same store + ticket + date, or same ticket +
  date + total) are one receipt, whether it was photographed twice, in two
  halves, or attached to two recaps. It counts on the recap where it was
  uploaded first; the others list it as "also on".
* Each receipt is split into the brand's products (lines naming the brand,
  or matched to its catalog by the reader), other items (ice, cups, shot
  glasses…), fees and tax. What the lines do not cover is "unitemized".
* A photo with no total on it (the top half of a long receipt) adds nothing
  to the totals; it is listed so nobody wonders where it went.

Date ranges use the EVENT date (the period the spend belongs to), falling
back to the filing date when the event has none — same as the expense
receipts export.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.conf import settings

_ZERO = Decimal("0.00")
_CENT = Decimal("0.01")

CSV_COLUMNS = [
    "Event date",
    "Receipt date",
    "Time",
    "Store",
    "Store #",
    "City",
    "Ticket",
    "Brand product $",
    "Other items $",
    "Unitemized $",
    "Fees $",
    "Tax $",
    "Total $",
    "Recap",
    "Event",
    "Also on recaps",
    "Open flags",
    "Note",
]


def _d(value) -> Decimal:
    if value is None or value == "":
        return _ZERO
    try:
        return Decimal(str(value)).quantize(_CENT)
    except Exception:  # noqa: BLE001
        return _ZERO


def _recap_url(uuid: str) -> str:
    base = (getattr(settings, "ADMIN_FRONTEND_URL", "") or "").rstrip("/")
    path = f"/recap/view-custom/{uuid}"
    return f"{base}{path}" if base else path


class _Groups:
    """Union-find over readings by their duplicate keys."""

    def __init__(self):
        self.parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def group_readings(readings: list) -> list[list]:
    """Readings → receipts (lists of readings that are the same ticket).

    Within one recap, a photo with a total but no ticket and a photo with a
    ticket but no total are taken as two halves of one receipt when the
    recap has exactly one of each."""
    groups = _Groups()
    by_key: dict[tuple[str, str], int] = {}
    for r in readings:
        groups.find(r.id)
        for field in ("fingerprint", "ticket_key", "loose_key"):
            value = getattr(r, field, "") or ""
            if not value:
                continue
            seen = by_key.get((field, value))
            if seen is None:
                by_key[(field, value)] = r.id
            else:
                groups.union(seen, r.id)
    members: dict[int, list] = defaultdict(list)
    for r in readings:
        members[groups.find(r.id)].append(r)

    # Two halves of one long receipt on the same recap.
    per_recap: dict[int, dict[str, list[int]]] = defaultdict(lambda: {"head": [], "tail": []})
    for root, rows in members.items():
        recap_ids = {r.custom_recap_file.custom_recap_id for r in rows}
        if len(recap_ids) != 1:
            continue
        rid = next(iter(recap_ids))
        has_total = any(r.total is not None for r in rows)
        has_ticket = any(r.ticket_number for r in rows)
        if has_ticket and not has_total:
            per_recap[rid]["head"].append(root)
        elif has_total and not has_ticket:
            per_recap[rid]["tail"].append(root)
    for parts in per_recap.values():
        if len(parts["head"]) == 1 and len(parts["tail"]) == 1:
            head, tail = parts["head"][0], parts["tail"][0]
            members[head].extend(members.pop(tail))

    out = []
    for rows in members.values():
        rows.sort(key=lambda r: (r.custom_recap_file.created_at, r.custom_recap_file_id))
        out.append(rows)
    out.sort(key=lambda rows: (rows[0].custom_recap_file.created_at, rows[0].id))
    return out


def summarize_receipt(rows: list) -> dict:
    """One receipt (a group of readings) → its money split and labels."""
    first = rows[0]
    with_total = next((r for r in rows if r.total is not None), None)
    lines_from = max(rows, key=lambda r: len(r.line_items or []))
    pick = lambda attr: next((getattr(r, attr) for r in rows if getattr(r, attr)), "")  # noqa: E731
    # Two halves of one long receipt (top: ticket and items, no total;
    # bottom: the rest and the total) each hold different lines: use both.
    # Two photos of the whole receipt hold the same lines: use one.
    halves = len(rows) == 2 and {
        (bool(r.ticket_number), r.total is not None) for r in rows
    } == {(True, False), (False, True)}
    line_items = (
        [i for r in rows for i in (r.line_items or [])] if halves else (lines_from.line_items or [])
    )
    product = other = _ZERO
    for item in line_items:
        amount = _d(item.get("amount"))
        if item.get("isBrand"):
            product += amount
        else:
            other += amount
    total = _d(with_total.total) if with_total else None
    fees = _d((with_total or lines_from).fees)
    tax = _d((with_total or lines_from).tax)
    unitemized = _ZERO
    note = ""
    if total is None:
        note = "No total on this photo (part of a receipt)"
        product = other = fees = tax = _ZERO
    else:
        rest = (abs(total) - product - other - fees - tax).quantize(_CENT)
        if rest > _CENT:
            unitemized = rest
        if total < 0:
            note = "Refund"
    recap_ids = []
    for r in rows:
        rid = r.custom_recap_file.custom_recap_id
        if rid not in recap_ids:
            recap_ids.append(rid)
    return {
        "readingIds": [r.id for r in rows],
        "fileIds": [r.custom_recap_file_id for r in rows],
        "ownerRecapId": recap_ids[0],
        "alsoOnRecapIds": recap_ids[1:],
        "receiptDate": pick("receipt_date"),
        "purchaseTime": pick("purchase_time"),
        "merchant": pick("merchant"),
        "storeNumber": pick("store_number"),
        "city": pick("city"),
        "ticketNumber": pick("ticket_number"),
        "product": product,
        "other": other,
        "unitemized": unitemized,
        "fees": fees,
        "tax": tax,
        "total": total,
        "counted": total is not None,
        "photos": len(rows),
        "note": note,
        "status": first.status,
    }


def _readings_qs():
    from recaps.models import ReceiptReading

    return ReceiptReading.objects.filter(
        status__in=("read", "unreadable"), custom_recap_file__custom_recap__isnull=False
    ).select_related(
        "custom_recap_file",
        "custom_recap_file__custom_recap",
        "custom_recap_file__custom_recap__event",
    )


def _open_flags_by_recap(recap_ids) -> dict[int, list[str]]:
    from recaps.models import RecapFlag

    out: dict[int, list[str]] = defaultdict(list)
    rows = RecapFlag.objects.filter(
        custom_recap_id__in=list(recap_ids), resolved_at__isnull=True
    ).values_list("custom_recap_id", "kind")
    for rid, kind in rows:
        if kind not in out[rid]:
            out[rid].append(kind)
    return out


def recap_spend(recap_id: int) -> dict:
    """Spend for one recap, with its receipts. A receipt also attached to an
    earlier recap is listed but counted there, not here."""
    from django.db.models import Q

    own = list(_readings_qs().filter(custom_recap_file__custom_recap_id=recap_id))
    keys = Q()
    for r in own:
        for field in ("fingerprint", "ticket_key", "loose_key"):
            value = getattr(r, field, "")
            if value:
                keys |= Q(**{field: value})
    twins = list(_readings_qs().filter(keys).exclude(custom_recap_file__custom_recap_id=recap_id)) if keys else []
    receipts = [summarize_receipt(g) for g in group_readings(own + twins)]
    receipts = [r for r in receipts if recap_id in [r["ownerRecapId"], *r["alsoOnRecapIds"]]]
    totals = _totals(r for r in receipts if r["ownerRecapId"] == recap_id)
    return {"totals": totals, "receipts": receipts}


def _totals(receipts) -> dict:
    out = {"product": _ZERO, "other": _ZERO, "unitemized": _ZERO, "fees": _ZERO,
           "tax": _ZERO, "total": _ZERO, "receipts": 0}
    for r in receipts:
        if not r["counted"]:
            continue
        for k in ("product", "other", "unitemized", "fees", "tax"):
            out[k] += r[k]
        out["total"] += r["total"]
        out["receipts"] += 1
    return out


def program_spend(
    *, tenant_id: int | None, date_from: date | None, date_to: date | None
) -> dict:
    """Spend for a tenant (or every tenant) over an event-date range: the
    totals, one row per receipt, and one row per recap."""
    from recaps.receipt_reader import _event_window_q

    qs = _readings_qs().filter(_event_window_q("custom_recap_file__custom_recap__", date_from, date_to))
    if tenant_id:
        qs = qs.filter(custom_recap_file__custom_recap__tenant_id=tenant_id)
    readings = list(qs[:20000])
    recaps = {}
    for r in readings:
        recap = r.custom_recap_file.custom_recap
        recaps[recap.id] = recap
    receipts = [summarize_receipt(g) for g in group_readings(readings)]
    flags = _open_flags_by_recap(recaps.keys())
    rows = []
    for rec in receipts:
        recap = recaps.get(rec["ownerRecapId"])
        event = getattr(recap, "event", None)
        event_day = getattr(event, "date", None)
        rows.append(
            {
                **rec,
                "recapUuid": str(recap.uuid) if recap else "",
                "tenantId": recap.tenant_id if recap else None,
                "eventName": (getattr(event, "name", "") or "")[:120],
                "eventDate": event_day.date().isoformat() if event_day else "",
                "alsoOnUuids": [str(recaps[i].uuid) for i in rec["alsoOnRecapIds"] if i in recaps],
                "openFlags": sorted(
                    {k for i in [rec["ownerRecapId"], *rec["alsoOnRecapIds"]] for k in flags.get(i, [])}
                ),
            }
        )
    rows.sort(key=lambda r: (r["eventDate"], r["receiptDate"], r["purchaseTime"]))
    per_recap: dict[int, dict] = {}
    for row in rows:
        slot = per_recap.setdefault(
            row["ownerRecapId"],
            {"recapId": row["ownerRecapId"], "recapUuid": row["recapUuid"],
             "eventName": row["eventName"], "eventDate": row["eventDate"],
             "openFlags": row["openFlags"], "receipts": []},
        )
        slot["receipts"].append(row)
    recap_rows = []
    for slot in per_recap.values():
        recap_rows.append({**{k: v for k, v in slot.items() if k != "receipts"}, **_totals(slot["receipts"])})
    recap_rows.sort(key=lambda r: (r["eventDate"], r["recapId"]))
    return {"totals": _totals(rows), "receipts": rows, "recaps": recap_rows}


def _money(value) -> str:
    return "" if value is None else f"{value:.2f}"


def spend_csv(rows: list[dict]) -> str:
    """One CSV row per receipt (see :data:`CSV_COLUMNS`)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_COLUMNS)
    for r in rows:
        writer.writerow(
            [
                r.get("eventDate", ""),
                r.get("receiptDate", ""),
                r.get("purchaseTime", ""),
                r.get("merchant", ""),
                r.get("storeNumber", ""),
                r.get("city", ""),
                r.get("ticketNumber", ""),
                _money(r.get("product")),
                _money(r.get("other")),
                _money(r.get("unitemized")),
                _money(r.get("fees")),
                _money(r.get("tax")),
                _money(r.get("total")),
                _recap_url(r.get("recapUuid", "")),
                r.get("eventName", ""),
                " ".join(_recap_url(u) for u in r.get("alsoOnUuids", [])),
                ", ".join(r.get("openFlags", [])),
                r.get("note", ""),
            ]
        )
    return buf.getvalue()
