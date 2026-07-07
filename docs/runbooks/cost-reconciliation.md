# Cost reconciliation — monthly

Compare Langfuse totals against provider invoices once a month. Catches
provider rate changes, Langfuse catalog drift, and missing trace
coverage on new paid calls.

## When

First Monday of every month, for the prior calendar month.

## Inputs

- **Langfuse "Usage" dashboard** — filter by `feature` (one pass each
  for `agent` and `extraction`). Read the totals column for the prior
  month.
- **Provider invoices** for the same month:
  - Anthropic (Sonnet 4.6)
  - OpenAI (gpt-4o, gpt-4o-mini, embeddings)
  - Voyage AI (voyage-4-lite)
  - Groq (whisper-large-v3-turbo)
  - Apify (per-actor breakdown)
  - Google Cloud Console (Maps Platform → Places API New)

## Procedure

For each provider in the table above:

1. Pull the Langfuse total for spans belonging to that provider.
   Filter by `metadata.feature`, model name, or span name as needed.
2. Compare to the invoice total for the same month.
3. Compute drift: `(invoice − langfuse) / invoice`.

## Acceptable drift — provisional

- ≤ 20% per provider
- ≤ 10% across all providers combined

These thresholds are **placeholders for the first two months**. After
two clean reconciliations, replace them with values derived from
observed variance (per-provider invoice − Langfuse delta over the
trailing 2-month window). Known sources of legitimate >10% single-month
swings:

- **Apify**: rental fees + per-result, plus free-tier offset on
  apify/instagram-post-scraper.
- **Google Places**: $200/month free-tier offset against billed calls.
- **OpenAI**: batch-billing lag — calls in the last days of the month
  may invoice the following month.

## If drift exceeds threshold (post-calibration)

1. Identify which line item moved. Read the provider's current pricing
   page (URLs at the bottom of this file — they're also inlined in
   `app.yaml` next to each provider's rates).
2. Update the matching entry in `config/app.yaml` under `pricing:`.
3. Update that provider's `# <provider> — verified YYYY-MM-DD` comment
   to today's date. Each provider section in `app.yaml` carries its own
   verified-on date so a single-provider update doesn't touch the
   others. Reviewers can `git blame` the date line to see when a rate
   was last touched.
4. Commit with a message describing the rate change, effective month,
   and observed variance. The git log is the audit trail — there is no
   separate ticketing system for this team.

## What this catches

- Provider rate changes.
- Drift in Langfuse's pricing catalog for LLM models (when their hosted
  catalog falls behind the provider's actual price).
- Missing spans — a new paid call wired without `traced_call` shows up
  as invoice spend with zero Langfuse cost.

## What this does NOT catch

- Free-tier overage that gets discounted on the invoice but not in
  Langfuse arithmetic.
- SDK-internal retries that don't surface as separate spans.
- Bandwidth / CDN / Cloudflare incidentals.

## Pricing sources

- OpenAI — https://openai.com/api/pricing
- Anthropic — https://platform.claude.com/docs/en/about-claude/pricing
- Voyage AI — https://docs.voyageai.com/docs/pricing
- Groq — https://groq.com/pricing
- Google Places (New) — https://developers.google.com/maps/billing-and-pricing/pricing
- Apify actor pages — `https://apify.com/<actor-org>/<actor-name>` (pricing tab)
