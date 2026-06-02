"""
Coverage for the `repair_event_dates` backfill management command.

It copies a derived date (start_time → request.date → request.start_time) into
``Event.date`` for events that have ``date IS NULL`` but ``start_time`` set —
the pre-#718 shape that made the recap "Event Date" show "N/A". Mirrors the
behaviour contract of repair_missing_events_for_approved_requests:
    * DRY-RUN by default (writes nothing); --execute opts in to writes.
    * idempotent (a second --execute run updates 0 rows).
    * --tenant scopes to one tenant.
    * saves ONLY the `date` field.
"""

from __future__ import annotations

from datetime import datetime, timezone as _tz
from io import StringIO

import pytest
from django.core.management import call_command

from events import models as event_models
from events.tests.base import EventsGraphQLTestCase


def _aware(y, m, d, hh=10, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=_tz.utc)


@pytest.mark.django_db
class TestRepairEventDates(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        # NOTE: deliberately NOT calling setup_default_roles() — this command
        # test only needs a tenant + event/request statuses + the system user,
        # and the fixed-id role creation collides with the TransactionTestCase
        # table flush when this file is run interleaved with transaction=True
        # tests in the same session.
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Liquid Death", slug="liquid-death")
        self.other_tenant = self.create_tenant(name="Total Wireless", slug="total-wireless")
        self.ev_status = self.create_event_status(
            name="Approved", tenant=self.tenant, slug="approved"
        )
        self.ev_status_other = self.create_event_status(
            name="Approved", tenant=self.other_tenant, slug="approved"
        )
        self.request_type = self.create_request_type(
            name="Sampling", tenant=self.tenant
        )
        self.request_type_other = self.create_request_type(
            name="Sampling", tenant=self.other_tenant
        )

    # ─── factories ───────────────────────────────────────────────────

    def _make_event(
        self,
        *,
        tenant=None,
        status=None,
        date,
        start_time,
        req_date=None,
        req_start=None,
        with_request=True,
    ):
        tenant = tenant or self.tenant
        status = status or self.ev_status
        request_type = (
            self.request_type if tenant == self.tenant else self.request_type_other
        )
        request = None
        if with_request:
            req_status = event_models.RequestStatus.objects.create(
                name="Approved",
                slug="approved",
                tenant=tenant,
                created_by=self.system_user,
            )
            request = event_models.Request.objects.create(
                name="Vons",
                address="1608 Broadway St",
                tenant=tenant,
                status=req_status,
                request_type=request_type,
                date=req_date,
                start_time=req_start,
                created_by=self.system_user,
            )
        return event_models.Event.objects.create(
            name="Vons activation",
            tenant=tenant,
            request=request,
            status=status,
            date=date,
            start_time=start_time,
            created_by=self.system_user,
        )

    # ─── core: sets date from start_time ─────────────────────────────

    def test_execute_sets_date_from_start_time(self):
        ev = self._make_event(date=None, start_time=_aware(2026, 5, 29, 12))
        out = StringIO()
        call_command("repair_event_dates", execute=True, stdout=out)
        ev.refresh_from_db()
        assert ev.date == _aware(2026, 5, 29, 12)
        assert "Updated: 1 event(s)" in out.getvalue()

    def test_derive_date_falls_back_to_request_date_then_start_time(self):
        # The command's single-row deriver: start_time → request.date →
        # request.start_time. The queryset already requires a non-null
        # start_time, but the helper stays correct if start_time is absent
        # (e.g. reused elsewhere) — assert the fallback branches directly.
        from events.management.commands.repair_event_dates import _derive_date

        ev = self._make_event(
            date=None,
            start_time=_aware(2026, 5, 29, 12),
            req_date=_aware(2026, 5, 29, 9),
            req_start=_aware(2026, 5, 29, 8),
        )
        ev = event_models.Event.objects.select_related("request").get(id=ev.id)
        # start_time present → wins.
        assert _derive_date(ev) == _aware(2026, 5, 29, 12)
        # start_time absent → request.date.
        ev.start_time = None
        assert _derive_date(ev) == _aware(2026, 5, 29, 9)
        # start_time AND request.date absent → request.start_time.
        ev.request.date = None
        assert _derive_date(ev) == _aware(2026, 5, 29, 8)

    # ─── idempotency ─────────────────────────────────────────────────

    def test_second_execute_run_updates_zero(self):
        self._make_event(date=None, start_time=_aware(2026, 5, 29, 12))
        first = StringIO()
        call_command("repair_event_dates", execute=True, stdout=first)
        assert "Updated: 1 event(s)" in first.getvalue()

        second = StringIO()
        call_command("repair_event_dates", execute=True, stdout=second)
        assert "Updated: 0 event(s)" in second.getvalue()

    # ─── dry-run writes nothing ──────────────────────────────────────

    def test_dry_run_is_default_and_writes_nothing(self):
        ev = self._make_event(date=None, start_time=_aware(2026, 5, 29, 12))
        out = StringIO()
        # No execute kwarg → dry-run default.
        call_command("repair_event_dates", stdout=out)
        ev.refresh_from_db()
        assert ev.date is None  # untouched
        report = out.getvalue()
        assert "DRY RUN" in report
        assert "Would update: 1 event(s)" in report

    # ─── --tenant scoping ────────────────────────────────────────────

    def test_tenant_scope_only_touches_that_tenant(self):
        mine = self._make_event(
            tenant=self.tenant, status=self.ev_status,
            date=None, start_time=_aware(2026, 5, 29, 12),
        )
        theirs = self._make_event(
            tenant=self.other_tenant, status=self.ev_status_other,
            date=None, start_time=_aware(2026, 6, 1, 12),
        )
        out = StringIO()
        call_command(
            "repair_event_dates", execute=True, tenant="liquid-death", stdout=out
        )
        mine.refresh_from_db()
        theirs.refresh_from_db()
        assert mine.date == _aware(2026, 5, 29, 12)  # repaired
        assert theirs.date is None  # other tenant untouched
        assert "Updated: 1 event(s)" in out.getvalue()

    # ─── leaves already-dated rows alone ─────────────────────────────

    def test_event_with_existing_date_is_not_touched(self):
        ev = self._make_event(
            date=_aware(2026, 1, 1), start_time=_aware(2026, 5, 29, 12)
        )
        out = StringIO()
        call_command("repair_event_dates", execute=True, stdout=out)
        ev.refresh_from_db()
        assert ev.date == _aware(2026, 1, 1)  # unchanged
        assert "Updated: 0 event(s)" in out.getvalue()

    # ─── only saves the date field ───────────────────────────────────

    def test_only_date_field_is_saved(self):
        ev = self._make_event(date=None, start_time=_aware(2026, 5, 29, 12))
        original_name = ev.name
        # Mutate name in memory only — should NOT be persisted by the command,
        # which uses update_fields=["date"].
        event_models.Event.objects.filter(id=ev.id).update(name="UNTOUCHED")
        call_command("repair_event_dates", execute=True, stdout=StringIO())
        ev.refresh_from_db()
        assert ev.date == _aware(2026, 5, 29, 12)
        assert ev.name == "UNTOUCHED"  # command didn't clobber other fields
        assert original_name != "UNTOUCHED"
