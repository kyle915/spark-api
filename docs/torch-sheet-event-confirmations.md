# Torch Sheet → Event Confirmation / Cancel

Automates BA event confirmations (and cancellations) from the Torch retail
schedule Google Sheet, reusing Spark’s existing Event Confirmation mailer
(email-only — no Event / roster create).

**Sheet:** [External // Torch Beverage Retail Demos](https://docs.google.com/spreadsheets/d/1kAvZhy2B9HoeSS-qjKXve8JWUV1oBxDqnhs1-7dQUYw/edit)  
**ID:** `1kAvZhy2B9HoeSS-qjKXve8JWUV1oBxDqnhs1-7dQUYw`  
**Tenant:** `keee-torch-thc` · walk-up `/checkin/TH-2HRV3D` (do not remint)  
**Endpoint:** `POST https://spark-api-new-490085168610.us-central1.run.app/internal/torch-sheet-event-confirmation`  
**Auth:** `X-Cron-Secret: <INTERNAL_CRON_SECRET>`

---

## Columns

### Existing (read)

| Col | Header | Use |
|-----|--------|-----|
| A | State | TZ fallback |
| C | Date | Local store date (tomorrow OK) |
| D | Store Name | Email |
| E / F | Start / End Time | `1p` / `4p` style |
| G | Address | Geocode → IANA TZ for reminders |
| N | SKUs to sample | Products |
| **O** | **Event Confirmation Sent?** | **Status stamp only** (not the trigger, no checkbox) |
| S | BA Name | Required |
| V | Email | BA recipient |
| W | Cell Phone # | |
| X | Shipped? | |

Event type is always **Retail Sampling**. Do not read it from a sheet column.

### Confirmation columns (header name, not letter)

Apps Script and the API find these by header name. On the live retail tab
(gid 0, read 2026-09-17) they are **Y–AE**, not AT–AZ. `ensureConfirmationColumns`
only appends a header that is missing, and it will not write over a cell that
already has a header.

| Col | Header | Type | Role |
|-----|--------|------|------|
| **Y** | **Send Confirmation** | checkbox | Trigger confirmation email |
| **Z** | **Cancel Confirmation** | checkbox | Trigger cancel email (if previously Sent) |
| **AA** | **Force Resend** | checkbox | Allow Send again after Sent / Cancelled |
| AB | Confirmation Status | text | `Queued` → `Sent` → `Cancelled` / `Error` |
| AC | Confirmation Sent At | text | Local stamp when Sent |
| AD | Confirmation Error | text | Geocode fallback note or error |
| AE | Spark Confirmation UUID | text | Spark row id (cancel + audit) |

The pre-install map put **Spark Request UUID** at Y and **Request Type** at AB.
Those headers are **not** on this tab anymore, and they were **not shifted**
right of AE (the tab ends at AE). If they were live here before install, they
were overwritten. Other tabs in the workbook still have Spark Request UUID /
Request Type, but at different letters (those tabs do not have column O).

---

## How to use

1. Fill BA Name + Email (+ date / store / times / address / SKUs).
2. Check **Send Confirmation** → BA gets the same staffing@ confirmation as the admin tab (training from tenant `checkin_resources`, clock/recap → `client…/checkin/TH-2HRV3D`). Reminders (24h / 3h) stay on.
3. **Unchecking Send does nothing** — it never cancels.
4. **Cancel:** check **Cancel Confirmation**. If status was Sent, BA gets “this sampling has been cancelled”; reminders stop; status → Cancelled.
5. **Swap BA:** Cancel (or cancel checkbox) → change BA Name + Email → check **Force Resend** + **Send Confirmation**.
6. **Resend same BA:** check Force Resend + Send.
7. **Tomorrow bulk:** menu **Spark Confirmations → Send confirmations for tomorrow** (checks Send on eligible tomorrow rows missing Sent).

Tomorrow’s samplings are allowed — there is no “must be 2+ days out” gate.

---

## Apps Script install

1. Open the Torch sheet → **Extensions → Apps Script**.
2. Paste [`scripts/torch_sheet_event_confirmations.gs`](../scripts/torch_sheet_event_confirmations.gs).
3. **Project Settings → Script properties:**
   - `SPARK_CRON_SECRET` = Cloud Run `INTERNAL_CRON_SECRET`
   - `SPARK_API_BASE` = `https://spark-api-new-490085168610.us-central1.run.app` (optional)
4. Run `ensureConfirmationColumns`, then `installTriggers` (authorize the installing Google account — any Editor on the sheet).
5. Reload the sheet; **Spark Confirmations** menu appears.

Who installs: Kyle or any Ignite ops Editor. The SA
`spark-api-new-sa@spark-479222.iam.gserviceaccount.com` already has Editor for
status write-back; Apps Script runs as the human installer for `onEdit`.

---

## API contract

```json
{
  "action": "send",
  "rowNumber": 42,
  "sheetId": "1kAvZhy2B9HoeSS-qjKXve8JWUV1oBxDqnhs1-7dQUYw",
  "dryRun": true,
  "values": {
    "Date": "Sep 18, 2026",
    "Start Time": "1p",
    "End Time": "4p",
    "Store Name": "Binny's",
    "Address": "123 Main, Chicago, IL",
    "State": "IL",
    "SKUs to sample": "Torch 10mg",
    "BA Name": "Test BA",
    "Email": "you@igniteproductions.co",
    "Confirmation Status": "",
    "Force Resend": false
  }
}
```

`dryRun: true` validates mapping / TZ and returns what would send — **no email, no sheet stamp**. Use this for the first test.

---

## Test-send recommendation

1. Pick a **future** row (or tomorrow) and temporarily set Email to an Ignite inbox.
2. `curl` with `"dryRun": true` first; confirm `timezone` looks right.
3. Re-run with `"dryRun": false` once; confirm staffing@ mail + sheet Status = Sent.
4. Check Cancel on that same row; confirm cancel mail + Cancelled.
5. Restore the real BA email before live ops use.

Do not spam real BAs while wiring the script.

---

## Status machine

| From | Action | To |
|------|--------|-----|
| (empty) | Send | Queued → Sent (or Error) |
| Sent | Send (no Force) | refused |
| Sent | Force + Send | new confirmation + email |
| Sent | Cancel | Cancelled (+ cancel email) |
| (never Sent) | Cancel | Cancelled, **no** email |
| Cancelled | Send (no Force) | refused |
| Any | Uncheck Send | no-op |
