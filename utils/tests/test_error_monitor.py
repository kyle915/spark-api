"""Backend error monitor: log records become BackendErrorEvent rows, alerts
throttle per signature, expected GraphQL denials stay quiet, and a Resend
send with no id logs at ERROR (feeding the monitor)."""
import logging
from unittest.mock import patch

import pytest
from graphql import GraphQLError as CoreGraphQLError

from digest.models import BackendErrorEvent
from utils.error_monitor import ErrorEventLogHandler, report_backend_error


@pytest.mark.django_db(transaction=True)
class TestErrorMonitor:
    def test_handler_records_error_and_counts(self):
        # The settings LOGGING wiring already routes root ERROR records into
        # ErrorEventLogHandler — log through a plain logger and assert the
        # real production path.
        logger = logging.getLogger("spark.test.monitor")
        logger.error("boom %s", 1)
        logger.error("boom %s", 2)
        row = BackendErrorEvent.objects.get()
        assert row.signature.startswith("spark.test.monitor:")
        assert row.count == 2
        assert "boom 2" in row.message  # latest message wins

    def test_alert_throttles_per_signature(self, settings):
        settings.BACKEND_ERROR_ALERTS_ENABLED = True
        settings.BACKEND_ERROR_ALERT_EMAILS = ["kyle@igniteproductions.co"]
        with patch("utils.error_monitor._send_alert") as send:
            report_backend_error(signature="X:y", message="m1")
            report_backend_error(signature="X:y", message="m2")
        assert send.call_count == 1  # second within the hour is throttled
        assert BackendErrorEvent.objects.get(signature="X:y").count == 2

    def test_alerts_disabled_still_records(self, settings):
        settings.BACKEND_ERROR_ALERTS_ENABLED = False
        with patch("utils.error_monitor._send_alert") as send:
            report_backend_error(signature="Q:z", message="m")
        send.assert_not_called()
        assert BackendErrorEvent.objects.filter(signature="Q:z").exists()

    def test_schema_hook_splits_expected_from_unexpected(self):
        from utils.graphql.monitored_schema import MonitoredJwtSchema

        expected = CoreGraphQLError("Event not found.")
        expected.original_error = CoreGraphQLError("Event not found.")
        crash = CoreGraphQLError("Unexpected error.")
        try:
            raise ValueError("kaput")
        except ValueError as e:
            crash.original_error = e

        with patch("utils.graphql.monitored_schema.logger.error") as err, \
             patch("utils.graphql.monitored_schema._expected_logger.info") as info:
            MonitoredJwtSchema.process_errors(None, [expected, crash])
        assert err.call_count == 1
        assert info.call_count == 1

    def test_resend_missing_id_logs_error(self):
        from utils.mailer import Envelope, ResendMailDriver

        env_ = Envelope(subject="t", html="<p>x</p>", to_emails=["a@b.co"])
        with patch("utils.mailer.resend.Emails.send", return_value={}), \
             patch("utils.mailer.logger.error") as err:
            ResendMailDriver().send(env_)
        assert err.call_count == 1
