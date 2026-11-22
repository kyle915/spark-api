"""Outbound email envelopes for Standalone Spark pilot features."""

from __future__ import annotations

from datetime import date
from html import escape
from typing import Iterable

from utils.mailer import Envelope, Mailer


class BuyerWeeklyDigestMailer(Mailer):
    """Monday weekly buyer store-report digest with CSV attachment."""

    def __init__(
        self,
        *,
        recipients: Iterable[str],
        tenant_name: str,
        period_start: date,
        period_end: date,
        row_count: int,
        total_visits: int,
        total_supported_cost: str,
        pos_available: bool,
        csv_text: str,
        csv_filename: str,
    ) -> None:
        seen: set[str] = set()
        clean: list[str] = []
        for raw in recipients:
            email = (raw or "").strip()
            key = email.lower()
            if email and key not in seen:
                seen.add(key)
                clean.append(email)
        self.recipients = clean
        self.tenant_name = tenant_name or "Brand"
        self.period_start = period_start
        self.period_end = period_end
        self.row_count = int(row_count or 0)
        self.total_visits = int(total_visits or 0)
        self.total_supported_cost = total_supported_cost or "0.00"
        self.pos_available = bool(pos_available)
        self.csv_bytes = (csv_text or "").encode("utf-8")
        self.csv_filename = csv_filename or "buyer-store-report.csv"

    def envelope(self) -> Envelope:
        brand = escape(self.tenant_name)
        pos_line = (
            "Retailer POS coverage is available for this window."
            if self.pos_available
            else "Retailer POS coverage is unavailable for this window — "
            "lift/ROI cells stay withheld."
        )
        body = f"""
        <div style="font-family:Helvetica,Arial,sans-serif;color:#111;line-height:1.45">
          <h2 style="margin:0 0 8px">{brand} · Weekly buyer store report</h2>
          <p style="margin:0 0 12px;color:#444">
            {escape(self.period_start.isoformat())} → {escape(self.period_end.isoformat())}
          </p>
          <table cellpadding="8" cellspacing="0" style="border-collapse:collapse;border:1px solid #ddd">
            <tr>
              <td style="border:1px solid #ddd"><b>Stores in report</b></td>
              <td style="border:1px solid #ddd" align="right">{self.row_count}</td>
            </tr>
            <tr>
              <td style="border:1px solid #ddd"><b>Visits</b></td>
              <td style="border:1px solid #ddd" align="right">{self.total_visits}</td>
            </tr>
            <tr>
              <td style="border:1px solid #ddd"><b>Supported brand cost</b></td>
              <td style="border:1px solid #ddd" align="right">${escape(str(self.total_supported_cost))}</td>
            </tr>
          </table>
          <p style="margin:16px 0 8px;color:#444">{escape(pos_line)}</p>
          <p style="margin:0;color:#666;font-size:13px">
            Full store table attached as CSV for your buyer / budget meeting.
          </p>
        </div>
        """
        return Envelope(
            subject=(
                f"{self.tenant_name}: weekly buyer store report "
                f"({self.period_start.isoformat()} – {self.period_end.isoformat()})"
            ),
            html=body,
            to_emails=self.recipients,
            attachments=[
                {
                    "filename": self.csv_filename,
                    "content": list(self.csv_bytes),
                    "content_type": "text/csv",
                }
            ],
        )
