"""CLI entrypoint (docs/IMPROVEMENTS.md 3.6): ad-hoc pipeline runs alongside
the existing scheduled/dashboard server, without the two stepping on each
other (docs/IMPROVEMENTS.md 3.7 -- the cross-process file lock in
app/pipeline.py is what actually enforces that; this module is just the
argument parsing and exit-code mapping on top of it).

    python -m app run                    # one pipeline pass, all accounts
    python -m app run --account jameson  # scrape just one configured account
    python -m app run --headful          # visible browser (first login / challenge)
    python -m app serve                  # dashboard + scheduler (same as
                                          # `uvicorn app.main:app`, which keeps working too)
"""

import argparse
import sys

from app.config import settings
from app.db import get_run, init_db, mark_stale_runs_as_error
from app.logging_setup import configure_logging
from app.pipeline import STALE_RUN_AFTER_HOURS, run_pipeline

# python -m app run exit codes, per docs/IMPROVEMENTS.md 3.6: cron/scripts can
# react to these without parsing stdout.
EXIT_SUCCESS = 0
EXIT_PARTIAL = 1
EXIT_ERROR = 2

_RUN_STATUS_EXIT_CODES = {"success": EXIT_SUCCESS, "partial": EXIT_PARTIAL, "error": EXIT_ERROR}


def _run(args: argparse.Namespace) -> int:
    configure_logging()
    init_db()
    # Same stale-run sweep app/main.py does at server startup -- a CLI run
    # can otherwise be permanently refused by a 'running' row a crashed
    # process left behind (docs/IMPROVEMENTS.md item 3).
    mark_stale_runs_as_error(STALE_RUN_AFTER_HOURS)

    # --headful overrides SCRAPE_HEADLESS for just this run (None below means
    # "use the configured setting" -- run_pipeline/build_driver treat None
    # and "use settings.scrape_headless" identically).
    headless = False if args.headful else None

    run_id = run_pipeline(account_label=args.account, headless=headless)
    if run_id is None:
        print("Another pipeline run is already in progress (data/pipeline.lock is held) -- skipped.")
        return EXIT_ERROR

    run = get_run(run_id)
    if run is None:
        # Should not happen -- run_pipeline() always writes its own row before
        # returning an id -- but never crash the CLI over a missing summary.
        print(f"Run #{run_id} completed but its record could not be found.")
        return EXIT_ERROR

    print(
        f"Run #{run_id}: {run['status']} -- "
        f"found {run['orders_found']}, parsed {run['orders_parsed']}, matched {run['orders_matched']}"
    )
    if run["error_message"]:
        print(f"  {run['error_message']}")

    return _RUN_STATUS_EXIT_CODES.get(run["status"], EXIT_ERROR)


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("app.main:app", host=args.host, port=settings.dashboard_port)
    return EXIT_SUCCESS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run one pipeline pass (scrape -> parse -> match) and exit.")
    run_parser.add_argument(
        "--headful",
        action="store_true",
        help="Override SCRAPE_HEADLESS for this run -- watch the browser. Primary use: the first "
        "login on a new account almost always hits an Amazon challenge; a visible-browser run "
        "clicks through it and persists the Chrome profile.",
    )
    run_parser.add_argument(
        "--account",
        metavar="LABEL",
        default=None,
        help="Scrape only this configured account label (default: every configured account).",
    )
    run_parser.set_defaults(func=_run)

    serve_parser = subparsers.add_parser(
        "serve", help="Run the dashboard + scheduler (equivalent to `uvicorn app.main:app`)."
    )
    serve_parser.add_argument("--host", default="0.0.0.0")
    serve_parser.set_defaults(func=_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
