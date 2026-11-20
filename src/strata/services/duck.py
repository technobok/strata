"""Every DuckDB connection that runs author SQL goes through this sandbox.

Strata runs SQL written by authors: build scripts, report bodies and
constraint queries. Each connection first does its trusted setup (ATTACH the
version file, dependencies and bound connections; LOAD extensions), then calls
``lock_down``. After that the connection can only touch the directories it was
given, cannot install or load extensions, and cannot change any setting back
(``lock_configuration``).

Behaviour probed on DuckDB 1.5.6:

- file reads, ``COPY ... TO``, ``ATTACH`` and ``glob`` outside the allowed
  directories raise ``PermissionException``; so do INSTALL and LOAD.
- databases ATTACHed before the lockdown stay queryable, and spilling works
  when ``temp_directory`` is inside an allowed directory (DuckDB creates the
  directory on demand and deletes it when the connection closes).
- GAP: once ``sqlite_scanner`` is loaded, ``ATTACH ... (TYPE sqlite)`` and
  ``sqlite_scan()`` bypass the file checks. It is therefore loaded only for a
  build that binds a sqlite connection, and never on the serve path.

The build sandbox can be switched off with the ``build.sandbox`` setting (an
escape hatch in case a connection driver turns out to need external access).
"""

import logging
import uuid
from collections.abc import Iterable
from pathlib import Path

import duckdb
from flask import current_app

log = logging.getLogger(__name__)

# Extensions loaded (best effort) before a build's lockdown, because a locked
# connection can no longer autoload them. excel provides read_xlsx.
BUILD_PRELOAD_EXTENSIONS = ("excel",)


class SqlShapeError(ValueError):
    """Author SQL that must be a single SELECT was something else."""


def _sql_list(paths: Iterable[Path | str]) -> str:
    quoted = ", ".join("'" + str(p).replace("'", "''") + "'" for p in paths)
    return f"[{quoted}]"


def _dir_prefix(path: Path) -> str:
    # A trailing slash makes the allowed prefix match only inside the directory
    # (without it, /data/ab would also allow /data/abc).
    return str(path).rstrip("/") + "/"


def lock_down(conn: duckdb.DuckDBPyConnection, directories: Iterable[Path] = ()) -> None:
    """Restrict ``conn`` to ``directories`` and freeze its configuration."""
    conn.execute("SET autoinstall_known_extensions = false")
    conn.execute("SET autoload_known_extensions = false")
    dirs = [_dir_prefix(d) for d in directories]
    if dirs:
        conn.execute(f"SET allowed_directories = {_sql_list(dirs)}")
    conn.execute("SET enable_external_access = false")
    conn.execute("SET lock_configuration = true")


def data_root() -> Path:
    return Path(current_app.config["DATA_ROOT"])


def exports_dir() -> Path:
    """Where the serve path writes COPY/xlsx output before streaming it."""
    d = data_root() / "_exports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _scratch_root() -> Path:
    d = data_root() / "_tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def serve_conn(version_file: Path) -> duckdb.DuckDBPyConnection:
    """An in-memory connection with ``version_file`` ATTACHed read-only as the
    default catalog, locked down to the export and spill directories."""
    conn = duckdb.connect(":memory:")
    try:
        conn.execute(f"ATTACH '{_escape(version_file)}' AS d (READ_ONLY)")
        conn.execute("USE d")
        scratch = _scratch_root()
        # Per-connection spill directory: DuckDB creates it on demand and
        # removes it on close, so concurrent connections never share files.
        conn.execute(f"SET temp_directory = '{_escape(scratch / uuid.uuid4().hex)}'")
        lock_down(conn, [exports_dir(), scratch])
    except Exception:
        conn.close()
        raise
    return conn


def build_sandbox_enabled() -> bool:
    return bool(current_app.config.get("BUILD_SANDBOX", True))


def preload_build_extensions(conn: duckdb.DuckDBPyConnection) -> None:
    """LOAD (installing first if needed) the extensions builds commonly use.

    Best effort: a failure is logged and the build carries on; a script that
    needs the extension then fails with a clear permission error.
    """
    for ext in BUILD_PRELOAD_EXTENSIONS:
        try:
            conn.execute(f"LOAD {ext}")
        except duckdb.Error:
            try:
                conn.execute(f"INSTALL {ext}")
                conn.execute(f"LOAD {ext}")
            except duckdb.Error as exc:
                log.warning("could not preload DuckDB extension %s: %s", ext, exc)


def lock_down_build(conn: duckdb.DuckDBPyConnection, dm_dir: Path) -> None:
    """Lock a build connection to its own datamodule's ``sources/`` and ``tmp/``.

    Called after dependencies and connections are attached. A no-op when the
    ``build.sandbox`` setting is off.
    """
    if not build_sandbox_enabled():
        return
    preload_build_extensions(conn)
    lock_down(conn, [dm_dir / "sources", dm_dir / "tmp"])


def validate_select(sql: str, what: str = "Query") -> None:
    """Require ``sql`` to be exactly one SELECT statement.

    Report bodies are pasted into ``CREATE MACRO ... AS TABLE (<body>)``; a
    body that closed the parenthesis could otherwise smuggle in more
    statements, which would then run on every serve.
    """
    try:
        statements = duckdb.extract_statements(sql)
    except duckdb.Error as exc:
        raise SqlShapeError(f"{what} does not parse: {exc}") from exc
    if len(statements) != 1:
        raise SqlShapeError(f"{what} must be a single SELECT statement.")
    if statements[0].type != duckdb.StatementType.SELECT:
        raise SqlShapeError(f"{what} must be a SELECT statement.")


def _escape(path: Path | str) -> str:
    return str(path).replace("'", "''")
