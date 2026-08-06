# Amazon Receipts → YNAB Enrichment Service

## Context

Jameson's YNAB budget has a credit card account with bank-feed auto-import turned on. Amazon charges land there with "no useful data other than the amount charged." The goal is a small, locally-hosted personal tool that logs into his Amazon account, reads new order receipts, uses an LLM to extract line items, and **enriches the existing bank-fed YNAB transaction in place** (memo + per-item split + category) — never creating a new transaction, since a new one would duplicate what the bank feed already created.

This is a greenfield build: the repo currently contains only `.git` (one "Initial commit"), no source.

Jameson wants to reuse the working code from two existing repos directly rather than rewrite it from scratch — vendor it into this app, then wrap it with the protections and GUI this project actually needs:
- `aelzeiny/Amazon-Orders-WebScraper` — Selenium + pyotp login (incl. a virtual WebAuthn authenticator via CDP to satisfy passkey challenges), order-history pagination, downloads each order's `print.html` receipt page. Doesn't touch YNAB at all — reusable close to as-is.
- `aelzeiny/YNAB_AMAZON` — strips receipt HTML to text, sends to an LLM for structured extraction, builds YNAB split-subtransaction payloads, tracks processed orders in SQLite. Its HTML-stripping and Pydantic `Receipt`/`Item` models are reusable close to as-is. Its `ynab.py`/`main.py` (which **POSTs new transactions**) is not reusable as-is — that's the one behavior this project must not do, since it would duplicate what the bank feed already created; this piece gets replaced by the matcher/apply logic below, not ported. It also **contains a real bug**: `Item.set_ynab_category` matches a category and then unconditionally overwrites it back to `None` on the very next line, so auto-categorization never actually applies in the original code. Must be fixed (early-return, not overwrite) when vendored.

**Licensing note**: neither repo declares a license (GitHub shows no LICENSE file for either) — meaning all rights are reserved by their author by default, not granted under MIT/Apache/etc. For private, personal, non-redistributed use this is low practical risk and common practice, but it's worth knowing plainly: this isn't "open source we're licensed to reuse," it's unlicensed code we're vendoring for personal use. If this app's source were ever shared or published, that would need a fresh look (e.g., asking the author, or rewriting the handful of reused files independently).

Key decisions already made with Jameson, driving everything below:
- **Match-and-PATCH existing YNAB transactions, never POST new ones** (bank feed stays on for this account).
- **Pluggable LLM**: default to Anthropic Claude (Haiku — this is a narrow, schema-constrained extraction task, not general reasoning), but must also support pointing at a local OpenAI-compatible endpoint (Ollama/vLLM) since he's considering self-hosting the model.
- **Control panel**: this is a real always-on local web service (dashboard + scheduler + pipeline), not a bare cron script — he wants to see logs, last-run status, and integration health.
- **Safety-first on writes**: a Pending Review queue with manual Approve before any YNAB PATCH; ambiguous matches (0 or 2+ candidates) are never auto-picked.
- **Explicit duplicate-prevention and safe re-push/reprocess for testing** (detailed in §4 and §7 below) — this was called out as a hard requirement, not a nice-to-have.
- Scraping Amazon this way violates its Terms of Service and risks account challenges/lockout — accepted knowingly.

## 1. Reuse strategy: vendor, don't rewrite, where it actually fits

- `git clone` both repos into `vendor/amazon_orders_webscraper/` and `vendor/ynab_amazon/` at Phase 0, kept close to their original form (small diffs tracked cleanly, not squashed into our own files) so upstream fixes/updates can be diffed back in later if wanted.
- **Amazon-Orders-WebScraper**: reused close to verbatim — `pages.py`'s page-object classes (login flow, WebAuthn CDP trick, order pagination, receipt fetch) become the implementation behind `app/scraper/`, which is a thin wrapper that swaps hardcoded env-var reads for `app/config.py` settings and calls into the vendored classes directly. No reason to reinvent Selenium selectors that already work.
- **YNAB_AMAZON**: `models.py` (the `Receipt`/`Item` Pydantic models, minus the category bug) and the HTML-stripping logic in `main.py` are reused directly — good, tested shape for the extraction schema. `gpt.py` is reused as the *template* for the prompt/schema but rewritten behind the pluggable LLM interface (§ below), since it hardcodes OpenAI and an outdated model. `ynab.py`'s `get_accounts`/`get_categories` GET calls are reused as-is; its `post_transaction` and all of `main.py`'s orchestration are **not** reused — replaced by `app/ynab/matcher.py` and `app/ynab/apply.py`, because posting new transactions is the one thing this project must not do.
- Net effect: the scraper and the extraction-schema/models are "take the open source and wrap it," per Jameson's instruction. The YNAB-writing path is necessarily new code, because its job (enrich an existing transaction, never create one) is different in kind from what YNAB_AMAZON does — that's an architecture mismatch, not a laziness-about-reuse question.

## 2. Architecture

**Single FastAPI + Uvicorn app, APScheduler running in-process** (`BackgroundScheduler`, not asyncio — Selenium is blocking and must not stall the dashboard's event loop). Splitting scraper/parser/matcher into separate services isn't justified for a single-user tool; a hard per-run timeout + watchdog protects against a hung Chrome session wedging the scheduler.

**Docker Compose as the default deployment**: `app` container + `selenium/standalone-chrome` container over remote WebDriver.
- Isolates Chrome from macOS's own Chrome auto-updates (a frequent Selenium breakage source).
- Matches Jameson's later goal of possibly moving this to a Pi/NAS — remote WebDriver over Compose ports unchanged.
- `restart: unless-stopped` + Docker Desktop "start at login" covers reboot survival.
- Practical compromise: develop the scraper first (Phase 1) against local `chromedriver_autoinstaller` with a visible browser for fast selector-debugging iteration; containerize starting Phase 4 once scraper logic is stable. A `launchd` LaunchAgent plist is included as a documented non-Docker fallback.

## 3. File layout

```
Amazon-Receipts/
  vendor/
    amazon_orders_webscraper/  # git-cloned from aelzeiny/Amazon-Orders-WebScraper, kept close to original
    ynab_amazon/                # git-cloned from aelzeiny/YNAB_AMAZON, kept close to original
  app/
    main.py                    # FastAPI app; mounts dashboard; starts scheduler on lifespan
    config.py                  # pydantic Settings from .env
    db.py                      # sqlite3 connection + schema init/migration
    models.py                  # Receipt, Item, MatchCandidate (adapted from vendor/ynab_amazon/models.py)
    scheduler.py               # APScheduler cron job registration; "Run Now" hook
    pipeline.py                # run_pipeline(): scrape -> parse -> match; writes pipeline_runs row
    logging_setup.py           # rotating file handler + in-memory ring buffer for dashboard log tail
    scraper/
      driver.py                # remote Selenium (Docker) or local chromedriver_autoinstaller
      wrapper.py                # thin wrapper calling vendor/amazon_orders_webscraper/pages.py classes
                                 # directly; swaps hardcoded env-var reads for app/config.py settings
    parsing/
      llm_client.py            # AnthropicProvider + OpenAICompatibleProvider, one interface
                                 # (prompt/schema shape ported from vendor/ynab_amazon/gpt.py)
      receipt_parser.py        # HTML-strip (reused from vendor/ynab_amazon/main.py) -> structured parse
      categories.py            # YNAB category cache (all real, non-hidden categories,
                                 # no opt-in group gate) + FIXED category resolver
                                 # (get_categories reused from vendor/ynab_amazon/ynab.py; bug fixed)
    ynab/
      client.py                # get_transactions(account, since_date) [new], get_categories [reused],
                                 # patch_transaction [new — post_transaction from vendor is NOT reused]
      matcher.py                # candidate-finding: window/amount/payee/uncategorized filters (§5)
      apply.py                  # NEW — atomic claim, apply, reset, re-apply logic (§5, §7)
    dashboard/
      routes.py                # Home, Review, History, Receipt Detail routes
      templates/{base,index,review,history,receipt_detail}.html
      static/style.css
  data/
    receipts_html/             # raw saved receipt HTML (gitignored)
    app.db                     # sqlite (gitignored)
    logs/app.log
  scripts/
    verify_scrape.py           # Phase 1 manual check
    verify_parse.py            # Phase 2 manual check
    reprocess_order.py         # CLI: --order-id X --action {reset|reapply} — calls the SAME
                                 # app/ynab/apply.py functions the dashboard uses, so there is one code path
    init_db.py
  tests/
    test_matching.py
    test_apply.py              # covers the atomic-claim / idempotent-approve / reset transitions
    test_parsing.py
  .env.example
  .gitignore                   # .env, data/, __pycache__
  requirements.txt
  Dockerfile
  docker-compose.yml
  launchd/com.user.amazonreceipts.plist
  README.md                    # credits both vendored upstream repos (see licensing note in Context)
```

## 4. SQLite schema

Adds an explicit "what did we actually touch" record separate from the *proposed* match, an apply counter, a short-lived claim/lock state, and an append-only audit log — this is what makes duplicate-prevention and safe re-push/reprocess (§7) concrete rather than assumed.

```sql
CREATE TABLE amazon_orders (
    order_id TEXT PRIMARY KEY,
    order_date DATE,
    html_path TEXT NOT NULL,
    scraped_at DATETIME NOT NULL,

    parsed_json TEXT,
    parse_status TEXT NOT NULL DEFAULT 'pending'
        CHECK(parse_status IN ('pending','parsed','error')),
    parse_error TEXT,
    parsed_at DATETIME,
    grand_total_cents INTEGER,

    match_status TEXT NOT NULL DEFAULT 'pending_parse'
        CHECK(match_status IN
          ('pending_parse','pending_review','applying','approved',
           'no_candidate','ambiguous','error','rejected')),
                                      -- 'applying' is a short-lived claim state, see apply.py below
    candidate_ynab_txn_ids TEXT,      -- JSON array of {id, date, amount, payee} for the ambiguous case
    selected_ynab_txn_id TEXT,        -- proposed candidate, set by the matcher, pre-apply
    ynab_patch_payload TEXT,          -- most recent full payload sent (overwritten each apply)
    matched_at DATETIME,

    approved_at DATETIME,
    ynab_transaction_id_patched TEXT, -- the txn actually PATCHed on the last successful apply
                                       -- (distinct from selected_ynab_txn_id, which is pre-apply/proposed)
    ynab_patched_at DATETIME,
    ynab_patch_error TEXT,
    apply_count INTEGER NOT NULL DEFAULT 0,  -- 0 = never applied; 1 = normal; 2+ visibly flags a
                                              -- dev re-apply happened, surfaced on Home (§6)

    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Append-only, one row per PATCH attempt — never edited, only inserted. This table alone can
-- always answer "has this order been applied, to which transaction, when, first-time-or-retest."
CREATE TABLE ynab_apply_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL REFERENCES amazon_orders(order_id),
    ynab_transaction_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,             -- exact PATCH body sent, for later diffing
    is_reapply BOOLEAN NOT NULL DEFAULT 0,  -- 0 = first apply, 1 = explicit dev re-apply/overwrite
    success BOOLEAN NOT NULL,
    error_message TEXT,
    applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE pipeline_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at DATETIME NOT NULL,
    finished_at DATETIME,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK(status IN ('running','success','partial','error')),
    orders_found INTEGER DEFAULT 0,
    orders_parsed INTEGER DEFAULT 0,
    orders_matched INTEGER DEFAULT 0,
    error_message TEXT
);
```

## 5. Matching + apply algorithm

**Candidate-finding** (`app/ynab/matcher.py`) — for each row with `parse_status='parsed'` and `match_status='pending_parse'`:

1. `window = order_date ± YNAB_MATCH_WINDOW_DAYS` (default 5).
2. `GET /v1/budgets/{budget_id}/accounts/{account_id}/transactions?since_date={window_start}`, scoped to the specific dedicated account (`YNAB_ACCOUNT_ID`) — never budget-wide.
3. Filter locally (YNAB's API has no upper-bound date filter):
   - `date <= window_end`
   - `abs(amount) == abs(grand_total_milliunits)` (exact match)
   - not deleted
   - `category_id is None` (default on — matches the "no useful data yet" problem statement; configurable)
   - `payee_name` contains one of `YNAB_AMAZON_PAYEE_FILTERS` (default on; configurable)
   - **not already bound**: exclude any transaction that is `ynab_transaction_id_patched` on another order with `match_status='approved'` — prevents two receipts claiming the same bank transaction
4. Result:
   - **0 candidates** → `no_candidate`. Expected, safe outcome when Amazon splits one order into multiple shipment charges — v1 does not attempt combinatorial sum-matching across several transactions (real, documented limitation, flagged as a v2 idea, not solved here due to false-positive risk).
   - **1 candidate** → `pending_review`; `selected_ynab_txn_id` and `ynab_patch_payload` staged, but it **still requires a manual Approve** — one candidate is not the same as a safe auto-apply.
   - **2+ candidates** → `ambiguous`; all candidates stored with full date/amount/payee for the dashboard to render; no auto-selection.

**Payload construction** (category bug fixed):
- 1 item → set top-level `memo`/`category_id` directly, no subtransactions.
- 2+ items → `subtransactions[]` (one per item, price prorated against `grand_total` for tax/discount, milliunits, last item absorbs cent-rounding drift so the sum matches exactly), top-level `memo = "Amazon order {order_id}"`.
- Category resolution is a pure function returning the matched category or `None` — no line may reassign it back to `None` after a match (this is the exact fix for the upstream bug: reused GET calls, rewritten resolver).
- **PATCH always sends the full desired state** (complete `memo`/`subtransactions`, never a partial/append) — this is what makes re-apply an overwrite rather than a stack/double-split.

**Apply step** (`app/ynab/apply.py`) — the actual guarded routine invoked by both the dashboard's Approve button and `scripts/reprocess_order.py`, so there is exactly one code path for writing to YNAB:

```python
def apply_patch(order_id: str, allow_reapply: bool = False) -> ApplyResult:
    # 1. Duplicate guard — refuse before touching YNAB at all.
    row = get_order(order_id)
    if row.match_status == "approved" and row.ynab_transaction_id_patched and not allow_reapply:
        return AlreadyApplied(row.ynab_transaction_id_patched, row.approved_at)  # no-op

    # 2. Atomic claim — a single UPDATE...WHERE is what makes Approve idempotent against a
    #    double-click OR an overlapping scheduler run. Whichever caller's UPDATE actually moves
    #    a row wins; the other gets rowcount == 0 and backs off instead of racing to PATCH twice.
    allowed_from = ("pending_review",) if not allow_reapply else ("pending_review", "approved")
    claimed = db.execute(
        "UPDATE amazon_orders SET match_status='applying' WHERE order_id=? AND match_status IN (...)",
        order_id, *allowed_from,
    )
    if claimed.rowcount == 0:
        return AlreadyProcessing()  # someone else already claimed this order_id

    # 3. Re-fetch the transaction fresh — guards against it being edited/reconciled/deleted
    #    since the matcher last saw it.
    txn_id = row.selected_ynab_txn_id
    fresh_txn = ynab_client.get_transaction(txn_id)
    if fresh_txn.deleted:
        return mark_error(order_id, "transaction was deleted since match")

    # 4. Final double-claim check at write time, not just at match time (state can change
    #    between matching and approval).
    other = find_order_already_bound_to(txn_id, exclude=order_id)
    if other:
        return mark_error(order_id, f"transaction already claimed by order {other.order_id}")

    # 5. Build the FULL desired end-state from parsed_json every time and PATCH it.
    payload = build_full_replacement_payload(row.parsed_json)
    result = ynab_client.patch_transaction(txn_id, payload)

    # 6. Log the attempt unconditionally — success or failure, first-apply or re-apply.
    log_apply_attempt(order_id, txn_id, payload, is_reapply=allow_reapply, result=result)

    if result.ok:
        mark_approved(order_id, txn_id, payload)   # sets ynab_transaction_id_patched, apply_count+=1
    else:
        mark_error(order_id, result.error)
```

**Allowed state transitions** (explicit, so no dashboard/CLI action can reach an invalid state):

```
pending_parse  -> pending_review | ambiguous | no_candidate | error      (matcher)
ambiguous      -> pending_review                                          (manual candidate pick)
pending_review -> applying -> approved | error                            (apply_patch, first apply)
approved       -> applying -> approved | error   [dev only, allow_reapply=True]
approved | rejected | error -> pending_review | pending_parse             [dev only, reset_order()]
```

The two "dev only" transitions are gated behind config flags, off by default (§8) — this is a safety-bypass tool and shouldn't be reachable by accident against a live budget.

## 6. Dashboard pages (Jinja2, server-rendered — a status/table UI needs no JS framework)

1. **Home (`/`)** — last run time/status, next scheduled run, Run Now button, Amazon-login health, YNAB-connectivity health, counts linking into Review/Ambiguous/No-Candidate, and an "N orders re-applied during testing" diagnostic (from `apply_count > 1`) so a stray dev re-apply against a real transaction doesn't go unnoticed.
2. **Pending Review (`/review`)** — `pending_review` items show the parsed line items *and* the actual candidate YNAB transaction's date/amount/payee side by side (approval is checked against two independent records, not the parse alone). Approve posts to the guarded `apply_patch()` — a double-click or refresh-and-resubmit renders an inline "already handled" notice instead of erroring. `ambiguous` items show all candidates with a "pick this one" button, which demotes them to `pending_review`.
3. **History (`/history`)** — every processed order with status/date/amount, plus the `pipeline_runs` log tail.
4. **Receipt Detail (`/receipts/{order_id}`)** — parsed JSON, staged/sent payload, an "Applied N×" badge with `ynab_transaction_id_patched`/`ynab_patched_at`, a mini history table sourced from `ynab_apply_log` (timestamp, transaction id, first-apply vs. re-apply, success/error, expandable payload), link to the raw saved HTML and to the YNAB transaction, and the two reprocess controls from §7 — each hidden entirely unless its config flag is on.

## 7. Re-push / reprocess semantics (for testing, and for real corrections)

Two distinct, clearly-labeled actions — deliberately not one button, because they carry different risk, and both call the exact same `app/ynab/apply.py` functions used by the dashboard and by `scripts/reprocess_order.py` (one code path, whether triggered from the GUI or the CLI):

- **"Reset to Pending"** (visible when `match_status` ∈ {approved, rejected, error} and `ALLOW_RESET=true`) — a dropdown picks the target: `pending_review` (re-run the matcher against the existing `parsed_json`, fast iteration on matching logic without re-parsing) or `pending_parse` (force a full re-parse, for testing LLM prompt/schema changes). This **never touches YNAB** — it only resets local state. Crucially, it does **not** clear `ynab_transaction_id_patched`/`ynab_patched_at`/`apply_count`/`ynab_apply_log` — that history stays visible on Receipt Detail even after reset, so mid-test you can see "this was applied once already, at 2:14pm, to txn X" while re-running it.
- **"Force Re-Apply (overwrite YNAB transaction)"** (visible only when `match_status='approved'` and `ALLOW_REAPPLY=true`) — requires an explicit confirmation checkbox ("I understand this will overwrite this transaction's memo/category split with the current parsed data") before submitting; calls `apply_patch(order_id, allow_reapply=True)`. Safe by construction: same target transaction (`ynab_transaction_id_patched`), full-replacement PATCH, so re-applying never stacks or double-splits — it overwrites, matching YNAB's own PATCH semantics.
- Every attempt from either path is logged in `ynab_apply_log`, so testing repeatedly during development never loses the audit trail of what was actually sent to YNAB and when.

## 8. `.env` variables

```
# Amazon — AMAZON_TOTP_SECRET fully bypasses 2FA; the single most sensitive value here
AMAZON_EMAIL=
AMAZON_PASSWORD=
AMAZON_TOTP_SECRET=

# YNAB
YNAB_PERSONAL_ACCESS_TOKEN=
YNAB_BUDGET_ID=last-used
YNAB_ACCOUNT_ID=
YNAB_MATCH_WINDOW_DAYS=5
YNAB_ONLY_MATCH_UNCATEGORIZED=true
YNAB_AMAZON_PAYEE_FILTERS=Amazon,AMZN

# LLM — pluggable
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=
ANTHROPIC_MODEL=claude-haiku-4-5
OPENAI_COMPATIBLE_BASE_URL=http://localhost:11434/v1
OPENAI_COMPATIBLE_API_KEY=
OPENAI_COMPATIBLE_MODEL=llama3.1

# Scraper
SELENIUM_REMOTE_URL=                # empty = local chromedriver_autoinstaller
SCRAPE_HEADLESS=true

# Dev-only safety bypasses — leave false in whatever .env backs the scheduled/production run
ALLOW_RESET=false     # enables "Reset to Pending" in the dashboard/CLI
ALLOW_REAPPLY=false   # enables "Force Re-Apply" — overwrites an already-applied YNAB transaction

# App
PIPELINE_SCHEDULE_CRON=0 7 * * *
DATABASE_PATH=./data/app.db
RECEIPTS_DIR=./data/receipts_html
DASHBOARD_PORT=8420
LOG_LEVEL=INFO
```

`.env` must be `.gitignore`'d and `chmod 600`. Never commit it; consider macOS Keychain migration later as a hardening pass, not required for v1.

## 9. Phased build order

- **Phase 0 — Scaffold**: repo layout, `.gitignore`, `.env.example`, empty modules; `git clone` both upstream repos into `vendor/`, credit them in `README.md`, initial commit.
- **Phase 1 — DB + scraper**: `db.py`/`init_db.py`; `app/scraper/wrapper.py` calling directly into `vendor/amazon_orders_webscraper/pages.py`'s page-object classes for login → order discovery → receipt fetch, dedup by `order_id`. Develop against local `chromedriver_autoinstaller` (visible browser) for fast iteration. Verify with `scripts/verify_scrape.py`: login incl. OTP succeeds, N receipts saved, N DB rows inserted. No parsing/matching yet.
- **Phase 2 — Parsing**: `app/models.py` adapted from `vendor/ynab_amazon/models.py` (bug fixed), `llm_client.py` (both providers behind one interface, prompt/schema ported from `vendor/ynab_amazon/gpt.py`), `receipt_parser.py` (HTML-strip reused from `vendor/ynab_amazon/main.py`), `categories.py` (`get_categories` GET reused from `vendor/ynab_amazon/ynab.py`, resolver rewritten). Verify with `scripts/verify_parse.py` against already-saved receipts; manually review parse accuracy before automating.
- **Phase 3 — Matching + apply + dashboard**: `ynab/client.py` (new `get_transactions`/`patch_transaction`, reused `get_categories`), `ynab/matcher.py`, `ynab/apply.py` (atomic claim, apply, reset, re-apply — §5, §7), the 4 dashboard pages, `scripts/reprocess_order.py`. End-to-end manual test: scrape→parse→match via CLI, approve one item in the dashboard, confirm in the actual YNAB app that the transaction updated correctly. Exercise both reprocess actions from §7 here specifically — including via a double-click and a manually-forced overlapping call to `apply_patch()` — and confirm `ynab_apply_log` shows exactly the expected number of PATCHes with no order ever landing on two different transactions.
- **Phase 4 — Scheduler + deployment**: `scheduler.py` (cron + "Run Now" via threadpool so it doesn't block the dashboard), Dockerfile + `docker-compose.yml` (`selenium/standalone-chrome` + app, volumes for `data/`+`.env`), `restart: unless-stopped`, Docker Desktop "start at login." `launchd` plist as documented non-Docker fallback. Final smoke test: `docker compose down && up` (or a reboot), confirm dashboard reachable and the next scheduled run still fires.

## 10. Risks (explicit, not glossed over)

1. **Amazon bot-detection / lockout** — accepted knowingly. Mitigate with a persistent browser profile/cookie dir across runs (fewer full-OTP re-triggers), daily-not-more-often schedule, and pausing the schedule at the first sign of repeated login failures.
2. **LLM sending PII off-device** — receipt HTML includes name/address. Real for the cloud Claude path; the local Ollama/vLLM option exists specifically so this can be avoided. Phase 2 hardening: strip address/name blocks before sending to any LLM, keep only line items + totals.
3. **Matching false positives** — amount+date+payee filtering reduces but doesn't eliminate coincidence. The Pending Review page showing the actual candidate transaction next to the parsed receipt (§6) is the real mitigation.
4. **Split-shipment orders** — documented in §5; v1 fails safe to `no_candidate` rather than guessing across multiple transactions.
5. **YNAB write conflicts** — mitigated by the re-fetch-before-PATCH step inside `apply_patch()` (§5).
6. **Chrome/chromedriver breakage on macOS auto-updates** — the concrete reason Docker's `selenium/standalone-chrome` is the recommended default over host Chrome.
7. **Credential exposure** — `.env` holds the Amazon password + TOTP secret + YNAB token + LLM key together. `chmod 600`, `.gitignore`, keep `data/`/`.env` out of any cloud-sync/backup path.
8. **Unlicensed vendored code** — both upstream repos ship with no declared license; vendoring them is a personal-use judgment call, not a legally clean reuse grant (see Context). Revisit before ever publishing this app's source.

## Verification

- Phase 1: `python scripts/verify_scrape.py` — confirms login success and receipt files/DB rows exist.
- Phase 2: `python scripts/verify_parse.py` — prints parsed JSON for saved receipts; manually eyeball accuracy against a few real receipts before trusting it.
- Phase 3: run the full pipeline once via CLI, approve a real item in the dashboard, then open the YNAB app/web UI directly and confirm the transaction now shows the correct memo/split/category. Exercise both reprocess actions in §7 and confirm `ynab_apply_log` records each attempt without a second transaction ever being touched unintentionally; confirm a double-click on Approve produces exactly one PATCH.
- Phase 4: `docker compose up`, browse to `http://localhost:8420`, confirm Home shows healthy status; stop and restart the stack (or reboot) and confirm the scheduled job still runs on the configured cron.
