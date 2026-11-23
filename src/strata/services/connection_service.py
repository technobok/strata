"""Connection driver registry, encryption, and ATTACH helpers.

Connection params are stored in the metadata DB encrypted with the app's
secret_key (already auto-generated at init_db_at). Adding a driver means
adding a DriverSpec entry to DRIVERS.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

import duckdb

from strata.crypto import decrypt_text, encrypt_text


def encrypt_params(params: dict) -> str:
    """JSON-serialize and Fernet-encrypt a params dict."""
    return encrypt_text(json.dumps(params))


def decrypt_params(token: str) -> dict:
    """Fernet-decrypt and JSON-parse back into a params dict."""
    try:
        return json.loads(decrypt_text(token))
    except RuntimeError as exc:
        raise RuntimeError("connection params decryption failed (key changed?)") from exc


# ---------------------------------------------------------------------------
# Driver registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParamField:
    name: str
    label: str
    kind: str = "text"  # 'text' | 'password' | 'number' | 'textarea'
    required: bool = True
    default: str | None = None
    secret: bool = False  # if True, blank value at edit-time keeps the existing value


@dataclass(frozen=True)
class DriverSpec:
    label: str
    param_schema: list[ParamField]
    extensions: list[str]
    # Per-driver "make this connection usable under <alias>" function: the
    # build script reads every connection by its local name. ATTACH-able drivers
    # (sqlite, postgres) attach a database named <alias>; ODBC sets a session
    # variable <alias> holding the odbc_connect() handle.
    attach: Callable[[duckdb.DuckDBPyConnection, str, dict], None]
    # How a build script reads it, shown beside each binding ({name} = alias).
    usage: str
    probe_sql: str = "SELECT 1"


def _sqlite_attach(conn: duckdb.DuckDBPyConnection, alias: str, params: dict) -> None:
    path = str(params.get("path", "")).replace("'", "''")
    conn.execute(f"ATTACH '{path}' AS {alias} (TYPE sqlite)")


def _postgres_attach(conn: duckdb.DuckDBPyConnection, alias: str, params: dict) -> None:
    parts = [
        f"host={params['host']}",
        f"port={params.get('port', 5432)}",
        f"dbname={params['database']}",
        f"user={params['user']}",
        f"password={params['password']}",
    ]
    conn_str = " ".join(parts).replace("'", "''")
    conn.execute(f"ATTACH '{conn_str}' AS {alias} (TYPE postgres)")


def _odbc_attach(conn: duckdb.DuckDBPyConnection, alias: str, params: dict) -> None:
    """Create a SET VARIABLE <alias> holding the odbc_connect() handle.

    DuckDB's core odbc_scanner extension is a query-function extension, not a
    storage extension — there's no `ATTACH ... TYPE odbc_scanner`. Instead,
    you connect once and pass the handle to odbc_query() per SELECT. We stash
    the handle in a session variable named after the local name, so build
    scripts reach it as `odbc_query(getvariable('<alias>'), '...')`.
    """
    cs = str(params["connection_string"]).replace("'", "''")
    conn.execute(f"SET VARIABLE {alias} = odbc_connect('{cs}')")


DRIVERS: dict[str, DriverSpec] = {
    "sqlite": DriverSpec(
        label="SQLite (file)",
        param_schema=[
            ParamField(
                "path",
                "Database file path (absolute, inside the container — e.g. "
                "/app/instance/external.sqlite3; bind-mount the file)",
            )
        ],
        extensions=[],
        attach=_sqlite_attach,
        usage="{name}.<table>",
    ),
    "postgres": DriverSpec(
        label="PostgreSQL",
        param_schema=[
            ParamField("host", "Host"),
            ParamField("port", "Port", kind="number", required=False, default="5432"),
            ParamField("database", "Database"),
            ParamField("user", "User"),
            ParamField("password", "Password", kind="password", secret=True),
        ],
        extensions=["postgres"],
        attach=_postgres_attach,
        usage="{name}.<schema>.<table>",
    ),
    "odbc": DriverSpec(
        label="ODBC (any driver — typically FreeTDS for SQL Server)",
        param_schema=[
            # Not marked secret=True even though the string contains PWD=…:
            # the operator needs to see the existing connection-string contents
            # (host, UID, driver-specific options) to meaningfully edit them.
            # The value is still Fernet-encrypted at rest in the DB.
            ParamField(
                "connection_string",
                "ODBC connection string",
                kind="textarea",
            ),
        ],
        # odbc_scanner is published in DuckDB's CORE extensions repo (v1.5.2+),
        # not community-extensions. Earlier versions of strata pointed at
        # community/odbc_scanner which 404s.
        extensions=["odbc_scanner"],
        attach=_odbc_attach,
        usage="odbc_query(getvariable('{name}'), '<SQL Server query>')",
    ),
}


def _install_load(conn: duckdb.DuckDBPyConnection, ext: str) -> None:
    """INSTALL+LOAD a DuckDB extension.

    Accepts 'name' for core extensions or 'community/name' for the community
    repo (e.g. 'community/mssql').
    """
    if "/" in ext:
        repo, name = ext.split("/", 1)
        conn.execute(f"INSTALL {name} FROM {repo}")
    else:
        name = ext
        conn.execute(f"INSTALL {name}")
    conn.execute(f"LOAD {name}")


def usage(driver: str, name: str) -> str:
    """How a build script reads a connection bound under ``name``."""
    spec = DRIVERS.get(driver)
    return spec.usage.format(name=name) if spec else name


def attach_into(conn: duckdb.DuckDBPyConnection, alias: str, driver: str, params: dict) -> None:
    """Load required extensions and bind the source under `alias` on conn.

    For ATTACH-able drivers (sqlite, postgres) the alias is a DuckDB database.
    For ODBC it is the name of a session variable holding the odbc_connect()
    handle.
    """
    spec = DRIVERS[driver]
    for ext in spec.extensions:
        _install_load(conn, ext)
    spec.attach(conn, alias, params)


def test_connection(driver: str, params: dict) -> tuple[bool, str]:
    """Probe a connection by ATTACHing it and running a trivial query."""
    if driver not in DRIVERS:
        return False, f"Unknown driver: {driver}"
    spec = DRIVERS[driver]
    try:
        conn = duckdb.connect(":memory:")
        try:
            attach_into(conn, "probe", driver, params)
            conn.execute(spec.probe_sql).fetchall()
        finally:
            conn.close()
        return True, "OK"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
