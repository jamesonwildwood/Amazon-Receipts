# Server deploy prompt

Run this ON the home server, in the repo checkout, after `git pull`:

```bash
claude -p "$(cat docs/SERVER_DEPLOY.md)" --model sonnet
```

---

You are Claude Code running on the home server that hosts the Amazon-Receipts
Docker deployment, inside the repo checkout. Deploy the latest code and fix
this server's known-broken config. Findings from 2026-08-15 you are acting on:
every nightly run since 2026-08-07 failed at the YNAB step with 401
Unauthorized — this server's `.env` has a stale/revoked
`YNAB_PERSONAL_ACCESS_TOKEN` **and** a wrong `YNAB_BUDGET_ID`
(`c9b9b315-9083-4a23-8330-2a3e717f2fce`; the correct budget is
`e70bd7dc-c995-4d13-af72-8b35777d5e3a`). Because every run died before
touching YNAB, this server has never applied anything — its SQLite has no
approvals worth preserving.

Steps, in order — stop and report plainly at any blocker rather than
improvising:

1. `git pull --ff-only` on main; confirm HEAD includes commit 0a71d0c
   ("Skip sign-in when the persisted Chrome profile still holds a live
   session") or later.
2. Fix `.env`: set `YNAB_BUDGET_ID=e70bd7dc-c995-4d13-af72-8b35777d5e3a`.
   The YNAB token you cannot invent — if the operator hasn't already put a
   valid token in `.env`, stop and ask for it (it can be copied from the
   Mac's working `.env` or minted at app.ynab.com developer settings).
   Never print the token.
3. Confirm `amazon_accounts.toml` exists here with BOTH accounts (jameson,
   aesylvatica) and is chmod 600. It holds secrets and is not in git — if
   missing or single-account, stop and ask the operator to copy it from the
   Mac. Do not print its contents.
4. If the operator staged a database sync from the Mac (look for
   `/tmp/mac-sync/app.db` and `/tmp/mac-sync/receipts_html/`): stop the
   stack (`docker compose down`), back up `data/app.db` to
   `data/app.db.bak-<date>`, replace it with the Mac copy, and rsync the
   receipts into `data/receipts_html/`. Replacement (not row-merge) is safe
   ONLY because this server has zero applies — verify that first:
   `sqlite3 data/app.db "SELECT COUNT(*) FROM ynab_apply_log WHERE success=1"`
   must be 0; if it isn't, stop and report.
5. Migrate the Chrome-profile volume to the per-account layout (new code
   expects `<profile>/<label>`): with the stack down, run
   `docker compose run --rm --user 1200 --entrypoint sh selenium -c 'mkdir -p /home/seluser/chrome-profile/jameson && find /home/seluser/chrome-profile -maxdepth 1 -mindepth 1 ! -name jameson -exec mv {} /home/seluser/chrome-profile/jameson/ \;'`
6. `docker compose up -d --build`, then verify with
   `scripts/test_ynab_connection.py` inside the app container (it lists
   budgets/accounts; a 401 here means the token is still wrong — stop).
7. Trigger a run: `docker compose exec app python -m app run` (or the
   dashboard's Run Now). Tail `data/logs/app.log`. Expected: jameson scrapes
   clean; aesylvatica's FIRST scrape from this box may hit an Amazon
   challenge since the container has never seen her login — if her scrape
   fails at sign-in, report it and point the operator at the selenium
   container's noVNC (port 7900) to complete the login once by hand.
8. Report: pipeline run summary, per-account scrape results, YNAB
   connectivity status, next scheduled run time, and anything deferred.

Hard rules: never write to YNAB (no approvals, no re-applies, no
create-transaction — the pipeline's scrape/parse/match is fine); never print
secrets; don't edit code — this is a deploy, and code fixes belong in a PR
from the dev machine.
