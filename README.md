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
> - This app holds your Amazon password(s), a TOTP secret that bypasses your
>   2FA, and a YNAB token with write access to your budget — in plaintext
>   (`.env` and/or `amazon_accounts.toml`). Treat the machine it runs on
>   accordingly.
> - The `vendor/` directory contains code from upstream repos that declare
>   **no license** (see [Credits & licensing](#credits--licensing)). This
>   repository is currently suitable for personal use, not redistribution.

## How it works

```
scrape (Selenium) ──> parse (LLM) ──> match (YNAB API) ──> PATCH (auto) ──> YOU categorize
   saves raw HTML      line items      finds the bank-fed     writes memo +   in YNAB's own
   per order           + categories    txn by date/amount     category splits approve queue
```

Supports scraping **multiple Amazon accounts** (e.g. a household) into the
same YNAB budget/account, and can run **ad-hoc from the command line** in
addition to the always-on scheduled server — see
[Multi-account setup](#multi-account-setup) and [Running](#running) below.

Runs daily on a schedule (and on demand from the dashboard, or `python -m app
run`). A single-candidate match — the normal case — is written to YNAB
**automatically**, no human click required: memo and per-item category
splits, categorized wherever the parsed item resolves against a real YNAB
category and left uncategorized where it doesn't. The bank-fed transaction
still lands in YNAB's own approve/categorize queue as usual — this app's
job is getting the item detail onto the right transaction, not deciding your
budget categories for you. The write path itself is guarded regardless:

- **PATCH-only by default** — it enriches the transaction the bank feed
  already created; it never posts duplicates. (An optional, off-by-default
  flag allows creating a transaction for orders the bank feed missed.)
- **Duplicate prevention** — each YNAB transaction can be claimed by exactly
  one order, enforced at match time and re-checked at write time; an atomic
  claim makes the write idempotent against overlapping runs.
- **Amount re-verification** — the transaction is re-fetched at apply time
  and refused if it was deleted or its amount changed since matching.
- **Ambiguity is never auto-resolved** — zero candidates leaves the order for
  the re-matcher to keep retrying; two or more candidates leaves it alone
  entirely (no auto-pick) — both show up in History and the notification
  digest, never written to YNAB.
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
cp amazon_accounts.toml.example amazon_accounts.toml
chmod 600 amazon_accounts.toml
```

Fill in `.env` and `amazon_accounts.toml`. To find your budget and account ids:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/test_ynab_connection.py   # lists budgets + accounts for your token
```

Set `YNAB_BUDGET_ID` explicitly rather than leaving `last-used` — `last-used`
silently follows whichever budget you last opened in the YNAB UI.

### Multi-account setup

`amazon_accounts.toml` is the preferred way to configure which Amazon
account(s) to scrape — one `[[accounts]]` block per login, e.g. a household
with two Amazon accounts feeding the same YNAB card:

```toml
[[accounts]]
label = "jameson"
email = "jameson@example.com"
password = "..."
totp_secret = "..."

[[accounts]]
label = "spouse"
email = "spouse@example.com"
password = "..."
totp_secret = "..."
```

- Labels must be unique, non-empty, and filesystem-safe (letters, digits,
  `-`, `_`) — they're stored on orders, shown in the dashboard, and used as a
  Chrome-profile subdirectory (`{CHROME_PROFILE_DIR}/{label}`), since reusing
  one Chrome profile across two different Amazon logins trips Amazon's
  returning-device detection.
- Both accounts charge the same card in the common case, so both just enrich
  transactions in the one `YNAB_ACCOUNT_ID`. Add `ynab_account_id = "..."` to
  an account's block only if it needs to enrich a *different* YNAB account.
- All configured accounts share this app's single SQLite duplicate-prevention
  ledger — that's the point: two scrapers must never independently claim the
  same bank transaction.
- **Back-compat:** if `amazon_accounts.toml` doesn't exist, `AMAZON_EMAIL` /
  `AMAZON_PASSWORD` / `AMAZON_TOTP_SECRET` in `.env` are synthesized into a
  single account labeled `default`. If both exist, the toml wins (a warning
  is logged) — remove one or the other to silence it.
- Not editable from the dashboard by design — credentials only ever live in
  this file, never in SQLite or a backup. Restart the process to pick up edits.

### Configuration reference

| Variable | Default | Purpose |
|---|---|---|
| `AMAZON_ACCOUNTS_PATH` | `./amazon_accounts.toml` | Multi-account config (see above); Docker-mountable |
| `AMAZON_EMAIL` / `AMAZON_PASSWORD` / `AMAZON_TOTP_SECRET` | — | Legacy single-account back-compat only — prefer `amazon_accounts.toml` |
| `YNAB_PERSONAL_ACCESS_TOKEN` | — | YNAB API token (write access) |
| `YNAB_BUDGET_ID` | `last-used` | Set explicitly (see above) |
| `YNAB_ACCOUNT_ID` | — | The card account the bank feed imports into |
| `YNAB_MATCH_WINDOW_DAYS` | `10` | ± days around order date to search for the charge (Amazon charges at shipment, which can trail the order by more than a week) |
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
| `NOTIFY_SMTP_HOST` | empty | Empty disables notifications entirely (see [Notifications](#notifications)) |
| `NOTIFY_SMTP_PORT` | `587` | SMTP+STARTTLS port |
| `NOTIFY_SMTP_USER` / `NOTIFY_SMTP_PASSWORD` | — | SMTP auth (a Gmail app password, for example) |
| `NOTIFY_EMAIL_FROM` | `NOTIFY_SMTP_USER` | From address; defaults to the SMTP user when empty |
| `NOTIFY_EMAIL_TO` | — | Where to send notifications |
| `NOTIFY_DASHBOARD_URL` | `http://localhost:8420` | Link included in notification emails |
| `PIPELINE_SCHEDULE_CRON` | `0 7 * * *` | Daily run schedule (5-field cron) |
| `DASHBOARD_PORT` / `DATABASE_PATH` / `RECEIPTS_DIR` / `LOG_LEVEL` | sane defaults | Paths & port |

### Notifications

Nobody reliably checks a dashboard — the app can email you instead of relying
on that. Off by default; set `NOTIFY_SMTP_HOST` to turn it on. Fires:

- Immediately when a pipeline run ends `error` or `partial`, with the error.
- A digest after every run that applied, left ambiguous, left unmatched, or
  errored on at least one order — the primary "what happened" surface now
  that there's no Pending Review page to check instead (see
  [Auto-apply](#auto-apply) below). Silent on a healthy run with nothing to
  report.
- Once, the moment the [config sanity check](#config-sanity-check) starts
  failing (bad YNAB token/budget id, broken `amazon_accounts.toml`).

A notifier failure never fails or delays the pipeline itself — it's caught
and logged, nothing more.

Works with any SMTP+STARTTLS provider, including Gmail with an
[app password](https://support.google.com/accounts/answer/185833) (not your
normal Gmail password — you'll need 2-Step Verification enabled first):

```
NOTIFY_SMTP_HOST=smtp.gmail.com
NOTIFY_SMTP_PORT=587
NOTIFY_SMTP_USER=you@gmail.com
NOTIFY_SMTP_PASSWORD=<16-character app password>
NOTIFY_EMAIL_TO=you@gmail.com
```

### Auto-apply

Owner's call, decided 2026-08-15: bank-imported transactions already sit in
YNAB's own approve/categorize queue, so a second pending-review queue in this
app was a parallel workflow nobody checked. This app's unique value is item
names and split amounts landing on the right transaction — categorization is
a human act done inside YNAB, not here.

A single-candidate match is now applied **immediately, always** — there is
no flag and nothing to opt into. It goes through the same guarded
`apply_patch()` write path regardless (atomic claim, transaction re-fetch,
amount re-verification, claim ledger, full-state PATCH — none of that
changed). The payload auto-categorizes as much as it safely can: an item
whose LLM-guessed category resolves against your budget's real categories
gets that category; anything unresolved is left `category_id: null`
(Uncategorized) rather than force a guess. Either way the transaction lands
in YNAB's approve/categorize queue for you.

**Ambiguous matches (2+ candidates) and orders with no bank-fed transaction
yet are never auto-picked** — they're left alone, recorded in History, and
the re-matcher keeps retrying recent ones on later runs. A guard refusal at
apply time (amount changed since matching, already claimed, etc.) also stops
and waits for a human rather than forcing the write. All of the above show up
in the notification digest, which is now the primary "what happened"
surface — applied N (order/account/amount/matched transaction date),
ambiguous M, no bank match K, errors — silent when a run has nothing to
report. Applied orders are logged in `ynab_apply_log` exactly like any other
apply, visible per order on Receipt Detail.

### Config sanity check

At scheduler startup and at the start of every pipeline run (including
`python -m app run`), the app makes one cheap authenticated YNAB call and
re-checks `amazon_accounts.toml`. A revoked token or a stale/wrong budget id
(the "last-used" trap) shows up immediately as a banner on the Home page and
a notification naming the bad setting, instead of failing silently run after
run. (LLM-key sanity isn't checked the same way — there's no cheap
authenticated call for either provider that doesn't either spend tokens or
require shaping a request like a real extraction call.)

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

> The stack publishes its ports directly (`8420` for the dashboard, `4444` for
> Selenium) and depends on no reverse proxy or external Docker network. Add
> Traefik/Caddy labels yourself if you want hostname routing.

### Without Docker

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8420
# equivalently:
python -m app serve
```

Requires Chrome installed locally (`chromedriver_autoinstaller` handles the
driver). Run **single-worker only** — the scheduler lives in-process. To
survive reboots on macOS, see `launchd/com.user.amazonreceipts.plist`.

### CLI: ad-hoc runs

`python -m app run` runs one scrape → parse → match pass and exits — no
dashboard, no scheduler. It's safe to run alongside (or instead of) the
scheduled server: both share the same SQLite state and the same cross-process
file lock (`data/pipeline.lock`), so an ad-hoc run and a scheduled/Run-Now run
can never overlap into two simultaneous Amazon logins — one just gets
refused (exit code 2) rather than racing the other.

```bash
python -m app run                    # every configured account
python -m app run --account jameson  # just one account
python -m app run --headful          # visible browser -- see "First run" below
```

Exit codes: `0` success, `1` partial (e.g. one account's login failed but
others succeeded), `2` error (including "another run already has the lock").
It never writes to YNAB — approval always happens in the dashboard.

Two common scenarios this enables:
- **Clone-and-run**: no server, no Docker — just `python -m app run` by hand
  or from your own cron, pointed at the same `data/` directory the dashboard
  reads.
- **Docker nightly + occasional ad-hoc**: the compose stack runs the
  scheduled job as usual; `docker compose exec app python -m app run
  --account jameson` triggers an extra one-off pass (e.g. right after placing
  an order) without touching the container's own scheduler.

### First run

Expect Amazon to challenge the first login from a new device/IP. Run
`python -m app run --headful` (or set `SCRAPE_HEADLESS=false` and trigger
**Run Now** from the dashboard, or use `scripts/verify_scrape.py`) and click
through the challenge once — the persisted, per-account Chrome profile
(`{CHROME_PROFILE_DIR}/{label}`) keeps subsequent runs recognized. Do this
once per configured account.

## The dashboard

- **Home** — a prominent banner when the [config sanity check](#config-sanity-check)
  is failing (bad YNAB token/budget id, broken `amazon_accounts.toml`); KPI
  tiles (ambiguous / errors / no bank match / applied) linking into filtered
  History views; a read-only recent-activity list (what the re-matcher is
  still working through, or a rare crash-stuck order — nothing to action from
  here); pipeline state (last/next run, Run Now); and integration health
  (last scrape, last YNAB write, active dev flags, notifications status, and
  **one row per configured Amazon account** — label, masked email, that
  account's own last successful scrape, so one broken login doesn't hide
  behind the other account looking healthy). There is no more Pending
  Review page — `/review` redirects to History for old bookmarks.
- **History** — all orders filterable by status **and by Amazon account**,
  plus recent pipeline runs. This is the audit trail now — applied,
  ambiguous, no-match, and error orders all live here.
- **Order detail** — parsed items, staged payload, full apply history, raw
  receipt HTML, the order's Amazon account, and (when the flags are on)
  reset/re-apply/create-transaction tools — maintenance, not review.
- **Logs** — tail of the application log.

Account credentials are never shown in the dashboard, masked or otherwise —
edit `amazon_accounts.toml` and restart to change them.

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
audit trail), `logs/`, `chrome_profile/{label}/` (one persisted profile per
configured account), and `pipeline.lock` (an empty file used only to hold the
OS-level run lock — safe to delete if it's ever left behind after a hard
crash). **Back up `data/`** — it is the record of what was written to your
budget. `amazon_accounts.toml` (also gitignored, `chmod 600`) holds Amazon
credentials and lives next to `.env`, not inside `data/`.

In addition to backing up `data/` yourself, the app takes its own automatic
backups: after each pipeline run that finishes healthy, `app.db` is copied
via SQLite's own backup API (a consistent snapshot, safe even while the app
is writing) to `data/backups/app-YYYYMMDD-HHMMSS.db`, keeping the most
recent 14 and deleting older ones. **To restore one:** stop the app, move
(don't just copy over) the desired `data/backups/app-<timestamp>.db` to
`data/app.db`, then start the app again — `receipts_html/` and
`ynab_apply_log` history for anything applied after that backup's timestamp
are unaffected by the restore (the receipt HTML files and any real YNAB
writes already happened; only this app's own bookkeeping rolls back).

Security posture, plainly: the dashboard has **no authentication** — anyone
who can reach the port can approve writes to your budget. State-changing
routes reject cross-origin requests, but you should still keep it off
untrusted networks (localhost or a trusted LAN; don't port-forward it).

## Roadmap

See `docs/IMPROVEMENTS.md` — Parts 1–3 (correctness/safety fixes, the
dashboard rework, and multi-account + CLI support), Part 5 (notifications,
YNAB 429 retries, a wider match window, config sanity checks, and automatic
backups), and Part 6 (always-apply single-candidate matches, YNAB's own
approve/categorize queue as the review checkpoint, dashboard reduced to
history + logs — supersedes 5.2's opt-in auto-apply flag) are implemented;
Part 4 (licensing/secrets hygiene before any thought of open-sourcing this)
is not. `docs/DESIGN.md` covers the architecture and the reasoning behind the
safety model — note its "manual approve" description predates Part 6.

## Credits & licensing

Vendored, lightly-adapted code from:

- [aelzeiny/Amazon-Orders-WebScraper](https://github.com/aelzeiny/Amazon-Orders-WebScraper) — Amazon login + order-history scraping (`vendor/amazon_orders_webscraper/`)
- [aelzeiny/YNAB_AMAZON](https://github.com/aelzeiny/YNAB_AMAZON) — receipt model shapes + YNAB category lookups (`vendor/ynab_amazon/`)

This project started from aelzeiny's work and grew into a derivative with
its own architecture (the match-and-PATCH write path, review queue, and
safety model are original) — thanks to the original author for the
foundation. Neither upstream repo declares a license; this repo is private
and the vendored code is used personally, not redistributed. If this
repository is ever made public, resolve that first (author's permission or
an independent reimplementation of `vendor/`).
