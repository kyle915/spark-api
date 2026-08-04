#!/usr/bin/env python3
"""Turn a field-level recap-export JSON payload into XLSX + CSVs.

Runs with NOTHING but Python + openpyxl — no Django, no database, no
credentials — so the `recap-field-export` GitHub Action can render the client
workbook from the JSON the cron endpoint returned:

    python3 scripts/build_recap_workbook.py response.json girl-beer-recaps

The input may be either the raw payload (``{"columns": [...], "rows": [...]}``)
or the endpoint envelope (``{"ok": true, "data": {...}}``) — both are accepted,
because the artifact from the workflow is the envelope and a local
``--json`` run of the management command is the bare payload.

Writes ``<stem>.xlsx``, ``<stem>.csv`` and ``<stem>-files.csv``, then prints
what it wrote. Rendering itself lives in :mod:`utils.workbook`, shared with the
management command so the two can't drift.
"""
from __future__ import annotations

import json
import os
import sys

# Run from anywhere: put the repo root on sys.path so `utils.workbook` imports.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.workbook import write_outputs  # noqa: E402


def _unwrap(blob: dict) -> dict:
    """Accept either the bare payload or the endpoint's {"ok","data"} envelope."""
    if "columns" in blob and "rows" in blob:
        return blob
    for key in ("data", "payload", "report"):
        inner = blob.get(key)
        if isinstance(inner, dict) and "columns" in inner:
            return inner
    raise SystemExit(
        "Input JSON has no recap-export payload — expected top-level "
        '"columns"/"rows", or those nested under "data".'
    )


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        print("usage: build_recap_workbook.py <input.json> [output-stem]")
        return 2
    src = argv[1]
    stem = argv[2] if len(argv) > 2 else os.path.splitext(src)[0]

    with open(src, encoding="utf-8") as fh:
        payload = _unwrap(json.load(fh))

    written = write_outputs(payload, stem)
    meta = payload.get("meta") or {}
    print(
        f"rows={meta.get('row_count', 0)} "
        f"columns={meta.get('column_count', 0)} "
        f"files={meta.get('file_count', 0)}"
    )
    for label, path in written.items():
        size = os.path.getsize(path)
        print(f"  {label:<10} {path}  ({size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
