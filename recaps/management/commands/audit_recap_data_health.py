"""Recap data-health audit — catch implausible numbers BEFORE they reach a
client deck.

Custom-recap KPIs come from free-text CustomFieldValue rows via label
matching (``recaps.report_service._accumulate_custom`` → the shared
``_consumers_sampled_from_fields`` matcher). That matcher is the
single-source-of-truth and is deliberately NOT re-implemented here — but
label matching over free text can still mis-parse (the SHB "2,208
consumers" incident: a descriptive field digit-mashed to 1960 before the
#848 fix). This command runs the REAL matcher over each custom recap and
flags results that can't be right:

  * conversion > 100%  (willing-to-purchase > consumers-sampled — impossible)
  * consumers-sampled  > --max-consumers   (default 1000 / single event)
  * cans or packs sold > --max-units       (default 5000 / single event)

READ-ONLY. Window defaults to the current calendar year (matching the
dashboard); ``--all-time`` / ``--year N`` override. ``--tenant-slug`` scopes
to one client; otherwise every tenant with custom recaps is scanned. With
``--email`` a digest of the flags goes to the Ignite team. Prod:
``/internal/cron/recap-data-health`` + the ``recap-data-health`` workflow.
"""

from __future__ import annotations

import datetime

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = (
        "Flag custom recaps whose parsed KPIs are implausible "
        "(conversion >100%, absurd consumer/unit counts). Read-only."
    )

    def add_arguments(self, parser):
        parser.add_argument("--tenant-slug", default="", help="Scope to one tenant.")
        parser.add_argument("--year", type=int, default=None, help="Calendar year to scan.")
        parser.add_argument("--all-time", action="store_true", help="Scan every year.")
        parser.add_argument("--max-consumers", type=int, default=1000)
        parser.add_argument("--max-units", type=int, default=5000)
        parser.add_argument(
            "--email", action="store_true",
            help="Email the flag digest to the Ignite team.",
        )

    def handle(self, *args, **opts):
        from recaps.models import CustomRecap
        from recaps.report_service import (
            CampaignReportKpis,
            _accumulate_custom,
            implausibility_reasons,
        )
        from tenants.models import Tenant

        w = self.stdout.write
        max_consumers = opts["max_consumers"]
        max_units = opts["max_units"]

        qs = CustomRecap.objects.select_related("event", "event__tenant", "ambassador__user")

        if opts["tenant_slug"]:
            tenant = Tenant.objects.filter(slug=opts["tenant_slug"]).first()
            if tenant is None:
                w(f"no tenant with slug {opts['tenant_slug']!r}")
                return
            qs = qs.filter(event__tenant=tenant)

        if not opts["all_time"]:
            year = opts["year"] or timezone.localdate().year
            start = timezone.make_aware(datetime.datetime(year, 1, 1))
            end = timezone.make_aware(datetime.datetime(year + 1, 1, 1))
            # Window on the event's date (fallback start_time), matching the
            # dashboard's event-date basis.
            from django.db.models import Q

            qs = qs.filter(
                Q(event__date__gte=start, event__date__lt=end)
                | Q(event__start_time__gte=start, event__start_time__lt=end)
            )
            scope = f"year {year}"
        else:
            scope = "all-time"

        flags: list[dict] = []
        scanned = 0
        for cr in qs.iterator():
            scanned += 1
            try:
                kpis = CampaignReportKpis()
                _accumulate_custom(cr, kpis)
            except Exception as exc:  # noqa: BLE001 — never let one recap abort
                flags.append(self._flag(cr, "parse-error", repr(exc)[:120]))
                continue

            reasons = implausibility_reasons(
                kpis, max_consumers=max_consumers, max_units=max_units
            )
            if reasons:
                flags.append(self._flag(cr, "; ".join(reasons), None))

        w("")
        w(f"scope      : {scope}")
        w(f"scanned    : {scanned} custom recap(s)")
        w(f"flagged    : {len(flags)}")
        for f in flags:
            w(f"  ⚑ #{f['id']} {f['tenant']} · {f['event']} · {f['ba']} — {f['reason']}")

        if flags and opts["email"]:
            self._send_digest(flags, scope)
            w(self.style.SUCCESS("Flag digest emailed."))

    def _flag(self, cr, reason: str, extra: str | None) -> dict:
        amb = getattr(cr, "ambassador", None)
        user = getattr(amb, "user", None) if amb else None
        who = (f"{user.first_name} {user.last_name}".strip() if user else "") or "?"
        ev = getattr(cr, "event", None)
        return {
            "id": cr.id,
            "uuid": str(cr.uuid),
            "tenant": getattr(getattr(ev, "tenant", None), "name", "") or "",
            "event": getattr(ev, "name", "") or "(no event)",
            "ba": who,
            "reason": reason + (f" [{extra}]" if extra else ""),
        }

    def _send_digest(self, flags: list[dict], scope: str) -> None:
        import html as _html

        from tenants.support import _resolve_ignite_recipients
        from utils.mailer import Envelope, Mailer

        recipients = _resolve_ignite_recipients()
        if not recipients:
            self.stdout.write("  [digest] No Ignite recipients — skipped.")
            return

        rows = "".join(
            "<tr>"
            f"<td>#{f['id']}</td>"
            f"<td>{_html.escape(f['tenant'])}</td>"
            f"<td>{_html.escape(f['event'])}</td>"
            f"<td>{_html.escape(f['ba'])}</td>"
            f"<td>{_html.escape(f['reason'])}</td>"
            "</tr>"
            for f in flags
        )
        body = f"""
        <h2 style="margin:0 0 8px">{len(flags)} recap(s) with suspect numbers ({scope})</h2>
        <p style="margin:0 0 12px;color:#555">These custom recaps parsed into
          values that can't be right (conversion over 100%, or counts far above
          a single event). Review before the numbers land in a client report.</p>
        <table cellpadding="6" style="border-collapse:collapse" border="1">
          <tr><th>Recap</th><th>Client</th><th>Event</th><th>BA</th><th>Why flagged</th></tr>
          {rows}
        </table>
        <p style="color:#888;margin-top:12px">Recap data-health audit · read-only.
          Fix the recap's field value to clear the flag.</p>
        """

        class _HealthMailer(Mailer):
            def envelope(self) -> Envelope:
                return Envelope(
                    subject=f"[Spark] {len(flags)} recap(s) with suspect numbers ({scope})",
                    html=body,
                    to_emails=recipients,
                )

        _HealthMailer().send_now()
