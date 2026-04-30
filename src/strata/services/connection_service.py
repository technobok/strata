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
    kind: str = "text"  # 'text' | 'password' | 'number'
    required: bool = True
    default: str | None = None


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


DRIVERS: dict[str, DriverSpec] = {
    "sqlite": DriverSpec(
        label="SQLite (file)",
        param_schema=[ParamField("path", "Database file path")],
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
            ParamField("password", "Password", kind="password"),
        ],
        extensions=["postgres"],
        attach_sql=_postgres_attach,
    ),
}


def attach_into(conn: duckdb.DuckDBPyConnection, alias: str, driver: str, params: dict) -> None:
    """Load required extensions and ATTACH the source as `alias` on conn."""
    spec = DRIVERS[driver]
    for ext in spec.extensions:
        conn.execute(f"INSTALL {ext}")
        conn.execute(f"LOAD {ext}")
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
