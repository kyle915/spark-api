"""Backend error monitoring: durable ErrorEvent rows + throttled alert emails.

Two feeds, one sink:

1. ``ErrorEventLogHandler`` — attached to every configured logger at ERROR
   level (see settings.LOGGING wiring). Django logs unhandled request
   exceptions at ERROR ("django.request"), and app code already uses
   logger.error/exception at failure sites — so 500s, cron failures, and
   silent-email failures all land here with zero per-site changes.
2. ``MonitoredJwtSchema.process_errors`` (utils.graphql.monitored_schema) —
   GraphQL resolver crashes never reach Django's exception machinery (they
   are masked into the response's ``errors``), so the schema hook logs the
   UNEXPECTED ones (non-GraphQLError originals) at ERROR, which flows into
   feed 1. Expected business denials ("Event not found.") stay INFO.

Sink: get_or_create a ``digest.BackendErrorEvent`` per signature, bump
counts, and email an alert at most once per signature per hour. Alerts are
gated by settings.BACKEND_ERROR_ALERTS_ENABLED (defaults to on only when
DEBUG is off) so tests/CI don't pollute outboxes.

EVERYTHING here is best-effort: the monitor must never raise into the
request that triggered it, and must never recurse (an error while
reporting an error is swallowed).
"""

from __future__ import annotations

import logging
import threading
import traceback as tb_module
from datetime import timedelta

ALERT_MIN_INTERVAL = timedelta(hours=1)

_local = threading.local()
_internal_logger = logging.getLogger("utils.error_monitor.internal")

# Loggers the handler must ignore to avoid feedback loops or known noise.
_EXCLUDED_LOGGER_PREFIXES = (
    "utils.error_monitor",
    # strawberry's default error logging would double-report the expected
    # GraphQL denials; the schema hook feeds us the unexpected ones.
    "strawberry.execution",
)


def report_backend_error(
    *,
    signature: str,
    message: str,
    location: str = "",
    tb: str = "",
) -> None:
    """Record an error occurrence + send a throttled alert email."""
    if getattr(_local, "reporting", False):
        return
    _local.reporting = True
    try:
        from django.conf import settings
        from django.db.models import F
        from django.utils import timezone

        from digest.models import BackendErrorEvent

        row, created = BackendErrorEvent.objects.get_or_create(
            signature=signature[:255],
            defaults={
                "message": message[:5000],
                "location": location[:255],
                "traceback": tb[:20000],
            },
        )
        if not created:
            BackendErrorEvent.objects.filter(pk=row.pk).update(
                count=F("count") + 1,
                message=message[:5000],
                location=location[:255],
                traceback=tb[:20000],
                last_seen=timezone.now(),
            )
            row.refresh_from_db()

        if not getattr(settings, "BACKEND_ERROR_ALERTS_ENABLED", False):
            return
        now = timezone.now()
        if row.last_alerted_at and now - row.last_alerted_at < ALERT_MIN_INTERVAL:
            return
        # Claim the alert slot BEFORE sending so a slow/failing send can't
        # stampede duplicate emails from concurrent requests.
        claimed = BackendErrorEvent.objects.filter(
            pk=row.pk, last_alerted_at=row.last_alerted_at
        ).update(last_alerted_at=now)
        if claimed:
            _send_alert(row)
    except Exception:
        _internal_logger.warning("error-monitor reporting failed", exc_info=True)
    finally:
        _local.reporting = False


def _send_alert(row) -> None:
    from django.conf import settings

    from utils.mailer import Envelope, Mailer

    recipients = list(getattr(settings, "BACKEND_ERROR_ALERT_EMAILS", []) or [])
    if not recipients:
        return

    import html as _html

    body = f"""
    <h2 style="margin:0 0 8px">Backend error: {_html.escape(row.signature)}</h2>
    <p style="margin:0 0 4px"><b>Count:</b> {row.count} ·
       <b>First seen:</b> {row.first_seen:%Y-%m-%d %H:%M UTC} ·
       <b>Location:</b> {_html.escape(row.location or "-")}</p>
    <p style="margin:12px 0 4px"><b>Message</b></p>
    <pre style="white-space:pre-wrap;background:#f5f5f5;padding:8px">{_html.escape(row.message[:2000])}</pre>
    <p style="margin:12px 0 4px"><b>Traceback (tail)</b></p>
    <pre style="white-space:pre-wrap;background:#f5f5f5;padding:8px">{_html.escape(row.traceback[-4000:] or "-")}</pre>
    <p style="color:#888">Throttled: at most one email per signature per hour.
       Full history in digest_backenderrorevent.</p>
    """

    class _AlertMailer(Mailer):
        def envelope(self) -> Envelope:
            return Envelope(
                subject=f"[Spark alert] {row.signature} (×{row.count})",
                html=body,
                to_emails=recipients,
            )

    _AlertMailer().send()


class ErrorEventLogHandler(logging.Handler):
    """Funnels every ERROR-level log record into report_backend_error."""

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - thin
        try:
            if record.levelno < logging.ERROR:
                return
            if record.name.startswith(_EXCLUDED_LOGGER_PREFIXES):
                return
            signature = f"{record.name}:{record.funcName}"
            tb = ""
            if record.exc_info and record.exc_info[0] is not None:
                signature = f"{record.exc_info[0].__name__}:{signature}"
                tb = "".join(tb_module.format_exception(*record.exc_info))
            report_backend_error(
                signature=signature,
                message=record.getMessage(),
                location=f"{record.pathname}:{record.lineno}",
                tb=tb,
            )
        except Exception:
            pass
