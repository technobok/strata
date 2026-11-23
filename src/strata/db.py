"""Database connection and transaction handling using APSW."""

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import apsw

_standalone_db: apsw.Connection | None = None

# Highest migration version this code expects; checked at app boot. The containers
# run `strata-admin init-db` before starting, so a lower value on disk means the
# migration step did not run; the runtime then aborts with a clear message. Bump
# this when adding a new migration file.
EXPECTED_SCHEMA_VERSION = 8

# Tables only the Strata from before datamodules had (its database/schema.sql,
# removed in 9953905). Its schema cannot be migrated: the baseline's
# CREATE TABLE IF NOT EXISTS would skip the same-named old tables and fail on
# their missing columns. `strata-admin adopt-prepivot` replaces such a database.
PREPIVOT_TABLES = frozenset({"report_parameter", "report_access", "report_tag", "api_link"})


class UnusableDatabase(RuntimeError):
    """init-db refuses a file that holds tables but no record of Strata's
    migrations: Strata did not make it, so migrating it would fail half-way."""


def get_db_path() -> str:
    """Resolve the database path.

    Priority:
      1. STRATA_DB environment variable
      2. Flask current_app.config["DATABASE_PATH"] (if in app context)
      3. $STRATA_ROOT/instance/strata.sqlite3 (if STRATA_ROOT is set)
      4. Source-tree fallback (only when the package lives inside a checkout)
      5. CWD/instance/strata.sqlite3
    """
    import os

    db_path = os.environ.get("STRATA_DB")
    if db_path:
        return db_path

    try:
        from flask import current_app

        return current_app.config["DATABASE_PATH"]
    except RuntimeError, KeyError:
        pass

    if os.environ.get("STRATA_ROOT"):
        return str(Path(os.environ["STRATA_ROOT"]) / "instance" / "strata.sqlite3")

    source_root = Path(__file__).parent.parent.parent
    if (source_root / "src" / "strata" / "__init__.py").exists():
        return str(source_root / "instance" / "strata.sqlite3")

    return str(Path.cwd() / "instance" / "strata.sqlite3")


def _configure_connection(conn: apsw.Connection) -> None:
    """Apply standard PRAGMAs to a connection."""
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.execute("PRAGMA foreign_keys = ON;")
    _enable_wal(conn)


def _enable_wal(conn: apsw.Connection, timeout_seconds: float = 5.0) -> None:
    """Switch the DB to WAL, retrying while another connection holds a write lock.

    Moving a fresh (rollback-journal) file to WAL while another connection is
    switching it too, or holds a write transaction (as the migration runner's
    BEGIN IMMEDIATE does), returns SQLITE_BUSY at once: SQLite does not wait in
    the busy handler for it. That happens on a first deploy, where the app and
    worker containers both create the DB on start. Once the file is in WAL the
    pragma is a no-op that never contends.
    """
    import time

    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            conn.execute("PRAGMA journal_mode = WAL;")
            return
        except apsw.BusyError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def get_db() -> apsw.Connection:
    """Get the database connection for the current request (Flask context)."""
    from flask import g

    if "db" not in g:
        db_path = get_db_path()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        g.db = apsw.Connection(db_path)
        _configure_connection(g.db)
    return g.db


def close_db(e: BaseException | None = None) -> None:
    """Close the database connection at the end of the request."""
    from flask import g

    db = g.pop("db", None)
    if db is not None:
        db.close()


# ---------------------------------------------------------------------------
# Standalone DB access (no Flask context required)
# ---------------------------------------------------------------------------


def get_standalone_db() -> apsw.Connection:
    """Get a database connection without Flask context.

    Used by CLI commands and the worker process.
    The connection is cached at module level.
    """
    global _standalone_db
    if _standalone_db is None:
        db_path = get_db_path()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        _standalone_db = apsw.Connection(db_path)
        _configure_connection(_standalone_db)
    return _standalone_db


def close_standalone_db() -> None:
    """Close the standalone database connection."""
    global _standalone_db
    if _standalone_db is not None:
        _standalone_db.close()
        _standalone_db = None


@contextmanager
def standalone_transaction() -> Generator[apsw.Cursor]:
    """Transaction context manager for standalone (non-Flask) DB access."""
    db = get_standalone_db()
    cursor = db.cursor()
    cursor.execute("BEGIN IMMEDIATE;")
    try:
        yield cursor
        cursor.execute("COMMIT;")
    except Exception:
        cursor.execute("ROLLBACK;")
        raise


# ---------------------------------------------------------------------------
# Flask-context transactions
# ---------------------------------------------------------------------------


@contextmanager
def transaction() -> Generator[apsw.Cursor]:
    """Context manager for database transactions.

    Automatically commits on success, rolls back on exception.
    """
    db = get_db()
    cursor = db.cursor()
    cursor.execute("BEGIN IMMEDIATE;")
    try:
        yield cursor
        cursor.execute("COMMIT;")
    except Exception:
        cursor.execute("ROLLBACK;")
        raise


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------


def _migrations_dir() -> Path:
    """Locate database/migrations/ across dev and installed-package layouts."""
    from strata.paths import repo_dir

    return repo_dir("database/migrations")


def _migration_files() -> list[tuple[int, Path]]:
    """Return (version, path) for every NNNN_*.sql migration, ordered by version."""
    files: list[tuple[int, Path]] = []
    for path in _migrations_dir().glob("*.sql"):
        stem = path.name.split("_", 1)[0]
        try:
            version = int(stem)
        except ValueError:
            continue
        files.append((version, path))
    files.sort(key=lambda vp: vp[0])
    return files


def apply_migrations(conn: apsw.Connection) -> int:
    """Apply pending forward-only migrations; return the max applied version.

    Each database/migrations/NNNN_name.sql file is applied once, in version
    order, inside its own transaction, and recorded in schema_migrations.
    db_metadata.schema_version is stamped with the max applied version for the
    boot-time schema gate (_check_schema_version).

    Safe to run concurrently (the app and worker containers both migrate on
    start): each version is re-checked inside its BEGIN IMMEDIATE, so the write
    lock serialises runners and a version another runner applied in the
    meantime is skipped rather than applied twice.
    """
    from datetime import UTC, datetime

    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT)"
    )
    applied = {
        int(v)
        for (v,) in conn.execute("SELECT version FROM schema_migrations").fetchall()
        if v is not None
    }

    max_version = max(applied) if applied else 0
    for version, path in _migration_files():
        max_version = max(max_version, version)
        if version in applied:
            continue
        sql = path.read_text()
        now = datetime.now(UTC).isoformat()
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE;")
        try:
            if cursor.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
            ).fetchone():
                cursor.execute("COMMIT;")
                continue
            # Checked under the write lock: a runner racing us on a new file sees
            # our first migration recorded, not our tables without a record.
            if cursor.execute("SELECT 1 FROM schema_migrations LIMIT 1").fetchone() is None:
                _refuse_foreign_schema(cursor, conn.filename)
            for _ in cursor.execute(sql):
                pass
            cursor.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, now),
            )
            cursor.execute("COMMIT;")
        except Exception:
            cursor.execute("ROLLBACK;")
            raise

    conn.execute(
        "INSERT OR REPLACE INTO db_metadata (key, value) VALUES ('schema_version', ?)",
        (str(max_version),),
    )
    return max_version


def schema_tables(cursor: apsw.Cursor) -> list[str]:
    """The database's own tables: not SQLite's, not the migration ledger."""
    return [
        str(name)
        for (name,) in cursor.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'schema_migrations' ORDER BY name"
        ).fetchall()
    ]


def _refuse_foreign_schema(cursor: apsw.Cursor, db_path: str) -> None:
    """Before the first migration: a file that already holds tables was not made
    by Strata's migrations, and the baseline must not run over it."""
    tables = schema_tables(cursor)
    if not tables:
        return
    if PREPIVOT_TABLES & set(tables):
        raise UnusableDatabase(
            f"{db_path} was made by the Strata from before datamodules (it has the old "
            "report tables), and this Strata cannot use it. Its contents were not changed. "
            "Stop strata-web and strata-worker, then run `strata-admin adopt-prepivot`: it "
            "keeps this file as a backup and starts a new database with its settings and "
            'connections (README: "Upgrading from the Strata before datamodules").'
        )
    shown = ", ".join(tables[:5]) + (", ..." if len(tables) > 5 else "")
    raise UnusableDatabase(
        f"{db_path} holds tables ({shown}) but no record of Strata's migrations, so "
        "Strata did not make it. Its contents were not changed. Set STRATA_DB to a "
        "Strata database, or to a new file."
    )


def init_db_at(db_path: str) -> None:
    """Initialize / migrate the database at the given path (no Flask context).

    Idempotent: applies only pending migrations, then ensures a secret_key.
    """
    import secrets

    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = apsw.Connection(db_path)
    _configure_connection(conn)

    apply_migrations(conn)

    # Generate secret_key if absent (signs auth tokens; derives the Fernet key
    # for encrypted connection params).
    row = conn.execute("SELECT value FROM app_setting WHERE key = 'secret_key'").fetchone()
    if not row:
        new_key = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT OR IGNORE INTO app_setting (key, value, description) VALUES (?, ?, ?)",
            ("secret_key", new_key, "Secret key for signing auth tokens"),
        )

    conn.close()


def init_db() -> None:
    """Initialize the database with the schema (Flask context)."""
    db_path = get_db_path()
    init_db_at(db_path)


def get_schema_version() -> int:
    """Get the current schema version from db_metadata."""
    db = get_db()
    cursor = db.cursor()
    try:
        cursor.execute("SELECT value FROM db_metadata WHERE key = 'schema_version'")
        row = cursor.fetchone()
        return int(row[0]) if row else 0
    except apsw.SQLError:
        return 0
