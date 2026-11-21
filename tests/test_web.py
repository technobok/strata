"""strata-web: the gunicorn master must never import duckdb.

Importing duckdb starts its thread pool, and threads do not survive fork(): a
gunicorn worker forked from such a master segfaulted (in the container) or hung
for the 30 s grace period (on a host) when it exited. The master reads its
settings only; each worker builds the app after the fork.
"""

import os
import subprocess
import sys
from pathlib import Path

MASTER = """
import sys
import gunicorn.app.base as base

def run(self):  # instead of serving: report what the master holds
    print("duckdb imported:", "duckdb" in sys.modules)
    print("preload:", self.cfg.preload_app)
    print("bind:", self.cfg.bind)

base.BaseApplication.run = run
from strata.web import main
main(["--port", "5099"], standalone_mode=False)
"""


def test_the_gunicorn_master_never_imports_duckdb(tmp_path: Path) -> None:
    from strata.db import init_db_at

    db = tmp_path / "strata.sqlite3"
    init_db_at(str(db))
    env = {
        **os.environ,
        "STRATA_DB": str(db),
        "DATA_ROOT": str(tmp_path / "data"),
        "HOST": "127.0.0.1",
    }
    env.pop("GATEKEEPER_DB", None)
    out = subprocess.run(
        [sys.executable, "-c", MASTER], env=env, capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == [
        "duckdb imported: False",
        "preload: False",
        "bind: ['127.0.0.1:5099']",
    ]
