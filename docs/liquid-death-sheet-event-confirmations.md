# Liquid Death Sheet → Event Confirmation / Cancel

Automates BA event confirmations (and cancellations) from the Liquid Death
retail market-schedules Google Sheet, reusing Spark’s existing Event
Confirmation mailer (email-only — no Event / roster create).

**Sheet:** [External] Liquid Death (Retail) Market Schedules  
https://docs.google.com/spreadsheets/d/1P1k5bpGuce_qAeh1qg9kNI4QjzRYLZhHTDm2vnQQKi0/edit?gid=0  
**ID:** `1P1k5bpGuce_qAeh1qg9kNI4QjzRYLZhHTDm2vnQQKi0` (gid **0**)  
**Tenant:** `liquid-death` (alias `ighn-liquid-death`) · walk-up `/checkin/LD-TNBJ8K` (do not remint)  
**Endpoint:** `POST https://spark-api-new-490085168610.us-central1.run.app/internal/liquid-death-sheet-event-confirmation`  
**Auth:** `X-Cron-Secret: <INTERNAL_CRON_SECRET>`

This is the Torch sheet-confirmation pattern for LD Retail Samplings only
(LD also has Event / Product Seeding — this sheet automation always labels
**Retail Sampling**).

---

## Columns

### Existing (read) — gid 0, mapped 2026-09-18

| Col | Header | Use |
|-----|--------|-----|
| A | State | TZ fallback (may be blank; geocode Address first) |
| B | Date | Day-of-week text (e.g. Saturday) — ignored for send |
| C | Date | Local store calendar date (Apps Script keeps this when both are keyed `Date`) |
| D | Store Name | Email |
| E / F | Start / End Time | `10a` / `4p` style |
| G | Address | Geocode → IANA TZ for reminders |
| I | SKUs to sample | Products |
| L | BA Name | Required |
| R | Email | BA recipient |
| S | Cell Phone # | |
| T | Shipped? | (trailing space in header on sheet) |
| **U** | **SEND** | **Ops flag — not the confirmation trigger** |
| V–AA | Recap Link … Kyle Check | Untouched |

Event type is always **Retail Sampling**. Do not read it from a sheet column.

### Confirmation columns (header name, not letter)

Apps Script and the API find these by header name. After
`ensureConfirmationColumns` (append-only past **Kyle Check** / col AA) they
are expected at **AB–AI**:

| Col | Header | Type | Role |
|-----|--------|------|------|
| **AB** | **Event Confirmation Sent?** | text | Human-readable Sent / Cancelled stamp |
| **AC** | **Send Confirmation** | checkbox | Trigger confirmation email |
| **AD** | **Cancel Confirmation** | checkbox | Trigger cancel email (if previously Sent) |
| **AE** | **Force Resend** | checkbox | Sticky. BA swaps only. If left checked, the next Send double-emails. |
| AF | Confirmation Status | text | `Queued` → `Sent` → `Cancelled` / `Error` |
| AG | Confirmation Sent At | text | Local stamp when Sent |
| AH | Confirmation Error | text | Geocode fallback note or error |
| AI | Spark Confirmation UUID | text | Spark row id (cancel + audit) |
| *(appended)* | **Resend Confirmation** | checkbox | One-shot. Check to email the BA on that row again. Script clears the box and posts `resend: true`. Header-name lookup only — do not hardcode the letter. |

`ensureConfirmationColumns` only appends a header that is missing, and it will
not write over a cell that already has a header. It never remints or renames
existing columns (including **SEND** at U).

---

## How to use

1. Fill BA Name + Email (+ date / store / times / address / SKUs).
2. Check **Send Confirmation** → BA gets the same staffing@ confirmation as the
   admin tab (training / resources from tenant `checkin_resources`, clock/recap
   → `https://client.igniteproductions.co/checkin/LD-TNBJ8K`). Reminders
   (24h / 3h) stay on by default.
3. **Unchecking Send does nothing** — it never cancels.
4. **Cancel:** check **Cancel Confirmation**. If status was Sent, BA gets
   “this sampling has been cancelled”; reminders stop; status → Cancelled.
5. **Swap BA:** Cancel → change BA Name + Email → check **Force Resend** +
   **Send Confirmation**. Uncheck Force Resend afterward.
6. **BA didn’t get the confirmation:** check **Resend Confirmation** only.
   That force-sends once (`resend: true`), then the box clears itself.
   Do not also check Force Resend.
7. **Tomorrow bulk:** menu **Spark Confirmations → Send confirmations for tomorrow**.

Tomorrow’s samplings are allowed — there is no “must be 2+ days out” gate.

---

## Apps Script install

1. Open the LD sheet → **Extensions → Apps Script**.
2. Paste [`scripts/liquid_death_sheet_event_confirmations.gs`](../scripts/liquid_death_sheet_event_confirmations.gs).
3. **Project Settings → Script properties:**
   - `SPARK_CRON_SECRET` = Cloud Run `INTERNAL_CRON_SECRET`
   - `SPARK_API_BASE` = `https://spark-api-new-490085168610.us-central1.run.app` (optional)
4. Run `ensureConfirmationColumns` (appends **Resend Confirmation** if it is missing; does not overwrite existing headers), then `installTriggers` if this is a first install. Re-pasting updates `onConfirmationEdit` — the trigger name does not change — but you must run `ensureConfirmationColumns` once so the new header exists. The script already installed on the live sheet does not update until you paste.
5. Reload the sheet; **Spark Confirmations** menu appears.

Who installs: Kyle or any Ignite ops Editor. The SA
`spark-api-new-sa@spark-479222.iam.gserviceaccount.com` needs **Editor** on the
sheet for status write-back (share if missing). Apps Script runs as the human
installer for `onEdit`.

### Claude Code / paste prompt (optional)

```
Open the Liquid Death retail sheet
https://docs.google.com/spreadsheets/d/1P1k5bpGuce_qAeh1qg9kNI4QjzRYLZhHTDm2vnQQKi0
→ Extensions → Apps Script. Replace Code.gs with the contents of
spark-api/scripts/liquid_death_sheet_event_confirmations.gs.
Set script property SPARK_CRON_SECRET to Cloud Run INTERNAL_CRON_SECRET.
Run ensureConfirmationColumns (appends header "Resend Confirmation"), then installTriggers if not already installed. Do not email real BAs.
```

---

## API contract

```json
{
  "action": "send",
  "rowNumber": 42,
  "sheetId": "1P1k5bpGuce_qAeh1qg9kNI4QjzRYLZhHTDm2vnQQKi0",
  "dryRun": true,
  "resend": false,
  "values": {
    "Date": "10/3/2026",
    "Start Time": "10a",
    "End Time": "4p",
    "Store Name": "Piggly Wiggly #277",
    "Address": "W189S7847 Racine Ave, Muskego, WI 53150, USA",
    "State": "WI",
    "SKUs to sample": "Doctor Death, Rootbeer Wrath",
    "BA Name": "Test BA",
    "Email": "kyle@igniteproductions.co",
    "Confirmation Status": "",
    "Force Resend": false
  }
}
```

`dryRun: true` validates mapping / TZ and returns what would send — **no email, no sheet stamp**.

`resend: true` (with `action: "send"`) force-sends this request once. The **Resend Confirmation** column is not a force flag; Apps Script clears it after the click.

---

## Test-send recommendation

1. Pick a **future** row and temporarily set Email to `kyle@igniteproductions.co`.
2. `curl` with `"dryRun": true` first; confirm `timezone` looks right.
3. Re-run with `"dryRun": false` once; confirm staffing@ mail + sheet Status = Sent.
4. Check Cancel on that same row; confirm cancel mail + Cancelled.
5. Restore the real BA email before live ops use.

Do not spam real BAs while wiring the script.

---

## Status machine

Same as Torch (`docs/torch-sheet-event-confirmations.md`):

| From | Action | To |
|------|--------|-----|
| (empty) | Send | Queued → Sent (or Error) |
| **Queued** (mail already left, stamp failed) | Send (no Force) | **Sent** stamped, **no new email** (`alreadySent: true`) when an `EventConfirmation` + booked send exists |
| Queued (no confirmation found) | Send (no Force) | **409** — do not Force Resend |
| Sent | Send (no Force) | refused (409) |
| Sent | Force + Send | new confirmation + email (sticky — uncheck Force Resend after) |
| Sent | **Resend Confirmation** | new confirmation + email once (`resend: true`); checkbox clears |
| Sent | Cancel | Cancelled (+ cancel email) |
| (never Sent) | Cancel | Cancelled, **no** email |
| Cancelled | Send (no Force) | refused |
| Any | Uncheck Send | no-op |

---

## Kyle-only checklist

1. Share the sheet with `spark-api-new-sa@spark-479222.iam.gserviceaccount.com` as **Editor** (if not already).
2. Paste Apps Script + set `SPARK_CRON_SECRET`.
3. Run `ensureConfirmationColumns` + `installTriggers`.
4. Dry-run one row; optional live test to kyle@ only.
