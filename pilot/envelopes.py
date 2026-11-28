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


class SavedViewDigestMailer(Mailer):
    """Scheduled digest email for a SavedReportView filtered slice."""

    def __init__(
        self,
        *,
        recipients: Iterable[str],
        tenant_name: str,
        view_name: str,
        filter_summary: str,
        period_label: str,
        metrics: list[tuple[str, str]],
        csv_text: str | None = None,
        csv_filename: str = "spark-saved-view.csv",
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
        self.view_name = view_name or "Saved Report View"
        self.filter_summary = filter_summary or "All stores"
        self.period_label = period_label or "All time"
        self.metrics = metrics
        self.csv_bytes = csv_text.encode("utf-8") if csv_text is not None else None
        self.csv_filename = csv_filename or "spark-saved-view.csv"

    def envelope(self) -> Envelope:
        brand = escape(self.tenant_name)
        title = escape(self.view_name)
        rows_html = "".join(
            f"""
            <tr>
              <td style="border:1px solid #dcddd5;padding:8px 12px;color:#464a3e"><b>{escape(k)}</b></td>
              <td style="border:1px solid #dcddd5;padding:8px 12px;color:#14160f" align="right">{escape(v)}</td>
            </tr>
            """
            for k, v in self.metrics
        )
        attach_note = (
            "Filtered CSV export is attached for your distributor, broker, or buyer meeting."
            if self.csv_bytes is not None
            else "Summary-only digest configured for this saved view."
        )
        body = f"""
        <div style="font-family:Helvetica,Arial,sans-serif;color:#14160f;line-height:1.5;max-width:600px">
          <p style="margin:0 0 4px;font-size:11px;letter-spacing:0.1em;text-transform:uppercase;color:#6b705c">
            {brand} · Scheduled Saved View Digest
          </p>
          <h2 style="margin:0 0 8px;font-size:20px">{title}</h2>
          <p style="margin:0 0 14px;color:#464a3e;font-size:13px">
            <b>Window:</b> {escape(self.period_label)} &nbsp;·&nbsp;
            <b>Filters:</b> {escape(self.filter_summary)}
          </p>
          <table cellpadding="0" cellspacing="0" style="border-collapse:collapse;border:1px solid #dcddd5;width:100%">
            {rows_html}
          </table>
          <p style="margin:16px 0 0;color:#6b705c;font-size:12px">{escape(attach_note)}</p>
        </div>
        """
        attachments = (
            [
                {
                    "filename": self.csv_filename,
                    "content": list(self.csv_bytes),
                    "content_type": "text/csv",
                }
            ]
            if self.csv_bytes is not None
            else []
        )
        return Envelope(
            subject=f"{self.tenant_name}: {self.view_name} ({self.period_label})",
            html=body,
            to_emails=self.recipients,
            attachments=attachments,
        )


class ExecutiveSnapshotMailer(Mailer):
    """Automated 1st-of-the-month Executive Snapshot email + optional PDF brief."""

    def __init__(
        self,
        *,
        recipients: Iterable[str],
        tenant_name: str,
        month_label: str,
        prior_month_label: str,
        kpis: list[dict],
        top_doors: list[dict],
        shelf_summary: dict,
        themes: list[dict],
        photos: list[dict],
        pdf_bytes: bytes | None = None,
        pdf_filename: str = "executive-snapshot.pdf",
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
        self.month_label = month_label or "Monthly Brief"
        self.prior_month_label = prior_month_label or "Prior month"
        self.kpis = list(kpis or [])
        self.top_doors = list(top_doors or [])
        self.shelf_summary = dict(shelf_summary or {})
        self.themes = list(themes or [])
        self.photos = list(photos or [])
        self.pdf_bytes = pdf_bytes
        self.pdf_filename = pdf_filename or "executive-snapshot.pdf"

    def envelope(self) -> Envelope:
        brand = escape(self.tenant_name)
        period = escape(self.month_label)
        prior = escape(self.prior_month_label)

        kpi_rows = "".join(
            f"""
            <tr>
              <td style="border-bottom:1px solid #e6e5de;padding:8px 10px;color:#464a3e;font-size:13px"><b>{escape(str(item.get('label', '')))}</b></td>
              <td style="border-bottom:1px solid #e6e5de;padding:8px 10px;color:#14160f;font-size:14px;font-weight:700" align="right">{escape(str(item.get('formatted_value', '0')))}</td>
              <td style="border-bottom:1px solid #e6e5de;padding:8px 10px;color:#5c6152;font-size:12px" align="right">{escape(str(item.get('delta_label', '—')))}</td>
            </tr>
            """
            for item in self.kpis
        )

        if self.top_doors:
            door_rows = "".join(
                f"""
                <tr>
                  <td style="border-bottom:1px solid #ecebe4;padding:7px 10px;color:#14160f;font-size:13px">{escape(str(d.get('store_name', '')))}<br/><span style="color:#6b705c;font-size:11px">{escape(str(d.get('market', '')))}</span></td>
                  <td style="border-bottom:1px solid #ecebe4;padding:7px 10px;color:#464a3e;font-size:12px" align="right">{int(d.get('consumers_sampled') or 0):,}</td>
                  <td style="border-bottom:1px solid #ecebe4;padding:7px 10px;color:#464a3e;font-size:12px" align="right">{int(d.get('units_sold') or 0):,}</td>
                  <td style="border-bottom:1px solid #ecebe4;padding:7px 10px;color:#14160f;font-size:13px;font-weight:700" align="right">{escape(str(d.get('conversion_label', '—')))}</td>
                </tr>
                """
                for d in self.top_doors[:5]
            )
            doors_block = f"""
            <h3 style="margin:20px 0 8px;font-size:13px;text-transform:uppercase;letter-spacing:0.08em;color:#5c6152">Top converting doors</h3>
            <table cellpadding="0" cellspacing="0" style="border-collapse:collapse;border:1px solid #e2e1d9;width:100%">
              <thead>
                <tr style="background:#f5f4ee">
                  <th align="left" style="padding:7px 10px;font-size:11px;color:#6b705c;text-transform:uppercase">Store</th>
                  <th align="right" style="padding:7px 10px;font-size:11px;color:#6b705c;text-transform:uppercase">Sampled</th>
                  <th align="right" style="padding:7px 10px;font-size:11px;color:#6b705c;text-transform:uppercase">Sold</th>
                  <th align="right" style="padding:7px 10px;font-size:11px;color:#6b705c;text-transform:uppercase">Conv.</th>
                </tr>
              </thead>
              <tbody>{door_rows}</tbody>
            </table>
            """
        else:
            doors_block = ""

        callouts = self.shelf_summary.get("callouts") or []
        osa_pct = self.shelf_summary.get("osa_rate_pct")
        tag_pct = self.shelf_summary.get("price_tag_rate_pct")
        shelf_bits: list[str] = []
        if osa_pct is not None:
            shelf_bits.append(f"<b>{osa_pct}%</b> On-Shelf Availability")
        if tag_pct is not None:
            shelf_bits.append(f"<b>{tag_pct}%</b> Price-Tag Compliance")
        if self.shelf_summary.get("median_price"):
            shelf_bits.append(
                f"Median shelf price <b>${escape(str(self.shelf_summary['median_price']))}</b>"
            )
        callout_items = "".join(
            f"<li style='margin:4px 0;font-size:12.5px;color:#2c3024'><b>{escape(str(c.get('store_name', '')))}</b> ({escape(str(c.get('market', '')))}) — {escape(str(c.get('detail', '')))}</li>"
            for c in callouts[:5]
        )
        shelf_block = (
            f"""
            <h3 style="margin:20px 0 8px;font-size:13px;text-transform:uppercase;letter-spacing:0.08em;color:#5c6152">Shelf compliance &amp; pricing</h3>
            <p style="margin:0 0 8px;font-size:13px;color:#14160f">{' &nbsp;·&nbsp; '.join(shelf_bits) if shelf_bits else 'No shelf audits recorded this month.'}</p>
            {f"<ul style='margin:6px 0 0 18px;padding:0'>{callout_items}</ul>" if callout_items else ''}
            """
            if (shelf_bits or callout_items)
            else ""
        )

        if self.themes:
            theme_items = "".join(
                f"<li style='margin:6px 0;font-size:13px;color:#14160f'><b>{escape(str(t.get('title', '')))}</b>"
                + (
                    f" — <span style='color:#464a3e'>{escape(str(t.get('summary', '')))}</span>"
                    if t.get("summary")
                    else ""
                )
                + "</li>"
                for t in self.themes[:3]
            )
            themes_block = f"""
            <h3 style="margin:20px 0 8px;font-size:13px;text-transform:uppercase;letter-spacing:0.08em;color:#5c6152">Top shopper themes</h3>
            <ul style="margin:0 0 0 18px;padding:0">{theme_items}</ul>
            """
        else:
            themes_block = ""

        if self.photos:
            photo_links = "".join(
                f"<li style='margin:4px 0;font-size:12.5px'><a href='{escape(str(p.get('url', '')))}' style='color:#14160f;font-weight:600'>{escape(str(p.get('store', 'Field photo')))}</a>"
                + (
                    f" <span style='color:#6b705c'>({escape(str(p.get('event_date', '')))})</span>"
                    if p.get("event_date")
                    else ""
                )
                + "</li>"
                for p in self.photos[:4]
            )
            photos_block = f"""
            <h3 style="margin:20px 0 8px;font-size:13px;text-transform:uppercase;letter-spacing:0.08em;color:#5c6152">Featured field photos</h3>
            <ul style="margin:0 0 0 18px;padding:0">{photo_links}</ul>
            """
        else:
            photos_block = ""

        attach_note = (
            "Your print-ready Executive Snapshot PDF brief is attached."
            if self.pdf_bytes
            else "Open Program Reports in Spark to explore the full interactive brief."
        )
        body = f"""
        <div style="font-family:Helvetica,Arial,sans-serif;color:#14160f;line-height:1.5;max-width:620px;background:#ffffff">
          <div style="border-bottom:3px solid #c5f546;padding-bottom:12px;margin-bottom:16px">
            <p style="margin:0 0 4px;font-size:11px;letter-spacing:0.12em;text-transform:uppercase;color:#6b705c">
              {brand} · Monthly Executive Snapshot
            </p>
            <h2 style="margin:0;font-size:22px;color:#14160f">{period} Program Brief</h2>
            <p style="margin:4px 0 0;font-size:12.5px;color:#6b705c">Compared against {prior}</p>
          </div>
          <table cellpadding="0" cellspacing="0" style="border-collapse:collapse;border:1px solid #e2e1d9;width:100%">
            <thead>
              <tr style="background:#f5f4ee">
                <th align="left" style="padding:7px 10px;font-size:11px;color:#6b705c;text-transform:uppercase">Metric</th>
                <th align="right" style="padding:7px 10px;font-size:11px;color:#6b705c;text-transform:uppercase">{period}</th>
                <th align="right" style="padding:7px 10px;font-size:11px;color:#6b705c;text-transform:uppercase">vs. {prior}</th>
              </tr>
            </thead>
            <tbody>{kpi_rows}</tbody>
          </table>
          {doors_block}
          {shelf_block}
          {themes_block}
          {photos_block}
          <p style="margin:20px 0 0;color:#6b705c;font-size:12px">{escape(attach_note)}</p>
        </div>
        """
        attachments = (
            [
                {
                    "filename": self.pdf_filename,
                    "content": list(self.pdf_bytes),
                    "content_type": "application/pdf",
                }
            ]
            if self.pdf_bytes
            else []
        )
        return Envelope(
            subject=f"{self.tenant_name}: {self.month_label} Executive Snapshot",
            html=body,
            to_emails=self.recipients,
            attachments=attachments,
        )


class RegionalShelfAlertMailer(Mailer):
    """Instant territory-scoped shelf alert email for brand regional managers."""

    def __init__(
        self,
        *,
        recipients: Iterable[str],
        tenant_name: str,
        subscription_name: str,
        store_name: str,
        state_code: str,
        event_date: str,
        reasons: list[str],
        observed_price: str | None = None,
        target_msrp: str | None = None,
        drift_pct: float | None = None,
        photo_url: str | None = None,
        is_test: bool = False,
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
        self.subscription_name = subscription_name or "Regional Shelf Alert"
        self.store_name = store_name or "Retail Store"
        self.state_code = (state_code or "").strip().upper()
        self.event_date = event_date or ""
        self.reasons = list(reasons or [])
        self.observed_price = observed_price
        self.target_msrp = target_msrp
        self.drift_pct = drift_pct
        self.photo_url = (photo_url or "").strip()
        self.is_test = bool(is_test)

    def envelope(self) -> Envelope:
        brand = escape(self.tenant_name)
        rule = escape(self.subscription_name)
        store = escape(self.store_name)
        loc = f"{store} ({escape(self.state_code)})" if self.state_code else store
        reason_items = "".join(
            f"<li style='margin:4px 0;font-size:13px;color:#14160f'>{escape(r)}</li>"
            for r in self.reasons
        )
        price_line = ""
        if self.observed_price or self.target_msrp:
            bits = []
            if self.observed_price:
                bits.append(f"Observed shelf price: <b>${escape(self.observed_price)}</b>")
            if self.target_msrp:
                bits.append(f"Target MSRP: <b>${escape(self.target_msrp)}</b>")
            if self.drift_pct is not None:
                sign = "+" if self.drift_pct > 0 else ""
                bits.append(f"Drift: <b>{sign}{self.drift_pct:.1f}%</b>")
            price_line = f"<p style='margin:12px 0 0;font-size:13px;color:#464a3e'>{' &nbsp;·&nbsp; '.join(bits)}</p>"

        photo_line = (
            f"<p style='margin:14px 0 0;font-size:12.5px'><a href='{escape(self.photo_url)}' style='color:#14160f;font-weight:600'>View audited shelf photo →</a></p>"
            if self.photo_url
            else ""
        )
        test_banner = (
            "<p style='margin:0 0 12px;padding:8px 10px;background:#f5f4ee;border-left:3px solid #c5f546;font-size:12px;color:#464a3e'><b>Test alert:</b> This is a verification ping from Spark Regional Alerts.</p>"
            if self.is_test
            else ""
        )
        body = f"""
        <div style="font-family:Helvetica,Arial,sans-serif;color:#14160f;line-height:1.5;max-width:580px">
          {test_banner}
          <p style="margin:0 0 4px;font-size:11px;letter-spacing:0.1em;text-transform:uppercase;color:#a8340f;font-weight:700">
            {brand} · Regional Shelf Alert · {rule}
          </p>
          <h2 style="margin:0 0 6px;font-size:19px">{loc}</h2>
          {f"<p style='margin:0 0 12px;font-size:12.5px;color:#6b705c'>Audit date: {escape(self.event_date)}</p>" if self.event_date else ""}
          <ul style="margin:0 0 0 18px;padding:0">{reason_items}</ul>
          {price_line}
          {photo_line}
          <p style="margin:16px 0 0;font-size:11.5px;color:#6b705c">
            Sent automatically by your Spark Regional Alert rule &ldquo;{rule}&rdquo;. Manage rules anytime under Reports &amp; ROI → Shelf &amp; Voice.
          </p>
        </div>
        """
        prefix = "[TEST] " if self.is_test else ""
        return Envelope(
            subject=f"{prefix}{self.tenant_name} Shelf Alert: {self.store_name}"
            + (f" ({self.state_code})" if self.state_code else ""),
            html=body,
            to_emails=self.recipients,
        )
