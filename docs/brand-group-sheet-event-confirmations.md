# Famus / Alcohol Armor / Wook Water and Pivot Energy Sheet → Event Confirmation

Automates BA event confirmations (and cancellations) from two retail
workbooks on one endpoint. Same mailer as Torch: `events/templates/emails/event_confirmation.html`
plus `emails/_confirmation_shift_actions.html` (the Torch clock, recap, and
BA-material buttons). Email only — no Event or roster create. Reminders
stay on (`send_reminders`): the day-before note and the few-hours note
("…scheduled for a {brand} retail sampling in a few hours").

**Endpoint:** `POST https://spark-api-new-490085168610.us-central1.run.app/internal/brand-group-sheet-event-confirmation`
**Auth:** `X-Cron-Secret: <INTERNAL_CRON_SECRET>`

`sheetId` picks the workbook. Omitting it stays the Famus group sheet.

## Famus / Alcohol Armor / Wook Water

**Sheet:** [External // Wook Water, Famus Energy, Alcohol Armor](https://docs.google.com/spreadsheets/d/1xnnzFb3s0mAV-6abyzmPzEX3wjtObD8Bv4qQsr8VB6A/edit)
**ID:** `1xnnzFb3s0mAV-6abyzmPzEX3wjtObD8Bv4qQsr8VB6A` (gid **0**, tab `Sheet1`)

Column **H (Brand)** chooses the tenant. The email includes only that brand's
standing check-in link, BA materials, and catalog SKUs. Several brands in H
(comma, slash, `&`, `+`, or `and`) repeat the same Torch button block once
per brand. A brand that is not named is not mentioned. An empty Brand cell
does not attach all three. Pivot is not a column H brand.

| Brand in H | Slug | Standing check-in (do not remint) |
|---|---|---|
| Famus Energy | `famus-energy` | `https://client.igniteproductions.co/checkin/FM-PAG4TX` |
| Alcohol Armor / Alcohol Armour | `alcohol-armor` | `https://client.igniteproductions.co/checkin/AA-RPP85E` |
| Wook Water / WookWater | `wook-water` | `https://client.igniteproductions.co/checkin/WW-36DJAJ` |

Famus Brand Guidelines and BA FAQ come from that tenant's `checkin_resources`
and appear only when Famus is in column H. Wook and Alcohol Armor do not get
those PDFs. SKU text is matched to the named tenants' Product catalog (for
example `OG Luxe` → `OG Luxe (Tropical Ice) 16oz`). A SKU that is not in
those catalogs is refused. Event type is always **Retail Sampling**.

## Pivot Energy Drink

**Sheet:** [External // Pivot Energy Retail Demos](https://docs.google.com/spreadsheets/d/1kMuUWZtaEJ0rbvsDzE-uI3FkBTIJdX_LbnEqYaAHG4I/edit)
**ID:** `1kMuUWZtaEJ0rbvsDzE-uI3FkBTIJdX_LbnEqYaAHG4I` (gid **0**, tab `Retail Schedule`)

There is no Brand column. Every confirmation is Pivot Energy Drink
(`pivot-energy-drink`). Standing check-in, do not remint:
`https://client.igniteproductions.co/checkin/PE-Y7VPYC`.

Clock in / Clock out and Open Recap Form use that URL on
`client.igniteproductions.co`. The email also includes **Retail Sampling FAQ**
at `https://client.igniteproductions.co/training/pivot-energy-drink/retail-sampling-faq.pdf`
(the same path the walk-up materials use). Famus, Wook, and Alcohol Armor
links are not attached.

SKU text is matched to the Pivot Product catalog only (`Blue Razz`,
`Pink Lemonade`, `Melon Rush`, `Going Grape`, plus whatever else is on that
tenant). A Famus, Wook, or Alcohol Armor SKU is refused. A blank SKU cell
sends with no product lines.

### Columns (gid 0, read 2026-10-20)

Headers are resolved by name. Letters below are where they sat when the
status block was appended after column AT.

| Col | Header | Use |
|---|---|---|
| A | State | TZ fallback |
| C | Date | Local store date |
| E | Store Name | Email |
| F / G | Start / End Time | `2p` / `5p` style |
| H | Address | Geocode → IANA TZ |
| O | SKUs to sample | Pivot catalog only |
| **P** | **Event Confirmation Sent?** | Status stamp (already on the sheet) |
| T | BA Name | Required |
| W | Email | BA recipient |
| AU | Send Confirmation | Checkbox trigger (appended) |
| AV | Cancel Confirmation | Checkbox trigger |
| AW | Force Resend | Sticky. Prefer Resend Confirmation |
| **AX** | **Confirmation Status** | `Queued` → `Sent` → `Cancelled` / `Error` |
| AY | Confirmation Sent At | |
| AZ | Confirmation Error | |
| BA | Spark Confirmation UUID | |
| BB | Resend Confirmation | One-shot |

## Famus group columns (gid 0, read 2026-10-19)

| Col | Header | Use |
|---|---|---|
| A | State | TZ fallback |
| C | Date | Local store date (`Oct 21` or `Oct 21, 2026`) |
| D | Store Name | Email |
| E / F | Start / End Time | `7a` / `10a` style |
| G | Address | Geocode → IANA TZ |
| **H** | **Brand** | Which tenant(s) this row confirms |
| J | SKUs | Products, matched to those tenants' catalogs |
| M | Confirmed BA Name | Required |
| Q | Email | BA recipient |
| T | Send Confirmation | Checkbox trigger |
| U | Cancel Confirmation | Checkbox trigger |
| V | Force Resend | Sticky. Prefer Resend Confirmation |
| **W** | **Confirmation Status** | `Queued` → `Sent` → `Cancelled` / `Error` (canonical) |
| X | Confirmation Sent At | |
| Y | Confirmation Error | |
| Z | Spark Confirmation UUID | |
| AA–AD | duplicate status headers | Ignored. The script keeps the first header of each name |
| *(appended)* | Resend Confirmation | One-shot. Clears itself and posts `resend: true` |

Status machine matches Torch (`docs/torch-sheet-event-confirmations.md`).
Checking Send on a Sent row does not email again unless Resend Confirmation,
Force Resend, or a BA email change.

## Apps Script install

1. Open the sheet → **Extensions → Apps Script**.
2. Paste [`scripts/brand_group_sheet_event_confirmations.gs`](../scripts/brand_group_sheet_event_confirmations.gs).
3. **Project Settings → Script properties:**
   - `SPARK_CRON_SECRET` = Cloud Run `INTERNAL_CRON_SECRET`
   - `SPARK_API_BASE` = `https://spark-api-new-490085168610.us-central1.run.app` (optional)
4. Share the sheet with `spark-api-new-sa@spark-479222.iam.gserviceaccount.com` as **Editor**.
5. Run `ensureConfirmationColumns`, then `installTriggers` on a first install.
6. Reload the sheet. **Spark Confirmations** menu appears.

Apps Script runs as the human installer for `onEdit`. The service account
writes status back. Drive sharing from the connected Sheets app could not
grant that Editor role (the file was not opened with the app), so the share
is a one-time click for Kyle.

## API contract

```json
{
  "action": "send",
  "rowNumber": 2,
  "sheetId": "1xnnzFb3s0mAV-6abyzmPzEX3wjtObD8Bv4qQsr8VB6A",
  "dryRun": true,
  "values": {
    "Date": "Oct 21, 2026",
    "Start Time": "7a",
    "End Time": "10a",
    "Store Name": "marathon",
    "Address": "1315 Elkhorn Rd, Campbellsville, KY 42718, USA",
    "State": "KY",
    "Brand": "Famus Energy",
    "SKUs": "OG Luxe, Havana Nights",
    "Confirmed BA Name": "Test BA",
    "Email": "you@igniteproductions.co"
  }
}
```

`dryRun: true` validates brand, catalog SKUs, and timezone. No email, no sheet stamp.

Subject shape matches Torch: `Your FAMUS Energy Retail Sampling – marathon | 10/21/2026 | 7a - 10a`.
A Famus + Wook row is `Your FAMUS Energy and Wook Water Retail Sampling – …`.
