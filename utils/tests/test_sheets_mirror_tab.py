"""The Master-Tracker mirror's opt-in tab targeting.

When a tenant sets master_tracker_tab_name the mirror must qualify its ranges
to that worksheet; when blank it must stay unqualified (first-worksheet — the
unchanged behavior every existing tenant, e.g. Girl Beer, relies on). And the
header ensure must only touch the first 15 columns so a tenant's manual
columns (BA Notes, Contract Sent, …) past "Spark Link" are never clobbered.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

from utils.sheets_mirror import (
    HEADER,
    _append_or_insert_new,
    _col_letter,
    _date_descending_insert_index,
    _ensure_header,
    _parse_sheet_date,
    _qualify,
    _row_date,
)


def test_qualify_unqualified_when_no_tab():
    assert _qualify(None, "A2:O") == "A2:O"
    assert _qualify("", "A2:O") == "A2:O"


def test_qualify_prefixes_tab():
    assert _qualify("Master Tracker", "A2:O") == "'Master Tracker'!A2:O"


def test_header_is_15_columns_bounded_to_O():
    # Guards the manual-columns invariant: writes never reach column P.
    assert len(HEADER) == 15
    assert _col_letter(len(HEADER)) == "O"


def _mock_svc(existing_first_row):
    svc = MagicMock()
    values = svc.spreadsheets.return_value.values.return_value
    values.get.return_value.execute.return_value = (
        {"values": [existing_first_row]} if existing_first_row is not None else {"values": []}
    )
    return svc


def test_ensure_header_skips_when_first15_match():
    # Live tracker: 15 mirror cols + manual cols → no write (manual cols safe).
    svc = _mock_svc(HEADER + ["BA Notes", "Contract Sent"])
    _ensure_header(svc, "sid", "Master Tracker")
    svc.spreadsheets.return_value.values.return_value.update.assert_not_called()


def test_ensure_header_writes_only_first15_to_named_tab():
    svc = _mock_svc(["something else"])
    _ensure_header(svc, "sid", "Master Tracker")
    update = svc.spreadsheets.return_value.values.return_value.update
    update.assert_called_once()
    assert update.call_args.kwargs["range"] == "'Master Tracker'!A1:O1"
    assert update.call_args.kwargs["body"]["values"] == [HEADER]


def test_ensure_header_unqualified_range_when_no_tab():
    svc = _mock_svc(["something else"])
    _ensure_header(svc, "sid")  # tab=None → first worksheet, bare range
    update = svc.spreadsheets.return_value.values.return_value.update
    assert update.call_args.kwargs["range"] == "A1:O1"


# --- date-positioned new-row insertion (master_tracker_insert_by_date) ---


def test_parse_sheet_date_formats():
    assert _parse_sheet_date("7/12/2026") == date(2026, 7, 12)
    assert _parse_sheet_date("07/05/2026") == date(2026, 7, 5)
    assert _parse_sheet_date("2026-07-12") == date(2026, 7, 12)
    assert _parse_sheet_date("") is None
    assert _parse_sheet_date(None) is None
    assert _parse_sheet_date("All below are pending") is None


def _mock_date_column(col_c_values):
    """svc whose values().get() returns B:C rows with the date in col C
    (col B blank) — the mirror-row shape (B = Status, C = Date)."""
    svc = MagicMock()
    svc.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
        "values": [["", v] for v in col_c_values]
    }
    return svc


# A live tracker sorted DESCENDING (newest first), with a blank divider row.
_DESC_DATES = ["7/15/2026", "7/12/2026", "", "7/01/2026", "6/20/2026"]


def test_row_date_reads_col_b_then_col_c():
    # Mirror row: B = Status, C = Date.
    assert _row_date(["Approved", "6/20/2026"]) == date(2026, 6, 20)
    # State-first manual row: B = weekday, C = date.
    assert _row_date(["Tuesday", "7/7/2026"]) == date(2026, 7, 7)
    # Day-first manual row: B = date, C = retailer.
    assert _row_date(["7/12/2026", "Walmart Supercenter"]) == date(2026, 7, 12)
    # Divider / unknown: neither parses.
    assert _row_date(["All below", ""]) is None
    assert _row_date([]) is None


def test_insert_index_middle_lands_above_first_older_date():
    svc = _mock_date_column(_DESC_DATES)
    # 7/05 is newer than 7/01 (row 5) → insert before row 5.
    assert _date_descending_insert_index(svc, "sid", "MASTER_Tracker", date(2026, 7, 5)) == 5


def test_insert_index_newest_goes_to_top():
    svc = _mock_date_column(_DESC_DATES)
    # Newer than every row → before the first data row (row 2 = top).
    assert _date_descending_insert_index(svc, "sid", "MASTER_Tracker", date(2026, 8, 1)) == 2


def test_insert_index_oldest_appends():
    svc = _mock_date_column(_DESC_DATES)
    # Older than every row → None → caller appends at the bottom.
    assert _date_descending_insert_index(svc, "sid", "MASTER_Tracker", date(2026, 6, 1)) is None


def test_insert_index_skips_blank_divider_rows():
    # New date falls exactly in the blank-row gap; the blank is skipped and the
    # next parseable older date (7/01 at row 5) is the anchor.
    svc = _mock_date_column(_DESC_DATES)
    assert _date_descending_insert_index(svc, "sid", "MASTER_Tracker", date(2026, 7, 6)) == 5


def test_insert_index_none_when_new_date_unparseable():
    svc = _mock_date_column(_DESC_DATES)
    assert _date_descending_insert_index(svc, "sid", "MASTER_Tracker", None) is None


def test_insert_index_honors_col_b_dates_in_manual_rows():
    # Hand-curated region: day-first rows carry the date in col B; a mirror row
    # below carries it in col C. Descending overall.
    svc = MagicMock()
    svc.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
        "values": [
            ["7/20/2026", "Walmart · 2026-07-20"],  # row 2 — date in B
            ["7/12/2026", "Vons · 2026-07-12"],      # row 3 — date in B
            ["Approved", "7/01/2026"],               # row 4 — mirror row, date in C
        ]
    }
    # 7/15 is newer than 7/12 (row 3) → insert before row 3.
    assert _date_descending_insert_index(svc, "sid", "MASTER_Tracker", date(2026, 7, 15)) == 3
    # 7/25 is newer than everything → insert before row 2 (top of schedule).
    assert _date_descending_insert_index(svc, "sid", "MASTER_Tracker", date(2026, 7, 25)) == 2
    # 6/01 is older than all → append.
    assert _date_descending_insert_index(svc, "sid", "MASTER_Tracker", date(2026, 6, 1)) is None


def _row_with_date(d):
    """A 15-col mirror row whose col C (index 2) is the date string."""
    row = ["uuid", "Approved", d] + [""] * 12
    return row


def test_append_or_insert_falls_back_to_append_when_flag_off():
    svc = MagicMock()
    tenant = SimpleNamespace(master_tracker_insert_by_date=False)
    _append_or_insert_new(svc, "sid", "MASTER_Tracker", tenant, _row_with_date("7/05/2026"), "O")
    values = svc.spreadsheets.return_value.values.return_value
    values.append.assert_called_once()
    values.update.assert_not_called()


def test_append_or_insert_appends_when_no_tab_even_if_flag_on():
    svc = MagicMock()
    tenant = SimpleNamespace(master_tracker_insert_by_date=True)
    _append_or_insert_new(svc, "sid", None, tenant, _row_with_date("7/05/2026"), "O")
    svc.spreadsheets.return_value.values.return_value.append.assert_called_once()


def test_append_or_insert_inserts_at_date_slot_when_enabled():
    svc = MagicMock()
    spreadsheets = svc.spreadsheets.return_value
    # _tab_gid → metadata with the named tab's sheetId.
    spreadsheets.get.return_value.execute.return_value = {
        "sheets": [{"properties": {"title": "MASTER_Tracker", "sheetId": 999, "index": 0}}]
    }
    # _date_descending_insert_index → B:C rows, date in col C (descending).
    spreadsheets.values.return_value.get.return_value.execute.return_value = {
        "values": [["", v] for v in _DESC_DATES]
    }
    tenant = SimpleNamespace(master_tracker_insert_by_date=True)
    _append_or_insert_new(svc, "sid", "MASTER_Tracker", tenant, _row_with_date("7/05/2026"), "O")

    # Inserted a blank row (batchUpdate insertDimension) then wrote the row.
    batch = spreadsheets.batchUpdate
    batch.assert_called_once()
    req = batch.call_args.kwargs["body"]["requests"][0]["insertDimension"]
    assert req["range"]["sheetId"] == 999
    assert req["range"]["startIndex"] == 4 and req["range"]["endIndex"] == 5  # row 5
    update = spreadsheets.values.return_value.update
    update.assert_called_once()
    assert update.call_args.kwargs["range"] == "'MASTER_Tracker'!A5:O5"
    # And it did NOT fall back to append.
    spreadsheets.values.return_value.append.assert_not_called()


# ── LD grid self-healing (ensure columns / blank-row debris / compensation) ──

def _meta_svc(cols, gid=77, title="MASTER_Tracker"):
    svc = MagicMock()
    svc.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [
            {"properties": {"title": title, "sheetId": gid, "index": 0,
                            "gridProperties": {"columnCount": cols}}}
        ]
    }
    return svc


def test_ld_ensure_grid_appends_missing_columns():
    from utils.sheets_mirror import _LD_KEY_COL_INDEX, _ld_ensure_grid

    svc = _meta_svc(cols=61)
    gid = _ld_ensure_grid(svc, "sid", "MASTER_Tracker")
    assert gid == 77
    body = svc.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]
    req = body["requests"][0]["appendDimension"]
    assert req["dimension"] == "COLUMNS"
    # 61 existing + appended == key column + one spare
    assert 61 + req["length"] == _LD_KEY_COL_INDEX + 2


def test_ld_ensure_grid_noops_when_wide_enough():
    from utils.sheets_mirror import _ld_ensure_grid

    svc = _meta_svc(cols=80)
    assert _ld_ensure_grid(svc, "sid", "MASTER_Tracker") == 77
    svc.spreadsheets.return_value.batchUpdate.assert_not_called()


def test_ld_remove_blank_rows_deletes_gap_but_not_tail():
    from utils.sheets_mirror import _ld_remove_blank_rows

    svc = MagicMock()
    # Rows 2-5 content, row 6 blank (the debris), rows 7-8 content, rest tail.
    data = [["NH", "Friday"]] * 4 + [[]] + [["NY"], ["TX"]]
    svc.spreadsheets.return_value.values.return_value.batchGet.return_value.execute.return_value = {
        "valueRanges": [{"values": data}, {"values": []}]
    }
    removed = _ld_remove_blank_rows(svc, "sid", "MASTER_Tracker", gid=77, last_row=40)
    assert removed == 1
    body = svc.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]
    rng = body["requests"][0]["deleteDimension"]["range"]
    # Row 6 → 0-based [5, 6); the blank tail rows 9-40 are NOT deleted.
    assert (rng["startIndex"], rng["endIndex"]) == (5, 6)
    assert len(body["requests"]) == 1


def test_ld_insert_dated_row_deletes_inserted_row_when_write_fails():
    from googleapiclient.errors import HttpError
    from utils.sheets_mirror import _ld_insert_dated_row

    svc = MagicMock()
    err = HttpError(resp=SimpleNamespace(status=400, reason="bad"), content=b"grid")
    svc.spreadsheets.return_value.values.return_value.batchUpdate.return_value.execute.side_effect = err
    try:
        _ld_insert_dated_row(svc, "sid", "MASTER_Tracker", gid=77, at_row=6,
                             row9=["NV"] * 9, uuid="u-1")
        assert False, "expected HttpError to propagate"
    except HttpError:
        pass
    # Two structural batchUpdates: the insert, then the compensating delete.
    calls = svc.spreadsheets.return_value.batchUpdate.call_args_list
    assert "insertDimension" in str(calls[0])
    assert "deleteDimension" in str(calls[1])
