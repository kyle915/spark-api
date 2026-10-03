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

import io
from unittest import mock

from django.core.management import call_command

from events.management.commands.import_event_schedule import (
    Command,
    _build_xlsx,
    _drop_rows_already_in_spark,
    _match_rows_to_spark,
    _walkin_events_for_rows,
)
from events.batch_requests import import_requests_from_excel_bytes
from events.models import Event, EventStatus, Request, RequestStatus, State, TimeZone
from events.tests.base import EventsGraphQLTestCase
from utils.sheets_mirror import suppress_sheet_mirror, upsert_request_row


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


def _json_report(stdout: str) -> list[dict]:
    import json

    return [
        json.loads(ln[len("JSON_ROW:"):])
        for ln in stdout.splitlines()
        if ln.startswith("JSON_ROW:")
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

    # ---------- per-row timezone + Feel Free schedule ----------

    def test_row_level_timezone_overrides_command_default(self):
        # Multi-market schedules (FL + TX) carry timezone_code per row; the
        # command-level code is only the fallback.
        cdt = TimeZone.objects.create(
            name="Central Daylight Time", code="CDT", offset=-300,
            created_by=self.system_user,
        )
        rows = [dict(_ROWS[0]), dict(_ROWS[1], timezone_code="CDT")]
        xlsx = _build_xlsx(
            rows=rows,
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
        by_name = {e.name: e for e in Event.objects.filter(tenant=self.tenant)}
        # EDT row: 15:00 local → 19:00 UTC. CDT row: 10:00 local → 15:00 UTC.
        assert by_name["Kroger #409 — Grand Blanc · 6/19"].start_time.hour == 19
        assert by_name["Kroger #526 — Milford · 6/20"].start_time.hour == 15

    def test_feel_free_schedule_dry_runs_clean_with_create_tenant(self):
        # End-to-end dry-run of the committed Feel Free schedule — the exact
        # prod invocation: creates the tenant, validates all 249 rows across
        # both time zones, writes no events.
        import io as _io

        from django.core.management import call_command

        from events.models import Request
        from tenants.models import Tenant

        TimeZone.objects.create(
            name="Central Daylight Time", code="CDT", offset=-300,
            created_by=self.system_user,
        )
        out = _io.StringIO()
        call_command(
            "import_event_schedule",
            "--schedule", "feel_free_summer2026",
            "--create-tenant",
            "--owner-email", self.system_user.email,
            stdout=out,
        )
        report = out.getvalue()
        tenant = Tenant.objects.filter(name__iexact="Feel Free").first()
        assert tenant is not None and tenant.slug == "feel-free"
        assert "CREATED" in report
        assert "failed     : 0" in report, report[-2000:]
        assert "would create : 249" in report or ": 249" in report
        # dry-run: no events/requests written
        assert not Event.objects.filter(tenant=tenant).exists()
        assert not Request.objects.filter(tenant=tenant).exists()

    # ---------- fuzzy dedup / Torch schedule ----------

    def _existing_request(self, *, name, address, start_utc):
        return Request.objects.create(
            tenant=self.tenant,
            created_by=self.system_user,
            name=name,
            date=start_utc,
            start_time=start_utc,
            end_time=start_utc + datetime.timedelta(hours=3),
            address=address,
            request_type=self.request_type,
        )

    def test_fuzzy_dedup_skips_same_street_address_within_the_hour(self):
        # 15:00 EDT on 6/19 = 19:00 UTC; Spark stored the geocoder's spelling.
        existing = self._existing_request(
            name="Kroger",
            address="12731 South Saginaw Street, Grand Blanc, MI 48439, USA",
            start_utc=datetime.datetime(2026, 6, 19, 19, 30, tzinfo=datetime.timezone.utc),
        )
        kept, skipped = _drop_rows_already_in_spark(_ROWS, self.tenant.id, self.edt)
        assert [r["store_number"] for r in kept] == ["526"]
        assert skipped == [(f"06/19/2026 15:00 {_ROWS[0]['name']}", str(existing.uuid))]

    def test_fuzzy_dedup_ignores_generic_name_at_another_address(self):
        self._existing_request(
            name=_ROWS[0]["name"],
            address="1 Other Rd, Flint, MI 48502",
            start_utc=datetime.datetime(2026, 6, 19, 19, 0, tzinfo=datetime.timezone.utc),
        )
        kept, skipped = _drop_rows_already_in_spark(_ROWS, self.tenant.id, self.edt)
        assert len(kept) == 2 and skipped == []

    def test_torch_chunk_dry_runs_clean_by_slug_without_sheet_mirror(self):
        torch = self.create_tenant(name="Torch THC", slug="keee-torch-thc")
        for code, off in (("CDT", -300), ("CST", -360)):
            TimeZone.objects.create(name=code, code=code, offset=off, created_by=self.system_user)
        for code in ("FL", "TX", "MO", "GA", "IL", "TN", "SC", "KS", "OH"):
            State.objects.create(name=code, code=code, created_by=self.system_user)
        out = io.StringIO()
        with mock.patch("events.signals.queues") as queues:
            call_command(
                "import_event_schedule",
                "--schedule", "torch_retail_sync_2026_10_02_p01",
                "--owner-email", self.system_user.email,
                stdout=out,
            )
        report = out.getvalue()
        assert f"tenant id : {torch.id}" in report
        assert "failed     : 0" in report, report[-2000:]
        assert "would create : 368" in report, report[-2000:]
        rows = _json_report(report)
        assert len(rows) == 368 and {r["outcome"] for r in rows} == {"would_create"}
        assert all(r["source_row"] for r in rows)
        assert not Request.objects.filter(tenant=torch).exists()
        queues.default.add.assert_not_called()

    def test_fuzzy_dedup_matches_a_retimed_row_on_the_same_day(self):
        # Spark booked 6/19 at 10:00 EDT; the sheet later moved it to 15:00.
        existing = self._existing_request(
            name="Kroger",
            address=_ROWS[0]["address"],
            start_utc=datetime.datetime(2026, 6, 19, 14, 0, tzinfo=datetime.timezone.utc),
        )
        kept, matches = _match_rows_to_spark(_ROWS, self.tenant.id, self.edt)
        assert [r["store_number"] for r in kept] == ["526"]
        assert [(m["request_uuid"], m["tier"]) for m in matches] == [(str(existing.uuid), "day")]

    def test_fuzzy_dedup_claims_each_request_once(self):
        # Two shifts at one store on one day, only the 15:00 one in Spark:
        # the 11:00 row must still import rather than ride the same request.
        early = dict(_ROWS[0], start_time="11:00", end_time="14:00", source_row=7)
        existing = self._existing_request(
            name="Kroger",
            address=_ROWS[0]["address"],
            start_utc=datetime.datetime(2026, 6, 19, 19, 0, tzinfo=datetime.timezone.utc),
        )
        kept, matches = _match_rows_to_spark([early, _ROWS[0]], self.tenant.id, self.edt)
        assert kept == [early]
        assert [(m["request_uuid"], m["tier"]) for m in matches] == [(str(existing.uuid), "time")]

    def test_link_walkin_events_adopts_the_orphan_instead_of_a_second_event(self):
        walkin = Event.objects.create(
            tenant=self.tenant,
            name="Kroger — Jun 19",
            address="12731 South Saginaw Street, Grand Blanc, MI 48439, USA",
            date=datetime.datetime(2026, 6, 19, 12, 0, tzinfo=datetime.timezone.utc),
            event_type=self.event_type,
            created_by=self.system_user,
        )
        other_day = Event.objects.create(
            tenant=self.tenant,
            name="Kroger — Jun 21",
            address=_ROWS[1]["address"],
            date=datetime.datetime(2026, 6, 21, 12, 0, tzinfo=datetime.timezone.utc),
            event_type=self.event_type,
            created_by=self.system_user,
        )
        assert _walkin_events_for_rows(_ROWS, self.tenant.id) == {0: walkin.id}

        rows = [dict(r, source_row=i + 2) for i, r in enumerate(_ROWS)]
        spec = {"tenant_name": self.tenant.name, "link_walkin_events": True,
                "mirror_to_sheet": False, "rows": rows}
        data_dir = self._write_schedule("test_walkin_link", spec)
        out = io.StringIO()
        with mock.patch(
            "events.management.commands.import_event_schedule._DATA_DIR", data_dir
        ), mock.patch("events.signals.queues"):
            call_command(
                "import_event_schedule", "--schedule", "test_walkin_link",
                "--owner-email", self.system_user.email, "--commit", stdout=out,
            )
        report = _json_report(out.getvalue())
        walkin.refresh_from_db()
        other_day.refresh_from_db()
        assert walkin.request is not None and walkin.request.address == _ROWS[0]["address"]
        assert other_day.request_id is None
        assert Event.objects.filter(request=walkin.request).count() == 1
        assert Event.objects.filter(tenant=self.tenant).count() == 3
        by_row = {r["source_row"]: r for r in report}
        assert by_row[2]["outcome"] == "created" and by_row[2]["linked_event_id"] == walkin.id
        assert by_row[3]["outcome"] == "created" and "linked_event_id" not in by_row[3]

    def _write_schedule(self, key, spec):
        import json
        import tempfile
        from pathlib import Path

        d = Path(tempfile.mkdtemp())
        (d / f"{key}.json").write_text(json.dumps(spec))
        return d

    def test_suppressed_mirror_never_reads_the_tenant_sheet(self):
        req = mock.Mock()
        with suppress_sheet_mirror():
            assert upsert_request_row(req) is False
        assert not req.mock_calls
