"""Email-link approval must retry the tracker mirror, and say so when it misses.

`upsert_request_row` swallows its own failures, and once an RMM hand-types a
missing row `reconcile_tracker_rows` treats it as a twin and never writes
Spark's row. So `_do_approve` retries the mirror explicitly and logs a miss.
"""

from __future__ import annotations

from datetime import datetime, timezone as _tz
from unittest.mock import patch

import pytest

from events import models as event_models
from events import views as views_module
from events.tests.base import EventsGraphQLTestCase


def _aware(y, m, d, hh=10, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=_tz.utc)


@pytest.mark.django_db(transaction=True)
class TestPublicApprovalMirrorsTracker(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Mirror Tenant", slug="mirror-tenant")
        self.req_pending = self.create_request_status(
            name="Pending", tenant=self.tenant, slug="pending", is_default=True
        )
        self.req_approved = self.create_request_status(
            name="Approved", tenant=self.tenant, slug="approved"
        )
        self.ev_pending = self.create_event_status(
            name="Pending", tenant=self.tenant, slug="pending", is_default=True
        )
        self.event_type = self.create_event_type(name="In Store", tenant=self.tenant)
        self.request_type = self.create_request_type(
            name="Sampling", tenant=self.tenant
        )

    def _pending_request(self):
        return event_models.Request.objects.create(
            name="Req",
            address="100 E Sunrise Hwy, Valley Stream, NY 11581, USA",
            tenant=self.tenant,
            status=self.req_pending,
            request_type=self.request_type,
            start_time=_aware(2026, 8, 17, 12),
            end_time=_aware(2026, 8, 17, 14),
            created_by=self.system_user,
        )

    def test_approval_retries_the_mirror(self):
        """The explicit retry runs even though post_save already attempted it."""
        req = self._pending_request()
        with patch("utils.sheets_mirror.upsert_request_row", return_value=True), patch(
            "events.views.upsert_request_row", return_value=True
        ) as retry:
            views_module._do_approve(req, "rmm@example.com")

        assert retry.call_count == 1, "approval must retry the tracker mirror"
        assert retry.call_args[0][0].id == req.id

    def test_a_silent_miss_is_logged(self):
        """A mirror that returns False must leave a trace naming the request.

        Asserted on the module logger rather than caplog: the app's LOGGING
        config stops `events.views` records propagating to pytest's handler."""
        req = self._pending_request()
        with patch.object(views_module.logger, "warning") as warn, patch(
            "utils.sheets_mirror.upsert_request_row", return_value=False
        ), patch("events.views.upsert_request_row", return_value=False):
            views_module._do_approve(req, "rmm@example.com")

        misses = [
            c for c in warn.call_args_list
            if "tracker mirror did not write" in str(c.args[0])
        ]
        assert misses, "a dropped row must be logged, not swallowed silently"
        assert req.id in misses[0].args

    def test_a_raising_mirror_never_breaks_approval(self):
        """A Sheets outage must not stop the client's approval going through."""
        req = self._pending_request()
        with patch(
            "utils.sheets_mirror.upsert_request_row",
            side_effect=RuntimeError("Sheets 500"),
        ), patch(
            "events.views.upsert_request_row",
            side_effect=RuntimeError("Sheets 500"),
        ):
            views_module._do_approve(req, "rmm@example.com")

        req.refresh_from_db()
        assert (req.status.slug or "").lower() == "approved"
