"""Database connection and transaction handling using APSW."""

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import apsw

_standalone_db: apsw.Connection | None = None

# Highest migration version this code expects; checked at app boot. If the DB on
# disk reports a lower value, the runtime aborts with a clear message instructing
# the operator to run `make db-init`. Bump this when adding a new migration file.
EXPECTED_SCHEMA_VERSION = 1


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
    conn.execute("PRAGMA journal_mode = WAL;")


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
    import os

    candidates = [
        # Source checkout: src/strata/db.py -> repo_root/database/migrations
        Path(__file__).parent.parent.parent / "database" / "migrations",
        # Container / installed layout: $STRATA_ROOT/database/migrations
        *(
            [Path(os.environ["STRATA_ROOT"]) / "database" / "migrations"]
            if os.environ.get("STRATA_ROOT")
            else []
        ),
        # CWD fallback (e.g. running from /app in docker)
        Path.cwd() / "database" / "migrations",
    ]
    for path in candidates:
        if path.is_dir():
            return path
    raise FileNotFoundError(
        f"Could not locate database/migrations/. Tried: {[str(p) for p in candidates]}"
    )


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
