"""The Master-Tracker mirror's opt-in tab targeting.

When a tenant sets master_tracker_tab_name the mirror must qualify its ranges
to that worksheet; when blank it must stay unqualified (first-worksheet — the
unchanged behavior every existing tenant, e.g. Girl Beer, relies on). And the
header ensure must only touch the first 15 columns so a tenant's manual
columns (BA Notes, Contract Sent, …) past "Spark Link" are never clobbered.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from utils.sheets_mirror import HEADER, _col_letter, _ensure_header, _qualify


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
