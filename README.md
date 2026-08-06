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

Fill in `.env`. Leave `ALLOW_RESET` and `ALLOW_REAPPLY` set to `false` unless
you're actively testing — both are safety bypasses on the YNAB-write path.

## Running

**Docker (recommended)** — isolates Chrome in its own container, avoiding the
frequent Selenium breakage that comes from macOS auto-updating your host
Chrome:

```bash
docker compose up -d --build
```

Dashboard: http://localhost:8420. Set Docker Desktop to start at login so the
daily scheduled run (`PIPELINE_SCHEDULE_CRON` in `.env`) survives reboots.

**Without Docker** — the scraper falls back to a local Chrome via
`chromedriver_autoinstaller`, so Chrome must be installed on this machine:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8420
```

To keep it running in the background across logins/reboots on macOS, see
`launchd/com.user.amazonreceipts.plist` (fill in the placeholder paths first).

**Verifying each layer in isolation** (useful before trusting the scheduled
run): `scripts/verify_scrape.py`, `scripts/verify_parse.py`,
`scripts/reprocess_order.py --order-id X --action {reset-review|reset-reparse|reapply}`,
and `pytest tests/`. See `docs/DESIGN.md` for what each is checking.
