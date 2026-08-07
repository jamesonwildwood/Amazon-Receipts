"""Cross-process exclusion for the run lock (docs/IMPROVEMENTS.md 3.7). The
in-process threading.Lock this replaced only ever guarded threads within one
Python process; a CLI run (app/__main__.py) and the server's scheduled run
are separate processes, so the real test has to spawn one."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import db, pipeline
from app.config import settings

REPO_ROOT = str(Path(__file__).resolve().parents[1])

# Run as `python -c <script> <sleep_seconds>` in a fresh process: acquires the
# pipeline lock, announces success on stdout (so the parent can synchronize
# without a fixed sleep-and-hope), holds it briefly, then releases.
_HOLD_LOCK_SCRIPT = """
import sys, time
sys.path.insert(0, {repo_root!r})
from app import pipeline

lock_file = pipeline._acquire_lock()
if lock_file is None:
    print("FAILED_TO_ACQUIRE", flush=True)
    sys.exit(1)
print("ACQUIRED", flush=True)
time.sleep(float(sys.argv[1]))
pipeline._release_lock(lock_file)
""".format(repo_root=REPO_ROOT)


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "test.db"))
    db.init_db()
    yield


def test_second_process_is_refused_while_first_holds_the_lock(temp_db, monkeypatch):
    monkeypatch.setattr(pipeline, "load_accounts", lambda: [])  # a real run_pipeline() must not scrape

    env = dict(os.environ, DATABASE_PATH=settings.database_path)
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK_SCRIPT, "1.5"],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        line = proc.stdout.readline().strip()
        assert line == "ACQUIRED", f"subprocess failed to acquire the lock: {line!r}"

        # A second, in-process run must be refused while the subprocess holds it.
        result = pipeline.run_pipeline()
        assert result is None
    finally:
        proc.wait(timeout=5)

    assert proc.returncode == 0

    # Now that the subprocess released it, a real call succeeds.
    result = pipeline.run_pipeline()
    assert result is not None
