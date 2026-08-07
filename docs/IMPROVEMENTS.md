# Improvement Plan

Findings from a full review of the codebase as of 2026-08-06 (commit 93edf2b).

**Status:** Parts 1 and 2 were implemented in PR #1 (merged 2026-08-07 as
693eec4) — P0 items 1–4, P1 items 5–9, and the full dashboard rework; P2 was
deliberately skipped. **Part 3 (below) is the next batch and is not yet
implemented.**

# Part 1 — Code improvements (✅ implemented, except P2)

## P0 — correctness & safety

### 1. `no_candidate` orders are never re-matched
`list_pending_match_order_ids()` only selects `match_status = 'pending_parse'`,
so matching is one-shot per order. But the bank feed typically posts the charge
1–5 days *after* the order (Amazon charges at shipment), so a scrape that runs
the morning after ordering will often find no candidate — and that order is then
stuck at `no_candidate` forever unless manually reset (which requires
`ALLOW_RESET=true`). 41 of the 76 orders in the current DB sit in this state
(most are historical backfill whose transactions are already categorized, but
the going-forward gap is real).

**Fix:** on each pipeline run, also re-run `match_order()` for orders where
`match_status = 'no_candidate'` and `order_date >= today − (match_window + ~10
days)`. Bounded by order_date so ancient orders aren't re-fetched forever.

### 2. No HTTP timeouts on any outbound request
Every `requests` call in `app/ynab/client.py` and `app/parsing/categories.py`
omits `timeout=`. One hung TCP connection wedges the pipeline thread
indefinitely; with `max_instances=1` on the scheduler job, all future scheduled
runs are then silently skipped.

**Fix:** `timeout=(5, 30)` on every call (or a shared `requests.Session` with a
default). Same for the Anthropic/OpenAI clients if their defaults are unbounded
(they're not — both default sanely — so `requests` is the gap).

### 3. Overlapping pipeline runs (Run Now vs cron)
`POST /run-now` spawns a bare thread with no coordination against the
scheduler (whose `max_instances=1` only guards cron-vs-cron). Two concurrent
runs would drive two Selenium logins into the same Amazon account (lockout
risk) and race on `insert_scraped_order` (PK violation mid-run).

**Fix:** a module-level `threading.Lock` acquired non-blocking in
`run_pipeline()`; a second caller logs and returns `None`. Two related
stuck-state fixes: treat a `running` pipeline row older than ~2 h as stale in
the dashboard (today it disables Run Now forever after a crash mid-run), and
mark leftover `running` rows as `error` at startup.

### 4. Stored XSS via saved receipt HTML
`GET /receipts/{id}/html` returns the raw scraped Amazon page as an
`HTMLResponse` on the dashboard's own origin. Any `<script>` in the saved page
executes with access to a dashboard that has unauthenticated state-changing
POST endpoints (approve, reapply, create-transaction).

**Fix:** serve it with `Content-Security-Policy: sandbox` (renders fine,
scripts inert), or as `text/plain`.

## P1 — robustness

### 5. O(N) YNAB API calls per run — rate-limit risk
`match_order()` calls `get_transactions_since()` and (via
`load_categories_map()`) the categories endpoint **per order**. YNAB's limit is
200 requests/hour per token; a backfill or a big re-match batch (see item 1)
can burn through it and start failing.

**Fix:** fetch transactions + categories once per pipeline run and pass them
into `match_order()`; keep the per-order fetch only as a fallback for the
single-order dashboard paths.

### 6. Transient errors are terminal
A network blip during matching sets `match_status = 'error'`, which is never
retried and can only be cleared with `ALLOW_RESET=true`. Same asymmetry as
item 1. **Fix:** re-match `error` orders on the next run (bounded, e.g. max N
attempts recorded in a retry_count column), keeping `error` terminal only for
apply-time failures that need human eyes.

### 7. No CSRF protection / auth on state-changing routes
The dashboard is exposed on the LAN via Traefik. All POSTs are plain-form,
same-site cookies don't apply (no auth at all), so any page you browse could
fire cross-site POSTs at `receipts.geekom.localdomain` (DNS rebinding makes
this realistic even for "internal" hostnames). Low likelihood, high blast
radius (it writes to the budget).

**Fix (cheap):** reject POSTs whose `Origin`/`Host` don't match; optionally a
static bearer token in a cookie set once. Not full auth — just enough to make
cross-origin abuse fail.

### 8. SQLite concurrency hygiene
Every helper opens a fresh connection with default journal mode and no
`busy_timeout`. The scheduler thread writes while dashboard requests read —
under load that's a `database is locked` error waiting to happen.
**Fix:** `PRAGMA journal_mode=WAL` once at init + `PRAGMA busy_timeout=5000`
on connect.

### 9. Verify amount at apply time
`apply_patch()` re-fetches the transaction (good) but only checks `deleted`.
If the transaction was edited since matching (amount changed, reconciled), the
patch still lands. **Fix:** compare `abs(fresh_txn["amount"])` against the
order's `grand_total_cents × 10` and refuse with a clear error if they no
longer match; consider also refusing `cleared == "reconciled"`.

## P2 — polish

10. **Deprecated startup hook** — `@app.on_event("startup")` → FastAPI
    lifespan context. Also document that the app must run single-worker
    (multiple uvicorn workers would start duplicate schedulers).
11. **Decimal end-to-end** — `_milliunits()` round-trips through `float`;
    fine for realistic magnitudes but `Decimal("…") * 1000` with `quantize`
    is free and exact.
12. **`_parse_json_response` fence-stripping** — `text.strip("`")` also eats
    legitimate trailing backticks; use a regex for ``` fences.
13. **`orders_matched` counts attempts, not matches** — a `no_candidate`
    result still increments it. Count only `pending_review`/`ambiguous`
    outcomes, or record both.
14. **Tests** — good coverage on apply/matcher/parser guards; nothing on
    `run_pipeline()` orchestration or the dashboard routes. Add a
    `TestClient` smoke test per route and a pipeline test with fakes.
15. **Backups** — `data/app.db` + `data/receipts_html/` are the system of
    record for what was applied; add a nightly copy (litestream, or a dumb
    `sqlite3 .backup` cron) before trusting it long-term.

# Part 2 — Dashboard/UI upgrade plan

The current UI is functional but raw: JSON dumps instead of readable receipts,
milliunit integers instead of dollar amounts, UTC timestamp strings, no error
surfacing, and silent redirects after actions (Approve can fail and the page
just reloads with no indication). Plan, page by page:

## Cross-cutting

- **Status badges everywhere** — map each `match_status` / run status to a
  label + tone (Applied ✓ green, Needs review ⚠ amber, Ambiguous ⚠ orange,
  Error ✕ red, No bank match / Awaiting parse neutral, Rejected muted).
  Always label + color, never color alone.
- **Formatting filters** (Jinja): cents → `$17.19`, milliunits → `$43.15`,
  timestamps → relative time ("3h ago", full timestamp on hover). SQLite
  timestamps are UTC and currently rendered as-is, which reads as local time.
- **Action feedback** — every POST redirect carries a human-readable outcome
  message (mapping `ApplyResult.reason` → text) rendered as a flash banner.
  Today a failed Approve is indistinguishable from a successful one.
- **Readable receipts** — render `parsed_json` as an items table
  (name · category · price, totals row) with the raw JSON behind a
  `<details>` toggle, on Review and Receipt Detail both.

## Home → an actual operations dashboard

- **KPI tile row** (linked): Needs review · Ambiguous · Errors (incl. parse
  errors) · No bank match · Applied. The three "action needed" tiles get
  status-toned accents when nonzero.
- **Needs-attention queue**: the concrete orders behind those tiles
  (pending_review + ambiguous + error + parse-error), newest first, with the
  error message inline — today the 3 error orders are invisible except as a
  count.
- **Pipeline card**: last run badge + relative times + found/parsed/matched,
  next scheduled run, Run Now (with stale-run handling per Part 1 §3), link
  to Logs. Auto-refresh (meta refresh) while a run is in progress.
- **Integration health card**: last successful scrape, last successful YNAB
  apply, LLM provider/model, budget id, cron schedule, match window/payee
  filters — plus warning chips when dev flags (ALLOW_RESET, ALLOW_REAPPLY,
  YNAB_ALLOW_CREATE_WITHOUT_MATCH) are enabled, so a live budget never runs
  with test bypasses on unnoticed.
- **Recent runs** mini-table (last ~7) with status badges.

## Review

- Side-by-side becomes: parsed receipt items table vs a formatted candidate
  transaction card (payee, date, `$` amount, id) instead of two JSON blobs.
- Show what will be written: memo + split count from the staged payload.
- **Add a Reject button** — the `rejected` status exists in the schema but
  nothing can set it today; a wrong match currently can't be dismissed.
  Backed by a guarded UPDATE (only from `pending_review`/`ambiguous`).
- Ambiguous section keeps the pick-a-candidate table, with formatted amounts.

## History

- Status filter chips (All · each status with its count) via `?status=`.
- Formatted currency, badges, relative scraped-at, truncated txn ids.

## Receipt Detail

- Items table + payload/apply-log as today, but formatted.
- **Backfill button**: when `match_status == 'no_candidate'`, offer "Create
  new YNAB transaction" wired to the existing `create_transaction()` — shown
  only when `YNAB_ALLOW_CREATE_WITHOUT_MATCH=true`, with a confirm checkbox
  (the function exists and is gated, but the flag currently has no UI at all).
  When the flag is off, show a hint naming the env var instead.

## New page: Logs

- `/logs` tailing the last N lines of `data/logs/app.log` (the rotating file
  handler already exists; DESIGN.md promised a dashboard log tail that was
  never built). Read-only, `?lines=` param, refresh link.

## Styling

- Keep the no-JS server-rendered approach; refresh `style.css` with a small
  token set (page/surface/ink/muted/border + the four status colors), stat
  tiles, badges, and `tabular-nums` on numeric table columns. Light + dark
  via `prefers-color-scheme` as today.

## Backend surface the UI plan requires

Small additions, no behavior changes to the pipeline itself:
- `db.list_orders_by_statuses(...)`, `db.list_parse_error_orders()` (reads)
- `db.mark_rejected(order_id)` (guarded write) + `POST /orders/{id}/reject`
- `POST /orders/{id}/create-transaction` (calls existing gated
  `create_transaction()`)
- Part 1 §3's run lock, so the Run Now button is honest.

A full draft of the reworked `routes.py` implementing the above was written
during review and is parked outside the repo (session scratchpad,
`routes.py.draft`) — usable as a starting point when this plan is approved.

# Part 3 — Multi-account support & run modes (next batch)

Two goals, decided 2026-08-07:
1. Scrape a **second Amazon account** (spouse) into the **same YNAB budget
   and same card/YNAB account**.
2. Make the app runnable **ad-hoc from the command line** as well as
   scheduled in Docker, without the two modes stepping on each other.

The single-app approach is deliberate: all duplicate-prevention state
(`bound_transaction_ids`, the atomic claim, the apply log) lives in this
app's SQLite. Both Amazon accounts charge the same card, so both scrapers
MUST share one claim ledger — a second instance could match the same bank
transaction to two different orders and both would PATCH it.

## 3.1 Account list: `amazon_accounts.toml`

- New gitignored file `amazon_accounts.toml` (chmod 600) next to `.env`,
  loaded once at startup with stdlib `tomllib`:

  ```toml
  [[accounts]]
  label = "jameson"          # short, stable; stored on orders, shown in UI
  email = "..."
  password = "..."
  totp_secret = "..."
  # ynab_account_id = "..."  # optional override; omit to use YNAB_ACCOUNT_ID
  ```

- Labels must be unique, non-empty, filesystem-safe (used in paths).
- Ship `amazon_accounts.toml.example`; add the real file to `.gitignore`.
- **Back-compat:** if the file is absent but `AMAZON_EMAIL`/`AMAZON_PASSWORD`/
  `AMAZON_TOTP_SECRET` are set, synthesize a single account labeled
  `default` — existing deployments keep working. If both are present, the
  toml wins and a warning is logged. Update `.env.example` to point at the
  toml as the preferred mechanism.
- Config path setting `AMAZON_ACCOUNTS_PATH` (default `./amazon_accounts.toml`)
  so Docker can mount it read-only.

## 3.2 Schema: track which account an order came from

- `ALTER TABLE amazon_orders ADD COLUMN amazon_account TEXT NOT NULL
  DEFAULT 'default'` — Amazon order ids are globally unique, so the PK is
  unaffected. `init_db()` needs a tiny migration step (check
  `pragma table_info`, ALTER if missing) since the schema uses
  `CREATE TABLE IF NOT EXISTS`.
- Backfill existing rows to the first configured account's label.
- `insert_scraped_order()` takes and stores the label.

## 3.3 Scraper: loop accounts, isolate Chrome profiles

- `run_pipeline()` iterates accounts **sequentially inside the existing run
  lock** — never two Selenium logins at once.
- Per-account Chrome profile: `{chrome_profile_dir}/{label}` (and the same
  subpath convention inside the selenium container). Reusing one profile
  across two Amazon logins would trip Amazon's returning-device logic — the
  exact problem the persisted profile was added to solve.
- A failure scraping one account must not abort the other: catch per
  account, mark the run `partial`, keep per-account results in the log.
- `pipeline_runs` counts stay aggregate; per-account detail goes to the log
  and the health card (3.5).

## 3.4 Matcher: per-account YNAB account id

- Candidate search uses the order's account → its `ynab_account_id`
  override, else the global `YNAB_ACCOUNT_ID`. (Same card today, so the
  override stays unset — the field exists so a separate-card household
  doesn't need a code change.)
- The batched per-run transaction fetch (P1 item 5) should fetch per
  *distinct* YNAB account id, not per Amazon account.

## 3.5 Dashboard: visibility, not editing

Account credentials are deliberately **not** editable in the UI — the list
lives in the toml, restart to reload. The dashboard is LAN-exposed with only
a same-origin check, and UI-managed credentials would put secrets in SQLite
and every backup. What the UI does get:

- Account badge on Review cards, History rows, and Receipt Detail.
- History filter by account.
- Integration-health card: one row per account — label, masked email
  (`j***@…`), last successful scrape *for that account* — so a broken login
  (TOTP drift, challenge) is visible instead of silently dropping orders.
- Never render passwords or TOTP secrets anywhere, masked or otherwise.

## 3.6 CLI entrypoint: ad-hoc runs

New `app/__main__.py` (stdlib argparse, no new deps):

- `python -m app run` — one pipeline pass (scrape → parse → match), summary
  to stdout, exit 0 on success, 1 on partial, 2 on error (cron/scripts can
  react). Safe headless: the pipeline never writes to YNAB — approval stays
  in the dashboard.
- `python -m app run --headful` — overrides `SCRAPE_HEADLESS` for this run.
  Primary use: first login on a new account almost always hits an Amazon
  challenge; one visible-browser run clicks through it and persists the
  Chrome profile.
- `python -m app run --account LABEL` — scrape just one account.
- `python -m app serve` — dashboard + scheduler (what
  `uvicorn app.main:app` does today; keep that invocation working).

## 3.7 Cross-process run lock

The Part 1 P0 run lock is a `threading.Lock` — in-process only. A CLI run
and the server's scheduled run are separate processes; overlapping them
means two simultaneous Selenium logins (account-lockout territory).

- Replace with an OS-level file lock: non-blocking `fcntl.flock` on
  `data/pipeline.lock`, acquired inside `run_pipeline()`. One mechanism for
  both threads and processes; on failure, log and return `None` exactly as
  the thread lock does today.
- Out of scope and documented as such: locking across *machines*. The
  SQLite DB must never be shared over SMB/NFS; ad-hoc runs against the
  server's data happen on the server.

## 3.8 Docs & tests for this batch

- README: multi-account setup, CLI usage, the two run scenarios
  (clone-and-run vs Docker nightly).
- Tests: toml loading/validation (dupe labels, missing fields, env
  fallback), schema migration on an existing DB, per-account matching, CLI
  arg handling, file-lock exclusion (two processes via subprocess).

# Part 4 — Before actually open-sourcing

The README is being written to open-source standard, but publishing this
repo has real blockers beyond docs:

1. **Vendored code has no license.** `vendor/amazon_orders_webscraper/` and
   `vendor/ynab_amazon/` are all-rights-reserved by default (no LICENSE
   upstream). Redistributing them is not permitted. Before publishing:
   get an explicit license/permission from the author (aelzeiny), or
   reimplement the vendored pieces (Selenium page objects; the
   Receipt/Item model shapes) independently.
2. **Choose and add a LICENSE** for the first-party code (MIT/Apache-2.0).
3. **Secrets hygiene check** — verified 2026-08-07: `.env` has never been
   committed and is gitignored; re-verify before publishing (`git log
   --all -- .env`, plus a scan for tokens in receipts fixtures and logs).
   `data/` and `amazon_accounts.toml` must be in `.gitignore`.
4. **Strip personal deployment details** — the Traefik labels/hostname in
   `docker-compose.yml` (`receipts.geekom.localdomain`) belong in a
   `docker-compose.override.yml` that's gitignored, not in the published
   file.
5. **Say the quiet part in the README** (done): scraping Amazon this way
   violates its ToS and risks account challenges/lockout; users accept that
   knowingly, and TOTP secrets in a plaintext file are the deployment's
   biggest secret.
