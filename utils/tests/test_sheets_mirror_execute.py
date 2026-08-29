"""Sheets mirror execute/retry and paginated LD key-column reads."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httplib2
from googleapiclient.errors import HttpError

from utils.sheets_mirror import (
    _execute_sheets,
    _ld_existing_rows,
    _ld_key_col,
)


def _http_error(status: int) -> HttpError:
    resp = httplib2.Response({"status": str(status)})
    return HttpError(resp, b"error")


def test_execute_sheets_retries_transient_http_error():
    req = MagicMock()
    req.execute.side_effect = [_http_error(503), {"values": [["ok"]]}]
    with patch("utils.sheets_mirror.time.sleep"):
        out = _execute_sheets(req, op="test read")
    assert out == {"values": [["ok"]]}
    assert req.execute.call_count == 2


def test_execute_sheets_retries_timeout():
    req = MagicMock()
    req.execute.side_effect = [TimeoutError(), {"values": [["ok"]]}]
    with patch("utils.sheets_mirror.time.sleep"):
        out = _execute_sheets(req, op="test read")
    assert out == {"values": [["ok"]]}
    assert req.execute.call_count == 2


def test_ld_existing_rows_paginates_until_empty_batch():
    svc = MagicMock()
    values = svc.spreadsheets.return_value.values.return_value
    col = _ld_key_col()
    req1 = MagicMock()
    req2 = MagicMock()
    values.get.side_effect = [req1, req2]
    req1.execute.return_value = {"values": [["uuid-a"], ["uuid-b"]]}
    req2.execute.return_value = {"values": []}

    with patch("utils.sheets_mirror._SHEETS_READ_BATCH_ROWS", 2):
        out = _ld_existing_rows(svc, "sid123", "MASTER_Tracker")

    assert out == {"uuid-a": 2, "uuid-b": 3}
    assert values.get.call_count == 2
    first_range = values.get.call_args_list[0].kwargs["range"]
    assert first_range == f"'MASTER_Tracker'!{col}2:{col}3"


def test_ld_existing_rows_returns_partial_on_timeout():
    svc = MagicMock()
    values = svc.spreadsheets.return_value.values.return_value
    req = MagicMock()
    values.get.return_value = req
    req.execute.side_effect = TimeoutError()

    with patch("utils.sheets_mirror._execute_sheets", side_effect=TimeoutError()):
        out = _ld_existing_rows(svc, "sid123", None)

    assert out == {}
