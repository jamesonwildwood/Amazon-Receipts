# Improvement Plan

Findings from a full review of the codebase as of 2026-08-06 (commit 93edf2b).
Plan only — nothing here has been implemented yet. Part 1 is code
improvements ordered by priority; Part 2 is the dashboard/UI upgrade plan.

# Part 1 — Code improvements

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
