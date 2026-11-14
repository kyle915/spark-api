"""The staffing-sheet reader: what it takes from a tab, how a row finds its
Spark event, and that it only ever mirrors.
"""

from __future__ import annotations

from datetime import date, datetime, timezone as dt_timezone

import pytest

from events.models import SheetShift
from events.sheet_staffing import (
    MATCH_AMBIGUOUS,
    MATCH_MATCHED,
    MATCH_NO_EVENT,
    ParsedRow,
    Source,
    match_event,
    name_key,
    parse_rows,
    sync_sheet_staffing,
)
from events.tests.base import EventsGraphQLTestCase

TODAY = date(2026, 11, 14)
SHEET = "sheet-under-test"

HEADER = [
    "State", "Day of Week", "Date", "Store Name", "Start Time", "End Time",
    "Address", "Brand", "Notes", "SKUs", "Confirmed BA Name ", "BA Rate",
    "Email", "Cell Phone #",
]


def _row(day="Nov 14", store="Plano Country Store", ba="Lashanda Beckwith",
         address="3205 Plano Rd, Bowling Green, KY 42104, USA"):
    return ["KY", "Saturday", day, store, "11a", "2p", address, "Famus Energy",
            "", "OG Luxe", ba, "$30", "ba@example.com", "12563358371"]


def _parse(values, fixed=7):
    return parse_rows(
        values, sheet_id=SHEET, today=TODAY, fixed_tenant_id=fixed,
        brand_tenant_ids=lambda brand: [],
    )


def test_reads_columns_by_header_name_and_keeps_only_staffed_rows_in_window():
    rows = _parse([
        HEADER,
        _row(),
        _row(ba=""),                       # nobody on it
        _row(day="Oct 2"),                 # weeks ago
        _row(day="not a date"),
        _row(day="11/20", store="Marathon", ba="Kayla Buis"),
    ])
    assert [(r.row_number, r.ba_name, r.day) for r in rows] == [
        (2, "Lashanda Beckwith", date(2026, 11, 14)),
        (6, "Kayla Buis", date(2026, 11, 20)),
    ]
    first = rows[0]
    assert (first.store_name, first.start_text, first.end_text) == (
        "Plano Country Store", "11a", "2p",
    )
    assert (first.rate, first.ba_phone, first.tenant_ids) == ("$30", "12563358371", [7])


def test_a_brand_column_routes_the_row_and_an_unknown_brand_drops_it():
    def brands(cell):
        if cell == "Famus Energy":
            return [3, 4]
        raise ValueError("unknown brand")

    values = [HEADER, _row(), _row()]
    values[2][7] = "Mystery Co"
    rows = parse_rows(values, sheet_id=SHEET, today=TODAY, fixed_tenant_id=None,
                      brand_tenant_ids=brands)
    assert [r.tenant_ids for r in rows] == [[3, 4]]


CALENDAR_HEADER = [
    "BRAND", "EVENT", "Date", "Time Scheduled", "Account Address",
    "Parking info / Notes from Store Manager", "Confirmed BA Name ", "BA Rate",
    "Email", "Cell Phone #",
]


def test_calendar_tabs_extra_ambassador_lines_belong_to_the_event_above():
    rows = _parse([
        CALENDAR_HEADER,
        ["Pivot", "Tailgate sampling", "11/14", "1p - 7:30p",
         "Hard Rock Stadium / 347 Don Shula Dr Suite 102, Miami Gardens, FL 33056",
         "", "Yoselin Gutierrez", "28/HR", "", "9545169300"],
        ["", "", "", "", "", "", "Paola Iturbide", "$150", "", "7244691476"],
        # A new event starts a new group: this line must not inherit above.
        ["Pivot", "Campus drop", "Sept 26", "9a - 11a", "1 Old St", "", "Old Timer", "", "", ""],
        ["", "", "", "", "", "", "", "", "", ""],
    ])
    assert [(r.ba_name, r.day, r.store_name, r.start_text) for r in rows] == [
        ("Yoselin Gutierrez", date(2026, 11, 14), "Tailgate sampling", "1p - 7:30p"),
        ("Paola Iturbide", date(2026, 11, 14), "Tailgate sampling", "1p - 7:30p"),
    ]
    assert rows[1].address.startswith("Hard Rock Stadium")


def test_a_house_number_is_found_wherever_it_sits_in_the_address():
    from events.sheet_staffing import _street_number

    assert _street_number("Hard Rock Stadium / 347 Don Shula Dr Suite 102, Miami Gardens, FL 33056") == "347"
    assert _street_number("Total Wine, 7400 Carson Blvd, Long Beach, CA 90808") == "7400"
    assert _street_number("3205 Plano Rd, Bowling Green, KY 42104") == "3205"
    assert _street_number("1315 N Elkhorn Rd") == "1315"
    assert _street_number("Golden Gate Golf Course") == ""
    assert _street_number("Suite #200, Miami FL 33056") == ""
    row = _parsed("Tailgate sampling", "Hard Rock Stadium / 347 Don Shula Dr Suite 102")
    stadium = _Event("Hard Rock Stadium", "347 Don Shula Drive, Miami Gardens, FL")
    assert match_event(row, [stadium]) == (stadium, MATCH_MATCHED)

    # Spark has only the venue's name; the sheet has "Venue, street address".
    row = _parsed("Francesca1993 Dry Sampling", "Total Wine, 7400 Carson Blvd, Long Beach, CA 90808")
    carson = _Event("11/13/2026 - Total Wine (Carson Blvd)", "Total Wine")
    other = _Event("11/13/2026 - Bevmo (Main St)", "Bevmo")
    assert match_event(row, [carson, other]) == (carson, MATCH_MATCHED)
    # Two Total Wine events that day: say so, don't pick one.
    second = _Event("11/13/2026 - Total Wine (Bellflower)", "Total Wine")
    assert match_event(row, [carson, second]) == (None, MATCH_AMBIGUOUS)


class _Event:
    def __init__(self, name, address):
        self.name, self.address = name, address


def _parsed(store, address):
    return ParsedRow(2, TODAY, "11a", "2p", store, address, "A B", "", "", "", [1])


def test_a_row_matches_on_the_place_not_just_the_day():
    plano = _Event("Plano Country Store · Retail Sampling", "3205 Plano Rd, Bowling Green")
    other = _Event("Marathon", "1315 Elkhorn Rd, Campbellsville")
    row = _parsed("Plano Country Store", "3205 Plano Rd, Bowling Green, KY")
    assert match_event(row, [plano, other]) == (plano, MATCH_MATCHED)
    # Same street number is enough when the names differ.
    renamed = _Event("Bowling Green demo", "3205 Plano Road")
    assert match_event(row, [renamed, other]) == (renamed, MATCH_MATCHED)
    assert match_event(row, [other]) == (None, MATCH_NO_EVENT)
    assert match_event(row, [plano, renamed]) == (None, MATCH_AMBIGUOUS)


def test_names_compare_without_case_or_punctuation():
    assert name_key("Viri  Ortega") == name_key("viri ortega")
    assert name_key("O'Neil, Sam") != name_key("Sam O'Neil")


class _FakeSheets:
    """Just enough of the Sheets client: values().get(...).execute()."""

    def __init__(self, values):
        self._values = values

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, **kwargs):
        return self

    def execute(self):
        return {"values": self._values}


@pytest.mark.django_db(transaction=True)
class TestSheetStaffingSync(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db, monkeypatch):
        self.tenant = self.create_tenant(name="Famus Energy")
        etype = self.create_event_type("Retail Sampling", self.tenant)
        self.event = self.create_event(
            name="Plano Country Store",
            tenant=self.tenant,
            address="3205 Plano Rd, Bowling Green, KY 42104",
            event_type=etype,
            date=datetime(2026, 11, 14, 17, 0, tzinfo=dt_timezone.utc),
        )
        source = Source(
            key="test", label="Test Schedule", sheet_id=SHEET,
            tab_title="Schedule", tab_gid=0, tenant_id=self.tenant.id,
        )
        monkeypatch.setattr("events.sheet_staffing._sources", lambda svc: [source])

    def _sync(self, values, **kwargs):
        return sync_sheet_staffing(svc=_FakeSheets(values), today=TODAY, **kwargs)

    def test_mirrors_rows_follows_edits_and_drops_rows_the_sheet_dropped(self):
        result = self._sync([HEADER, _row(), _row(store="Marathon", address="1315 Elkhorn Rd")])
        assert (result["rows"], result["matched"], result["no_event"]) == (2, 1, 1)
        matched = SheetShift.objects.get(row_number=2)
        assert (matched.event_id, matched.match, matched.ba_name) == (
            self.event.id, "matched", "Lashanda Beckwith",
        )
        assert SheetShift.objects.get(row_number=3).event_id is None

        # The sheet swaps the ambassador and clears the second row.
        self._sync([HEADER, _row(ba="Kayla Buis"), _row(ba="")])
        assert list(SheetShift.objects.values_list("row_number", "ba_name")) == [
            (2, "Kayla Buis")
        ]

    def test_dry_run_stores_nothing_and_never_touches_bookings(self):
        from ambassadors.models import AmbassadorEvent

        result = self._sync([HEADER, _row()], dry_run=True)
        assert result["matched"] == 1 and result["dry_run"] is True
        assert not SheetShift.objects.exists()
        self._sync([HEADER, _row()])
        assert not AmbassadorEvent.objects.exists()


SHEET_QUERY = """
    query SheetShifts($date: String, $tenantId: ID) {
      sheetShifts(date: $date, tenantId: $tenantId) {
        day storeName baName brandName sourceLabel sheetUrl match
        eventUuid bookedNames agrees startText endText
      }
    }
"""


@pytest.mark.django_db(transaction=True)
class TestSheetShiftsQuery:
    """Through the real schema: staff see the sheet beside Spark's booking;
    a brand user sees nothing."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from ambassadors.models import AmbassadorEvent
        from ambassadors.tests.base import AmbassadorsGraphQLTestCase

        from config.schema_client import schema_clients

        self.h = AmbassadorsGraphQLTestCase()
        self.h.schema = schema_clients
        self.h.endpoint_path = "/api/v1/graphql/clients"
        self.h.system_user = self.h.get_system_user()
        roles = self.h.setup_default_roles()
        self.tenant = self.h.create_tenant(name="Famus Energy")
        self.admin = self.h.create_user(
            username="admin-sheet@test.com", email="admin-sheet@test.com",
            role=roles["spark_admin"],
        )
        self.client_user = self.h.create_user(
            username="client-sheet@test.com", email="client-sheet@test.com",
            role=roles["client"],
        )
        ba_user = self.h.create_user(
            username="ba-sheet@test.com", email="ba-sheet@test.com",
            role=roles["ambassador"], first_name="Kayla", last_name="Buis",
        )
        ba = self.h.create_ambassador(ba_user)
        self.event = self.h.create_event(
            name="Plano Country Store", tenant=self.tenant,
            date=datetime(2026, 11, 14, 17, 0, tzinfo=dt_timezone.utc),
            start_time=None, end_time=None,
        )
        AmbassadorEvent.objects.create(
            ambassador=ba, event=self.event, tenant=self.tenant,
            is_approved=True, created_by=self.admin,
        )
        common = dict(tenant=self.tenant, sheet_id=SHEET, tab_gid=0, day=TODAY,
                      source_label="Test Schedule", store_name="Plano Country Store")
        SheetShift.objects.create(row_number=2, ba_name="kayla  buis", event=self.event,
                                  match="matched", **common)
        SheetShift.objects.create(row_number=3, ba_name="Someone Else", event=self.event,
                                  match="matched", **common)
        SheetShift.objects.create(row_number=4, ba_name="No Event", match="no_event",
                                  **common)

    async def _rows(self, user):
        result = await self.h._execute_query_authenticated(
            SHEET_QUERY, {"date": "2026-11-14", "tenantId": None}, user,
            self.h.endpoint_path,
        )
        assert result.errors is None, result.errors
        return result.data["sheetShifts"]

    @pytest.mark.asyncio
    async def test_staff_see_agreement_per_row(self):
        rows = {r["baName"]: r for r in await self._rows(self.admin)}
        assert rows["kayla  buis"]["agrees"] is True
        assert rows["kayla  buis"]["bookedNames"] == ["Kayla Buis"]
        assert rows["kayla  buis"]["eventUuid"] == str(self.event.uuid)
        assert rows["Someone Else"]["agrees"] is False
        assert rows["No Event"]["agrees"] is None
        assert rows["No Event"]["eventUuid"] is None
        assert rows["No Event"]["sheetUrl"].endswith("#gid=0&range=A4")

    @pytest.mark.asyncio
    async def test_brand_users_get_nothing(self):
        assert await self._rows(self.client_user) == []
