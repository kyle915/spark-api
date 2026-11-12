# Long Drink (EVENT CALENDAR) Sheet → Event Confirmation / Cancel

Automates BA event confirmations (and cancellations) from the **Long Drink**
tab on the Ignite EVENT CALENDAR workbook, reusing Spark’s Event Confirmation
mailer (email-only — no Event / roster create).

**Sheet:** [EVENT CALENDAR](https://docs.google.com/spreadsheets/d/1eNIuGiOqkLIwgF7al5LkGvwYtmNHrKoct4qu1UnYHMc/edit?gid=1367810944)  
**ID:** `1eNIuGiOqkLIwgF7al5LkGvwYtmNHrKoct4qu1UnYHMc` (gid **1367810944**, tab `Long Drink`)  
**Tenant:** `mark-anthony-brands` · walk-up `/checkin/MAB-V696HM` (do not remint)  
**Endpoint:** `POST https://spark-api-new-490085168610.us-central1.run.app/internal/long-drink-sheet-event-confirmation`  
**Auth:** `X-Cron-Secret: <INTERNAL_CRON_SECRET>`

Clock / recap CTAs open the Mark Anthony Brands standing check-in (Retail
Sampling / On-Premise / Event Activation). Confirmation email also includes
the **Long Drink BA Sampling Guide**
(`client…/training/mark-anthony-brands/ba-training-guide.pdf`).

This workbook hosts dozens of brand tabs. Apps Script and
`ensureConfirmationColumns` touch **Long Drink only** — never Mark Anthony
Brands, Torch Field Events, or other tabs.

---

## Columns

### Existing (read) — gid 1367810944, mapped 2026-11-12

| Col | Header | Use |
|-----|--------|-----|
| A | City | Store name in the email (no Store Name column) |
| B | Address | Geocode → IANA TZ |
| C | State | TZ fallback |
| D | Store Number | Untouched |
| E | FSM | Untouched |
| F | Distributor | Untouched |
| G | Date | Local store calendar date |
| H | Time | Start clock (`4:00 PM`) |
| I–L | Recaps / Paid / Spend | Untouched |
| M | BA Name | Required |
| N | Phone number | Untouched |
| O | Email | BA recipient |
| P | Rate | Untouched |
| Q | Hours | Pay/rate column only — **not** used for the email window (always 3h) |

Event type is always **Retail Sampling**. There is no SKU column — the email
sends with no product lines (same as a blank Pivot SKU cell).

### Confirmation columns (header name, not letter)

Apps Script and the API find these by header name. After
`ensureConfirmationColumns` (append-only past **Hours** / col Q) they are
expected at **R–Y**:

| Col | Header | Type | Role |
|-----|--------|------|------|
| **R** | **Send Confirmation** | checkbox | Trigger confirmation email |
| **S** | **Cancel Confirmation** | checkbox | Trigger cancel email (if previously Sent) |
| **T** | **Force Resend** | checkbox | Sticky compatibility flag. Prefer Resend Confirmation |
| **U** | **Confirmation Status** | text | `Queued` → `Sent` → `Cancelled` / `Error` |
| V | Confirmation Sent At | text | Local stamp when Sent |
| W | Confirmation Error | text | Geocode fallback note or error |
| X | Spark Confirmation UUID | text | Spark row id (cancel + audit) |
| **Y** | **Resend Confirmation** | checkbox | One-shot. Clears itself and posts `resend: true` |

---

## How to use

1. Fill BA Name + Email (+ City / Date / Time / Address / State). End time is always Time + 3 hours.
2. Check **Send Confirmation** → BA gets staffing@ confirmation with MAB
   clock/recap (`…/checkin/MAB-V696HM`) and the Long Drink sampling guide.
   Reminders (24h / 3h) stay on by default.
3. **Unchecking Send does nothing** — it never cancels.
4. **Cancel:** check **Cancel Confirmation**.
5. **Swap BA:** Cancel → change BA Name + Email → check **Resend Confirmation**.
6. **BA didn’t get the confirmation:** check **Resend Confirmation** only.
7. **Tomorrow bulk:** menu **Spark Confirmations → Send confirmations for tomorrow**.

---

## Apps Script install

1. Open the EVENT CALENDAR sheet → **Extensions → Apps Script**.
2. Paste [`scripts/long_drink_sheet_event_confirmations.gs`](../scripts/long_drink_sheet_event_confirmations.gs).
3. **Project Settings → Script properties:**
   - `SPARK_CRON_SECRET` = Cloud Run `INTERNAL_CRON_SECRET`
   - `SPARK_API_BASE` = `https://spark-api-new-490085168610.us-central1.run.app` (optional)
4. Run `ensureConfirmationColumns` (Long Drink only), then `installTriggers`
   on a first install.
5. Reload the sheet; **Spark Confirmations** menu appears.

Who installs: Kyle or any Ignite ops Editor. The SA
`spark-api-new-sa@spark-479222.iam.gserviceaccount.com` needs **Editor** on the
sheet for status write-back.

### Claude Code / paste prompt (optional)

```
Open the EVENT CALENDAR sheet
https://docs.google.com/spreadsheets/d/1eNIuGiOqkLIwgF7al5LkGvwYtmNHrKoct4qu1UnYHMc
→ Extensions → Apps Script. Replace Code.gs with the contents of
spark-api/scripts/long_drink_sheet_event_confirmations.gs.
Set script property SPARK_CRON_SECRET to Cloud Run INTERNAL_CRON_SECRET.
Run ensureConfirmationColumns (Long Drink tab only), then installTriggers
if not already installed. Do not email real BAs.
```

---

## API contract

```json
{
  "action": "send",
  "rowNumber": 2,
  "sheetId": "1eNIuGiOqkLIwgF7al5LkGvwYtmNHrKoct4qu1UnYHMc",
  "dryRun": true,
  "values": {
    "City": "Rochester Hills",
    "Address": "1242 S Rochester Rd, Rochester Hills, MI 48307",
    "State": "MI",
    "Date": "11/6/2026",
    "Time": "4:00 PM",
    "BA Name": "Test BA",
    "Email": "you@igniteproductions.co"
  }
}
```

`dryRun: true` validates tenant, standing check-in `MAB-V696HM`, timezone,
and Time + fixed 3h → end clock. No email, no sheet stamp.

Subject shape: `Your Mark Anthony Brands Retail Sampling – Rochester Hills | 11/06/2026 | 4p - 7p`.
