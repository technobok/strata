"""Connection driver registry, encryption, and ATTACH helpers.

Connection params are stored in the metadata DB encrypted with the app's
secret_key (already auto-generated at init_db_at). Adding a driver means
adding a DriverSpec entry to DRIVERS.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass

import duckdb
from cryptography.fernet import Fernet, InvalidToken

from strata.models.app_setting import get_setting

_fernet_cache: Fernet | None = None


def _fernet() -> Fernet:
    """Return a Fernet keyed off app_setting.secret_key (lazy + cached)."""
    global _fernet_cache
    if _fernet_cache is not None:
        return _fernet_cache
    secret = get_setting("secret_key")
    if not secret:
        raise RuntimeError("secret_key missing from app_setting; run 'strata-admin init-db' first")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    _fernet_cache = Fernet(key)
    return _fernet_cache


def encrypt_params(params: dict) -> str:
    """JSON-serialize and Fernet-encrypt a params dict."""
    return _fernet().encrypt(json.dumps(params).encode()).decode()


def decrypt_params(token: str) -> dict:
    """Fernet-decrypt and JSON-parse back into a params dict."""
    try:
        return json.loads(_fernet().decrypt(token.encode()).decode())
    except InvalidToken as exc:
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
    attach_sql: Callable[[str, dict], str]
    probe_sql: str = "SELECT 1"


def _sqlite_attach(alias: str, params: dict) -> str:
    path = str(params.get("path", "")).replace("'", "''")
    return f"ATTACH '{path}' AS {alias} (TYPE sqlite)"


def _postgres_attach(alias: str, params: dict) -> str:
    parts = [
        f"host={params['host']}",
        f"port={params.get('port', 5432)}",
        f"dbname={params['database']}",
        f"user={params['user']}",
        f"password={params['password']}",
    ]
    conn_str = " ".join(parts).replace("'", "''")
    return f"ATTACH '{conn_str}' AS {alias} (TYPE postgres)"


def _odbc_attach(alias: str, params: dict) -> str:
    cs = str(params["connection_string"]).replace("'", "''")
    return f"ATTACH '{cs}' AS {alias} (TYPE odbc_scanner, READ_ONLY)"


def _mssql_attach(alias: str, params: dict) -> str:
    # Build a standard ODBC-style MSSQL connection string. DuckDB's mssql
    # community extension accepts `Server=...;Database=...;User Id=...;...`.
    parts = [f"Server={params['server']}"]
    if params.get("port"):
        parts[0] = f"Server={params['server']},{params['port']}"
    parts.append(f"Database={params['database']}")
    parts.append(f"User Id={params['user']}")
    parts.append(f"Password={params['password']}")
    if str(params.get("encrypt", "")).lower() in ("yes", "true", "1"):
        parts.append("Encrypt=yes")
    if str(params.get("trust_server_certificate", "")).lower() in ("yes", "true", "1"):
        parts.append("TrustServerCertificate=yes")
    conn_str = ";".join(parts).replace("'", "''")
    return f"ATTACH '{conn_str}' AS {alias} (TYPE mssql)"


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
        attach_sql=_sqlite_attach,
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
        attach_sql=_postgres_attach,
    ),
    "mssql": DriverSpec(
        label="Microsoft SQL Server",
        param_schema=[
            ParamField("server", "Server (host or DSN)"),
            ParamField("port", "Port", kind="number", required=False),
            ParamField("database", "Database"),
            ParamField("user", "User"),
            ParamField("password", "Password", kind="password", secret=True),
            ParamField("encrypt", "Encrypt (yes/no)", required=False, default="yes"),
            ParamField(
                "trust_server_certificate",
                "Trust server certificate (yes/no)",
                required=False,
                default="no",
            ),
        ],
        # mssql lives in the community-extensions repo
        extensions=["community/mssql"],
        attach_sql=_mssql_attach,
    ),
    "odbc": DriverSpec(
        label="ODBC (any driver — typically FreeTDS for SQL Server)",
        param_schema=[
            ParamField(
                "connection_string",
                "ODBC connection string",
                kind="textarea",
                secret=True,
            ),
        ],
        extensions=["community/odbc_scanner"],
        attach_sql=_odbc_attach,
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


def attach_into(conn: duckdb.DuckDBPyConnection, alias: str, driver: str, params: dict) -> None:
    """Load required extensions and ATTACH the source as `alias` on conn."""
    spec = DRIVERS[driver]
    for ext in spec.extensions:
        _install_load(conn, ext)
    conn.execute(spec.attach_sql(alias, params))


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
