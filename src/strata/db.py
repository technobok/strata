"""Database connection and transaction handling using APSW."""

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import apsw

_standalone_db: apsw.Connection | None = None


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


def _find_schema_path() -> Path:
    """Locate database/schema.sql across dev and installed-package layouts."""
    import os

    candidates = [
        # Source checkout: src/strata/db.py -> repo_root/database/schema.sql
        Path(__file__).parent.parent.parent / "database" / "schema.sql",
        # Container / installed layout: $STRATA_ROOT/database/schema.sql
        *(
            [Path(os.environ["STRATA_ROOT"]) / "database" / "schema.sql"]
            if os.environ.get("STRATA_ROOT")
            else []
        ),
        # CWD fallback (e.g. running from /app in docker)
        Path.cwd() / "database" / "schema.sql",
    ]
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError(
        f"Could not locate database/schema.sql. Tried: {[str(p) for p in candidates]}"
    )


def init_db_at(db_path: str) -> None:
    """Initialize the database schema at the given path.

    Works without Flask context.
    """
    import secrets

    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = apsw.Connection(db_path)
    _configure_connection(conn)

    schema_path = _find_schema_path()
    with open(schema_path) as f:
        for _ in conn.execute(f.read()):
            pass

    # Generate secret_key if not exists
    row = conn.execute("SELECT value FROM app_setting WHERE key = 'secret_key'").fetchone()
    if not row:
        new_key = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT OR IGNORE INTO app_setting (key, value, description) VALUES (?, ?, ?)",
            ("secret_key", new_key, "Secret key for signing auth tokens"),
        )

    _apply_schema_migrations(conn)
    conn.close()


def _apply_schema_migrations(conn: apsw.Connection) -> None:
    """Idempotent ALTER TABLE upgrades for pre-existing databases."""
    report_cols = {row[1] for row in conn.execute("PRAGMA table_info(report)").fetchall()}
    if "connection_id" not in report_cols:
        conn.execute(
            "ALTER TABLE report ADD COLUMN connection_id INTEGER REFERENCES connection(id)"
        )

    conn.execute("INSERT OR REPLACE INTO db_metadata (key, value) VALUES ('schema_version', '1')")


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
