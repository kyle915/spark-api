"""In-memory stand-in for the googleapiclient Sheets v4 service.

Supports exactly the calls ``timeclock.sheets`` makes: spreadsheet
get/create/batchUpdate (addSheet, deleteDimension) and values
get/update/append over A1 ranges on named tabs.
"""

from __future__ import annotations

import re

_CELL = re.compile(r"^([A-Z]+)?(\d+)?$")


def _col_num(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n


def _col_letters(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


class _Call:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class FakeSheets:
    def __init__(self):
        self.tabs: dict[str, dict[str, list[list[str]]]] = {}
        self.gids: dict[str, dict[str, int]] = {}
        self.calls: list[str] = []
        self.fail_with: Exception | None = None

    def add_spreadsheet(self, sheet_id: str, tabs=("Sheet1",)):
        self.tabs[sheet_id] = {t: [] for t in tabs}
        self.gids[sheet_id] = {t: i for i, t in enumerate(tabs)}

    def grid(self, sheet_id: str, tab: str) -> list[list[str]]:
        return self.tabs[sheet_id][tab]

    def spreadsheets(self):
        return self

    def values(self):
        return _Values(self)

    def _guard(self):
        if self.fail_with is not None:
            raise self.fail_with

    def get(self, spreadsheetId, fields=None):
        def run():
            self._guard()
            return {
                "sheets": [
                    {"properties": {"title": t, "sheetId": g}}
                    for t, g in self.gids[spreadsheetId].items()
                ]
            }

        return _Call(run)

    def create(self, body, fields=None):
        def run():
            self._guard()
            sheet_id = f"created{len(self.tabs) + 1}"
            self.add_spreadsheet(sheet_id, [s["properties"]["title"] for s in body["sheets"]])
            return {"spreadsheetId": sheet_id}

        return _Call(run)

    def batchUpdate(self, spreadsheetId, body):
        def run():
            self._guard()
            replies = []
            for req in body["requests"]:
                if "addSheet" in req:
                    title = req["addSheet"]["properties"]["title"]
                    gid = 100 + len(self.gids[spreadsheetId])
                    self.tabs[spreadsheetId][title] = []
                    self.gids[spreadsheetId][title] = gid
                    replies.append({"addSheet": {"properties": {"sheetId": gid, "title": title}}})
                elif "deleteDimension" in req:
                    rng = req["deleteDimension"]["range"]
                    tab = next(t for t, g in self.gids[spreadsheetId].items() if g == rng["sheetId"])
                    del self.tabs[spreadsheetId][tab][rng["startIndex"] : rng["endIndex"]]
                    replies.append({})
            self.calls.append("batchUpdate")
            return {"replies": replies}

        return _Call(run)


class _Values:
    def __init__(self, fake: FakeSheets):
        self.fake = fake

    def _parse(self, sheet_id, rng):
        tab, _, a1 = rng.rpartition("!")
        tab = tab.strip("'")
        start, _, end = a1.partition(":")
        m1 = _CELL.match(start)
        m2 = _CELL.match(end) if end else m1
        c1 = _col_num(m1.group(1) or "A")
        r1 = int(m1.group(2) or 1)
        c2 = _col_num(m2.group(1) or m1.group(1) or "A")
        r2 = int(m2.group(2)) if m2.group(2) else None
        return self.fake.tabs[sheet_id][tab], c1, r1, c2, r2, tab

    def get(self, spreadsheetId, range, **_):
        def run():
            self.fake._guard()
            grid, c1, r1, c2, r2, _tab = self._parse(spreadsheetId, range)
            last = r2 if r2 is not None else len(grid)
            out = []
            for r in range_(r1, last):
                row = grid[r - 1] if r - 1 < len(grid) else []
                out.append(row[c1 - 1 : c2])
            while out and not any(out[-1]):
                out.pop()
            return {"values": out} if out else {}

        return _Call(run)

    def update(self, spreadsheetId, range, valueInputOption, body):
        def run():
            self.fake._guard()
            grid, c1, r1, _c2, _r2, _tab = self._parse(spreadsheetId, range)
            for offset, values in enumerate(body["values"]):
                r = r1 + offset
                while len(grid) < r:
                    grid.append([])
                row = grid[r - 1]
                while len(row) < c1 - 1 + len(values):
                    row.append("")
                row[c1 - 1 : c1 - 1 + len(values)] = [v if v is not None else row[c1 - 1 + i] for i, v in enumerate(values)]
            self.fake.calls.append("update")
            return {}

        return _Call(run)

    def append(self, spreadsheetId, range, valueInputOption, insertDataOption, body):
        def run():
            self.fake._guard()
            grid, _c1, _r1, _c2, _r2, tab = self._parse(spreadsheetId, range)
            for values in body["values"]:
                grid.append(list(values))
            n = len(grid)
            self.fake.calls.append("append")
            width = _col_letters(len(body["values"][0]))
            return {"updates": {"updatedRange": f"'{tab}'!A{n}:{width}{n}"}}

        return _Call(run)


def range_(a: int, b: int):
    return range(a, b + 1)
