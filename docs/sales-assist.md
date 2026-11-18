# Sales assist: Spark numbers into Close drafts

Sales assist writes **drafts** into Close. A rep reads each one, edits it and
sends it from Close. Nothing goes out automatically: the code can create a
note and an email with status "draft", and nothing else. It cannot send,
schedule, update or delete anything in Close.

## Two kinds of draft

### 1. Quarterly results for an existing client (renewal)

For each Spark brand linked to its Close lead, once a quarter (and ahead of the
renewal date, if one is set) it makes:

- **a note** on the lead with the quarter's Spark numbers. These are the
  brand dashboard's own figures, from approved recaps, counted by event date:
  events, consumers sampled, units sold, conversion (retail and on-premise
  only), top stores, states, a few shopper quotes and links to the best photos.
  No-product visits are left out of the demo count, and the note says how many.
- **a draft email** to the lead's first contact (or the contact email set on
  the link), titled "Your Q3 2026 in-store results".

The short summary paragraph is written by AI. Every number in it is checked
against Spark. If any number is not in Spark's figures, the paragraph is
dropped and the note goes out with just the numbers, which code fills in
directly.

A client's numbers only ever go onto that client's own lead.

### 2. A proposal for a new lead (prospect)

When a lead is created in Close, it:

1. **Researches the brand.** It reads the lead's Close fields (website,
   description, Industry, Total Funding and other enrichment already on the
   lead) and a few pages of the brand's own website: the homepage plus up to
   three about, products, where-to-buy or press pages. It follows robots.txt.
2. **Finds Ignite's closest real results** from Spark, matched on category and
   retailer, over the last 12 months.
3. **Drafts a short email and a one-page pitch.** The pitch goes in a note on
   the lead and the email is saved as a draft.

**Other clients are never named.** Their results appear only as anonymized
examples, such as "a national energy-drink brand at Kroger: 1,400+ consumers
sampled, 20% conversion". The protections:

- Each brand is described only by a generic descriptor (see "Descriptors"
  below).
- Figures are rounded down and shown with a plus sign.
- Retailers are named only if they are large chains.
- Category roll-ups need at least 3 brands.
- The AI never sees another client's name or exact figures.
- A draft that names another client, quotes an exact client figure, or
  contains a price, a rate, "42k", Girl Beer or kratom is thrown away.

Leads are skipped in these cases:

- The lead matches an existing Spark brand.
- The lead isn't a consumer brand, judging by its own website.
- There's nothing to research.
- More than 40 leads arrive at once. That is treated as a bulk import, not
  inbound leads.

## For reps

1. Open the lead in Close. Look for the note that starts with
   **"SPARK SALES ASSIST - DRAFT FOR REVIEW"**.
2. Check the research. It comes from the brand's own site and may be thin or
   out of date.
3. Open **Emails → Drafts** on the lead. Edit the draft, add your signature and
   send it when it's right. You can also delete it.
4. For prospects: never swap in a client's real name, and never add exact
   figures from another client's dashboard. Never quote rates or pricing in
   these emails.

## For staff: linking clients to Close

Nothing is linked automatically. To link a brand, use one of these:

- **GraphQL (staff only)** on the clients schema:
  `linkTenantToClose(tenantId, closeLeadId, closeOpportunityId?, renewalDate?, contactEmail?)`
  and `unlinkTenantFromClose(tenantId)`. `salesAssistLinks` lists links and
  `salesAssistDrafts(kind, first)` lists the audit log.
- **The workflow**: run "Sales assist" with action `suggest-links`. It returns
  fuzzy name matches and changes nothing. Review the matches, then run action
  `link` with a JSON map such as `{"18": "lead_abc…"}` and `apply` = true.
- **Django admin**: Sales assist → Close links / Sales profiles.

**Descriptors.** The first time a brand is used as an example, a small AI call
describes it in a generic way, for example "a national energy-drink brand".
The description is rejected if it contains the brand's name or a product name.
Staff can edit it, or tick "exclude from examples", under Django admin →
Sales profiles.

## Settings (Cloud Run)

| Variable | Default | What it does |
|---|---|---|
| `CLOSE_API_KEY` | empty | Close API key. When empty, every Close step does nothing. |
| `SALES_ASSIST_ENABLED` | `false` | Second lock. Close is written to only when this is true and the run passes `apply=true`. |
| `SALES_ASSIST_MAX_PROSPECTS_PER_RUN` | 10 | New leads drafted per run. Extras wait for the next run. |
| `SALES_ASSIST_MAX_RENEWALS_PER_RUN` | 30 | Renewal drafts per run. |
| `SALES_ASSIST_MAX_AI_CALLS_PER_RUN` / `SALES_ASSIST_MAX_USD_PER_RUN` | 40 / 1.00 | AI caps per run. |
| `SALES_ASSIST_IMPORT_BURST` | 40 | Above this many new leads at once, the batch is treated as an import and skipped. |
| `SALES_ASSIST_RENEWAL_LEAD_DAYS` | 45 | How many days before `renewal_date` the renewal draft is made. |
| `SALES_ASSIST_PROSPECT_STATUS_IDS` | empty | Optional: only draft for these Close lead statuses. |

## Runs

The endpoint is `/internal/cron/sales-assist` and needs the `X-Cron-Secret`
header. The GitHub workflow "Sales assist" calls it:

- **Weekdays, every 2 hours**: `prospects`, then renewals in `due` mode (links
  whose renewal date is close).
- **The 3rd of Jan, Apr, Jul and Oct**: `renewals` for every linked client,
  covering the quarter that just ended.
- **Manual**: any action, for one tenant or one lead (`lead=lead_…`), as a dry
  run unless `apply` is true.

Every run is logged in `SalesAssistRun`, with its AI call count and an
estimated cost. Every draft is logged in `SalesAssistDraft`, including dry
runs, skips and failures, with the facts it was allowed to use. One draft is
created per client per quarter and one per lead, so a rerun never duplicates
a draft. Each note also carries a `[spark-sales-assist:…]` marker that is
checked in Close before writing.

**Cost.** A new lead takes one small-model research call and one or two
drafting calls, roughly $0.01 to $0.04. A renewal note takes one or two
calls, roughly $0.01 to $0.02. The first proposal run also makes one small
call per brand that has no descriptor yet. These are estimates; OpenAI's
usage page has the real bill.
