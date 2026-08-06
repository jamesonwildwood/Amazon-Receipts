# Amazon Receipts → YNAB Enrichment Service

A locally-hosted personal tool that logs into an Amazon account, downloads order
receipts, extracts line items with an LLM, and enriches the matching existing
YNAB transaction (memo + per-item split + category) instead of creating a new
one — avoiding duplicates when bank-feed import is already turned on for that
account.

See `docs/DESIGN.md` for the full design: architecture, matching algorithm,
duplicate-prevention/reprocess semantics, schema, and phased build order.

## Credits / vendored code

`vendor/amazon_orders_webscraper/` and `vendor/ynab_amazon/` are vendored,
lightly-adapted copies of:

- [aelzeiny/Amazon-Orders-WebScraper](https://github.com/aelzeiny/Amazon-Orders-WebScraper) — Amazon login + order-history scraping.
- [aelzeiny/YNAB_AMAZON](https://github.com/aelzeiny/YNAB_AMAZON) — receipt parsing models and YNAB category lookups.

Neither upstream repo declares a license. They're vendored here for personal,
non-redistributed use only — this is not a license grant. If this repository
is ever published or shared, that decision needs revisiting independently
(e.g. contacting the original author, or reimplementing the handful of reused
files from scratch).

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
chmod 600 .env   # contains Amazon password + TOTP secret + YNAB token + LLM key
```

Fill in `.env`, then see the phased build order in the plan doc for how each
piece (scraper, parsing, matching/dashboard, scheduler/deployment) comes
together and how to verify it at each step.
