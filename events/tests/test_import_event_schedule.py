"""Coverage for the bulk client-schedule importer
(events/management/commands/import_event_schedule.py).

Pins the two pieces that aren't already covered by test_batch_requests:
the Eastern-timezone auto-resolution (prefers daylight EDT for summer
dates) and the XLSX-build → importer chain — that the file the command
hands the importer parses cleanly, lands the venue wall-clock at the right
UTC instant (the DST trap: June = EDT, -240 min), and dedups on re-run.
"""

import datetime

import pytest

from events.management.commands.import_event_schedule import Command, _build_xlsx
from events.batch_requests import import_requests_from_excel_bytes
from events.models import Event, EventStatus, RequestStatus, TimeZone
from events.tests.base import EventsGraphQLTestCase


_ROWS = [
    {
        "name": "Kroger #409 — Grand Blanc · 6/19",
        "date": "06/19/2026",
        "start_time": "15:00",
        "end_time": "19:00",
        "address": "12731 S Saginaw St, Grand Blanc, MI 48439",
        "store_number": "409",
        "retailer_name": "Kroger",
        "store_manager_phone": "(810) 695-6384",
        "notes": None,
    },
    {
        "name": "Kroger #526 — Milford · 6/20",
        "date": "06/20/2026",
        "start_time": "10:00",
        "end_time": "14:00",
        "address": "670 Highland Ave, Milford, MI 48381",
        "store_number": "526",
        "retailer_name": "Kroger",
        "store_manager_phone": "(248) 685-1528",
        "notes": None,
    },
]


@pytest.mark.django_db(transaction=True)
class TestImportEventSchedule(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Stone House Bread")
        # Both Eastern rows present so the EDT-preference is a real choice.
        self.edt = TimeZone.objects.create(
            name="Eastern Daylight Time", code="EDT", offset=-240,
            created_by=self.system_user,
        )
        self.est = TimeZone.objects.create(
            name="Eastern Standard Time", code="EST", offset=-300,
            created_by=self.system_user,
        )
        self.request_type = self.create_request_type(
            name="Retail Sampling", tenant=self.tenant,
        )
        self.event_type = self.create_event_type(
            name="Retail Sampling", tenant=self.tenant,
        )
        RequestStatus.objects.create(
            tenant=self.tenant, name="Approved", slug="approved",
            created_by=self.system_user,
        )
        EventStatus.objects.create(
            tenant=self.tenant, name="Approved", slug="approved",
            created_by=self.system_user,
        )

    # ---------- timezone resolution ----------

    def test_resolve_timezone_prefers_daylight_for_summer(self):
        # Auto-resolution must pick EDT (-240), not EST (-300): all the
        # activations are June/July, so the true offset is daylight.
        resolved = Command()._resolve_timezone(None)
        assert resolved.code == "EDT"

    def test_resolve_timezone_honors_forced_code(self):
        assert Command()._resolve_timezone("EST").code == "EST"

    # ---------- build → import chain ----------

    def test_build_and_import_creates_correctly_timed_events(self):
        xlsx = _build_xlsx(
            rows=_ROWS,
            scheduling_status="already_scheduled",
            timezone_code=self.edt.code,
            request_type_id=self.request_type.id,
            event_type_id=self.event_type.id,
        )
        result = import_requests_from_excel_bytes(
            file_bytes=xlsx,
            tenant_id=self.tenant.id,
            created_by_id=self.system_user.id,
            default_timezone_id=self.edt.id,
            default_request_type_id=self.request_type.id,
            sheet_name="Requests",
            dry_run=False,
            rollback_on_error=True,
        )
        assert result.failed_count == 0, [r.message for r in result.rows if not r.success]
        assert result.success_count == 2

        ev = Event.objects.get(tenant=self.tenant, name="Kroger #409 — Grand Blanc · 6/19")
        # 15:00 local EDT (-240 min) → 19:00 UTC. The DST-correct instant.
        assert ev.start_time.astimezone(datetime.timezone.utc).hour == 19
        assert ev.start_time.astimezone(datetime.timezone.utc).date() == datetime.date(2026, 6, 19)
        # Displays back as 15:00 when rendered at the event's -240 offset.
        assert ev.end_time.astimezone(datetime.timezone.utc).hour == 23  # 19:00 + 4h
        assert ev.event_type_id == self.event_type.id
        assert "Grand Blanc" in ev.address

    def test_reimport_is_idempotent(self):
        kwargs = dict(
            tenant_id=self.tenant.id,
            created_by_id=self.system_user.id,
            default_timezone_id=self.edt.id,
            default_request_type_id=self.request_type.id,
            sheet_name="Requests",
            rollback_on_error=True,
        )
        xlsx = _build_xlsx(
            rows=_ROWS, scheduling_status="already_scheduled",
            timezone_code=self.edt.code, request_type_id=self.request_type.id,
            event_type_id=self.event_type.id,
        )
        first = import_requests_from_excel_bytes(file_bytes=xlsx, dry_run=False, **kwargs)
        assert first.success_count == 2
        # Second run: same store + start time → both skipped, none duplicated.
        second = import_requests_from_excel_bytes(file_bytes=xlsx, dry_run=False, **kwargs)
        assert second.success_count == 0
        assert second.skipped_count == 2
        assert Event.objects.filter(tenant=self.tenant).count() == 2
