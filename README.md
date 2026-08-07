# Amazon Receipts → YNAB

A self-hosted service that logs into your Amazon account, downloads order
receipts, extracts line items with an LLM, and **enriches the matching
bank-fed YNAB transaction in place** — memo, per-item category splits —
instead of creating a new transaction. Built for the common setup where your
credit card's bank feed already imports Amazon charges as amounts with no
detail.

> **Read before using**
>
> - Scraping Amazon with automated login **violates Amazon's Terms of
>   Service** and can trigger security challenges or account lockout. Use it
>   knowingly, on your own account, at your own risk.
> - This app holds your Amazon password, a TOTP secret that bypasses your
>   2FA, and a YNAB token with write access to your budget — in a plaintext
>   `.env`. Treat the machine it runs on accordingly.
> - The `vendor/` directory contains code from upstream repos that declare
>   **no license** (see [Credits & licensing](#credits--licensing)). This
>   repository is currently suitable for personal use, not redistribution.

## How it works

```
scrape (Selenium) ──> parse (LLM) ──> match (YNAB API) ──> YOU approve ──> PATCH
   saves raw HTML      line items      finds the bank-fed     dashboard      writes memo +
   per order           + categories    txn by date/amount     review queue   category splits
```

Runs daily on a schedule (and on demand from the dashboard). Nothing is ever
written to YNAB without an explicit **Approve** click, and the write path is
guarded:

- **PATCH-only by default** — it enriches the transaction the bank feed
  already created; it never posts duplicates. (An optional, off-by-default
  flag allows creating a transaction for orders the bank feed missed.)
- **Duplicate prevention** — each YNAB transaction can be claimed by exactly
  one order, enforced at match time and re-checked at write time; an atomic
  claim makes Approve idempotent against double-clicks and overlapping runs.
- **Amount re-verification** — the transaction is re-fetched at apply time
  and refused if it was deleted or its amount changed since matching.
- **Ambiguity is never auto-resolved** — zero candidates parks the order;
  two or more requires you to pick.
- **Full audit log** — every apply attempt (payload, result, error) is
  recorded and visible per order in the dashboard.

## Requirements

- Python 3.11+ (or just Docker)
- A YNAB account with a [Personal Access Token](https://api.ynab.com/) and
  bank-feed import enabled on the card account
- An Amazon account with authenticator-app (TOTP) 2FA — you'll need the TOTP
  secret, shown at authenticator setup time
- An LLM: an Anthropic API key (default; uses Claude Haiku), **or** any
  OpenAI-compatible endpoint including a local Ollama/vLLM server
- Chrome/Chromium: provided by the bundled Selenium container under Docker;
  install locally otherwise

## Setup

```bash
git clone <this-repo> && cd Amazon-Receipts
cp .env.example .env
chmod 600 .env    # it will hold every secret this app has
```

Fill in `.env`. To find your budget and account ids:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/test_ynab_connection.py   # lists budgets + accounts for your token
```

Set `YNAB_BUDGET_ID` explicitly rather than leaving `last-used` — `last-used`
silently follows whichever budget you last opened in the YNAB UI.

### Configuration reference

| Variable | Default | Purpose |
|---|---|---|
| `AMAZON_EMAIL` / `AMAZON_PASSWORD` | — | Amazon login |
| `AMAZON_TOTP_SECRET` | — | TOTP secret (base32; spaces ok). **Bypasses 2FA — most sensitive value here** |
| `YNAB_PERSONAL_ACCESS_TOKEN` | — | YNAB API token (write access) |
| `YNAB_BUDGET_ID` | `last-used` | Set explicitly (see above) |
| `YNAB_ACCOUNT_ID` | — | The card account the bank feed imports into |
| `YNAB_MATCH_WINDOW_DAYS` | `5` | ± days around order date to search for the charge |
| `YNAB_ONLY_MATCH_UNCATEGORIZED` | `true` | Skip transactions you've already categorized |
| `YNAB_AMAZON_PAYEE_FILTERS` | `Amazon,AMZN` | Payee substrings that identify Amazon charges |
| `YNAB_ALLOW_CREATE_WITHOUT_MATCH` | `false` | Opt-in: allow creating a transaction when the bank feed has none |
| `LLM_PROVIDER` | `anthropic` | `anthropic` or `openai_compatible` |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` | — / `claude-haiku-4-5` | When using Anthropic |
| `OPENAI_COMPATIBLE_BASE_URL` / `_API_KEY` / `_MODEL` | Ollama defaults | When using a local/compatible server |
| `SELENIUM_REMOTE_URL` | empty | Empty = local Chrome; Docker compose sets this for you |
| `SCRAPE_HEADLESS` | `true` | Set `false` to watch the browser (debugging login challenges) |
| `CHROME_PROFILE_DIR` | `./data/chrome_profile` | Persists the browser session so Amazon sees a returning device |
| `ALLOW_RESET` / `ALLOW_REAPPLY` | `false` | **Dev-only safety bypasses — keep `false` against a live budget** |
| `PIPELINE_SCHEDULE_CRON` | `0 7 * * *` | Daily run schedule (5-field cron) |
| `DASHBOARD_PORT` / `DATABASE_PATH` / `RECEIPTS_DIR` / `LOG_LEVEL` | sane defaults | Paths & port |

## Running

### Docker (recommended)

```bash
docker compose up -d --build
```

This starts the app plus a `selenium/standalone-chromium` container (works on
amd64 and arm64 — including Raspberry Pi/NAS). Chrome's session is persisted
in a named volume so Amazon recognizes a returning device instead of
challenging every run.

Dashboard: http://localhost:8420. Configure your Docker host to start the
stack on boot (`restart: unless-stopped` is already set).

> The `traefik` labels and `web` network in `docker-compose.yml` are for the
> author's home reverse-proxy setup — remove them or adapt the hostname if
> you don't run Traefik.

### Without Docker

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8420
```

Requires Chrome installed locally (`chromedriver_autoinstaller` handles the
driver). Run **single-worker only** — the scheduler lives in-process. To
survive reboots on macOS, see `launchd/com.user.amazonreceipts.plist`.

### First run

Expect Amazon to challenge the first login from a new device/IP. Set
`SCRAPE_HEADLESS=false`, trigger **Run Now** from the dashboard (or
`scripts/verify_scrape.py`), and click through the challenge once — the
persisted Chrome profile keeps subsequent runs recognized.

## The dashboard

- **Home** — status tiles, a needs-attention queue, pipeline state
  (last/next run, Run Now), and integration health (last scrape, last YNAB
  write, active dev flags).
- **Review** — the approval queue: parsed receipt beside the matched bank
  transaction, with Approve / Reject; ambiguous matches list every candidate
  for you to pick.
- **History** — all orders filterable by status, plus recent pipeline runs.
- **Order detail** — parsed items, staged payload, full apply history, raw
  receipt HTML, and (when the flags are on) reset/re-apply tools.
- **Logs** — tail of the application log.

## Verifying & testing

```bash
pytest tests/                       # unit tests, no network needed
python scripts/verify_scrape.py     # just the Amazon login + scrape layer
python scripts/verify_parse.py      # just the LLM extraction layer
python scripts/test_ynab_connection.py  # just the YNAB token/ids
```

`scripts/reprocess_order.py` re-runs matching or parsing for a single order
(requires the dev flags).

## Data & operations

Everything lives in `./data/` (gitignored): `app.db` (SQLite — orders, match
state, apply audit log), `receipts_html/` (raw scraped receipts, kept as the
audit trail), `logs/`, and the Chrome profile. **Back up `data/`** — it is
the record of what was written to your budget.

Security posture, plainly: the dashboard has **no authentication** — anyone
who can reach the port can approve writes to your budget. State-changing
routes reject cross-origin requests, but you should still keep it off
untrusted networks (localhost or a trusted LAN; don't port-forward it).

## Roadmap

See `docs/IMPROVEMENTS.md` — next up: multiple Amazon accounts feeding one
budget (`amazon_accounts.toml`), a CLI entrypoint for ad-hoc runs, and a
cross-process run lock. `docs/DESIGN.md` covers the architecture and the
reasoning behind the safety model.

## Credits & licensing

Vendored, lightly-adapted code from:

- [aelzeiny/Amazon-Orders-WebScraper](https://github.com/aelzeiny/Amazon-Orders-WebScraper) — Amazon login + order-history scraping (`vendor/amazon_orders_webscraper/`)
- [aelzeiny/YNAB_AMAZON](https://github.com/aelzeiny/YNAB_AMAZON) — receipt model shapes + YNAB category lookups (`vendor/ynab_amazon/`)

**Neither upstream repo declares a license**, which means all rights are
reserved by their author. They are vendored here for personal,
non-redistributed use; this README is not a license grant. Before this
repository could be published or redistributed, the vendored code needs an
explicit license from its author or an independent reimplementation — see
`docs/IMPROVEMENTS.md` Part 4. First-party code in `app/` is unlicensed
pending that decision.
