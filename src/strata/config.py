"""Configuration registry and type system.

Every configurable setting is declared here with its key, type, default,
description, and whether it contains a secret.  The registry is the single
source of truth for what settings exist.

Settings marked ``Applies.LIVE`` are read from ``app_setting`` when used
(``live_str``/``live_int``/``live_bool``/``live_list``, cached 5 s), so a change
in /admin/config reaches the web app and the worker without a restart. Every
other setting is loaded into ``app.config`` once, at ``create_app``.
"""

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum


class ConfigType(Enum):
    STRING = "string"
    INT = "int"
    BOOL = "bool"
    STRING_LIST = "string_list"


class Applies(Enum):
    """When a changed setting takes effect (shown on /admin/config)."""

    LIVE = "applies immediately"
    APP = "restart the app"
    WORKER = "restart the worker"
    BOTH = "restart app + worker"


@dataclass(frozen=True, slots=True)
class ConfigEntry:
    key: str
    type: ConfigType
    default: str | int | bool | list[str]
    description: str
    secret: bool = False
    applies: Applies = Applies.BOTH


# ---------------------------------------------------------------------------
# Registry -- every known setting
# ---------------------------------------------------------------------------

REGISTRY: list[ConfigEntry] = [
    # -- server --
    ConfigEntry(
        "server.host",
        ConfigType.STRING,
        "0.0.0.0",
        "Bind address for production server",
        applies=Applies.APP,
    ),
    ConfigEntry(
        "server.port", ConfigType.INT, 5000, "Port for production server", applies=Applies.APP
    ),
    ConfigEntry(
        "server.dev_host",
        ConfigType.STRING,
        "127.0.0.1",
        "Bind address for dev server",
        applies=Applies.APP,
    ),
    ConfigEntry(
        "server.dev_port", ConfigType.INT, 5000, "Port for dev server", applies=Applies.APP
    ),
    ConfigEntry(
        "server.debug", ConfigType.BOOL, False, "Enable Flask debug mode", applies=Applies.APP
    ),
    ConfigEntry(
        "server.threads",
        ConfigType.INT,
        8,
        "Request threads per gunicorn worker (gthread)",
        applies=Applies.APP,
    ),
    ConfigEntry(
        "server.max_upload_mb",
        ConfigType.INT,
        512,
        "Largest accepted request body in MB (source-file uploads); larger gets HTTP 413",
        applies=Applies.APP,
    ),
    ConfigEntry(
        "server.public_url",
        ConfigType.STRING,
        "",
        "The address readers use to reach Strata, e.g. https://reports.example.com/strata. "
        "Links in mail are built from it (the worker has no request to take it from)",
        applies=Applies.LIVE,
    ),
    ConfigEntry(
        "server.timeout",
        ConfigType.INT,
        120,
        "Seconds a gunicorn worker may go silent before it is restarted (not a per-request "
        "limit under gthread)",
        applies=Applies.APP,
    ),
    # -- data storage --
    ConfigEntry(
        "data.root",
        ConfigType.STRING,
        "instance/data",
        "Root directory for datamodule storage (built versions + uploaded sources)",
    ),
    # -- auth --
    ConfigEntry(
        "auth.admin_group",
        ConfigType.STRING,
        "strata-admins",
        "Gatekeeper group whose members get strata admin access",
        applies=Applies.APP,
    ),
    # -- gatekeeper --
    ConfigEntry("gatekeeper.db_path", ConfigType.STRING, "", "Path to Gatekeeper SQLite database"),
    ConfigEntry("gatekeeper.url", ConfigType.STRING, "", "Gatekeeper HTTP API base URL"),
    ConfigEntry("gatekeeper.api_key", ConfigType.STRING, "", "Gatekeeper API key", secret=True),
    # -- mail / outbox --
    ConfigEntry(
        "mail.sender",
        ConfigType.STRING,
        "strata@localhost",
        "From-address on mail Strata sends (scheduled runs)",
        applies=Applies.LIVE,
    ),
    ConfigEntry(
        "outbox.db_path",
        ConfigType.STRING,
        "",
        "Path to outbox SQLite (queues outgoing email); or set outbox.url + outbox.api_key",
        applies=Applies.LIVE,
    ),
    ConfigEntry(
        "outbox.url",
        ConfigType.STRING,
        "",
        "Outbox HTTP API base URL, when outbox.db_path is not set",
        applies=Applies.LIVE,
    ),
    ConfigEntry(
        "outbox.api_key",
        ConfigType.STRING,
        "",
        "Outbox API key, for outbox.url",
        secret=True,
        applies=Applies.LIVE,
    ),
    # -- worker --
    ConfigEntry(
        "worker.poll_interval",
        ConfigType.INT,
        30,
        "Seconds between the worker's sweeps for dead builds and orphaned jobs (due schedules "
        "are checked every 10 s regardless)",
        applies=Applies.LIVE,
    ),
    ConfigEntry(
        "worker.concurrency",
        ConfigType.INT,
        2,
        "Builds one strata-worker process runs at once (override with --concurrency)",
        applies=Applies.WORKER,
    ),
    # -- serving --
    ConfigEntry(
        "serve.max_wait_seconds",
        ConfigType.INT,
        60,
        "How long a report request waits for the rebuild of stale data before serving the "
        "previous version, marked stale (PowerQuery gives up after 100s)",
        applies=Applies.LIVE,
    ),
    ConfigEntry(
        "serve.memory_limit",
        ConfigType.STRING,
        "2GB",
        "DuckDB memory limit for each report query (spills to disk beyond it)",
        applies=Applies.LIVE,
    ),
    ConfigEntry(
        "serve.memory_duckdb_default",
        ConfigType.BOOL,
        False,
        "Use DuckDB's own default memory limit (80% of RAM) for report queries, "
        "instead of serve.memory_limit",
        applies=Applies.LIVE,
    ),
    # -- schedules --
    ConfigEntry(
        "schedule.timezones",
        ConfigType.STRING_LIST,
        ["Australia/Brisbane", "Australia/Sydney", "Pacific/Auckland"],
        "Timezones offered for schedules (IANA names, comma-separated); the first is the default",
        applies=Applies.LIVE,
    ),
    # -- builds --
    ConfigEntry(
        "build.memory_limit",
        ConfigType.STRING,
        "4GB",
        "DuckDB memory limit per build (spills to disk beyond it). Sized for 2 concurrent "
        "builds on 16 GB; a script may SET its own",
        applies=Applies.LIVE,
    ),
    ConfigEntry(
        "build.memory_duckdb_default",
        ConfigType.BOOL,
        False,
        "Use DuckDB's own default memory limit (80% of RAM) for builds, instead of "
        "build.memory_limit",
        applies=Applies.LIVE,
    ),
    ConfigEntry(
        "build.threads",
        ConfigType.INT,
        0,
        "DuckDB threads per build; 0 = DuckDB's default (one per core)",
        applies=Applies.LIVE,
    ),
    ConfigEntry(
        "build.timeout_seconds",
        ConfigType.INT,
        3600,
        "A build still running after this many seconds is interrupted and fails; 0 = no limit",
        applies=Applies.LIVE,
    ),
    ConfigEntry(
        "build.sandbox",
        ConfigType.BOOL,
        True,
        "Lock build scripts to their own datamodule's sources/ and tmp/ (DuckDB external "
        "access off). Turn off only if a connection driver fails under the sandbox.",
        applies=Applies.LIVE,
    ),
    # -- runs (report and check snapshots) --
    ConfigEntry(
        "runs.retention_days",
        ConfigType.INT,
        30,
        "Days a report or check run (its row and its saved rows) is kept; the worker's "
        "sweep and strata-admin reconcile delete older ones",
        applies=Applies.LIVE,
    ),
    # -- proxy --
    ConfigEntry(
        "proxy.x_forwarded_for",
        ConfigType.INT,
        0,
        "Trust X-Forwarded-For (hop count)",
        applies=Applies.APP,
    ),
    ConfigEntry(
        "proxy.x_forwarded_proto",
        ConfigType.INT,
        0,
        "Trust X-Forwarded-Proto (hop count)",
        applies=Applies.APP,
    ),
    ConfigEntry(
        "proxy.x_forwarded_host",
        ConfigType.INT,
        0,
        "Trust X-Forwarded-Host (hop count)",
        applies=Applies.APP,
    ),
    ConfigEntry(
        "proxy.x_forwarded_prefix",
        ConfigType.INT,
        0,
        "Trust X-Forwarded-Prefix (hop count)",
        applies=Applies.APP,
    ),
    # -- bootstrap-only (read before the DB is open; shown read-only in /admin/config) --
    ConfigEntry(
        "storage.strata_db",
        ConfigType.STRING,
        "",
        "Path to the strata SQLite metadata DB. Bootstrap-only: must be set via "
        "the STRATA_DB env var since it is needed before the DB is opened.",
    ),
    ConfigEntry(
        "storage.strata_root",
        ConfigType.STRING,
        "",
        "Project root used to resolve database/migrations/ at init-db time. "
        "Bootstrap-only — set via STRATA_ROOT.",
    ),
]

# Fast lookup by key
_REGISTRY_MAP: dict[str, ConfigEntry] = {e.key: e for e in REGISTRY}


def resolve_entry(key: str) -> ConfigEntry | None:
    """Look up a registry entry by key."""
    return _REGISTRY_MAP.get(key)


# ---------------------------------------------------------------------------
# Value parsing / serialization
# ---------------------------------------------------------------------------


def parse_value(entry: ConfigEntry, raw: str) -> str | int | bool | list[str]:
    """Parse a raw string value according to the entry's type."""
    match entry.type:
        case ConfigType.STRING:
            return raw
        case ConfigType.INT:
            return int(raw)
        case ConfigType.BOOL:
            return raw.lower() in ("true", "1", "yes", "on")
        case ConfigType.STRING_LIST:
            return [s.strip() for s in raw.split(",") if s.strip()]


def serialize_value(entry: ConfigEntry, value: str | int | bool | list[str]) -> str:
    """Serialize a typed value to a string for storage."""
    match entry.type:
        case ConfigType.BOOL:
            return "true" if value else "false"
        case ConfigType.STRING_LIST:
            if isinstance(value, list):
                return ", ".join(value)
            return str(value)
        case _:
            return str(value)


# ---------------------------------------------------------------------------
# Mapping from registry keys to Flask app.config keys
# ---------------------------------------------------------------------------

KEY_MAP: dict[str, str] = {
    "server.host": "HOST",
    "server.port": "PORT",
    "server.dev_host": "DEV_HOST",
    "server.dev_port": "DEV_PORT",
    "server.debug": "DEBUG",
    "server.threads": "SERVER_THREADS",
    "server.timeout": "SERVER_TIMEOUT",
    "server.max_upload_mb": "MAX_UPLOAD_MB",
    "data.root": "DATA_ROOT",
    "auth.admin_group": "AUTH_ADMIN_GROUP",
    "gatekeeper.db_path": "GATEKEEPER_DB_PATH",
    "gatekeeper.url": "GATEKEEPER_URL",
    "gatekeeper.api_key": "GATEKEEPER_API_KEY",
    "worker.concurrency": "WORKER_CONCURRENCY",
    "proxy.x_forwarded_for": "PROXY_X_FORWARDED_FOR",
    "proxy.x_forwarded_proto": "PROXY_X_FORWARDED_PROTO",
    "proxy.x_forwarded_host": "PROXY_X_FORWARDED_HOST",
    "proxy.x_forwarded_prefix": "PROXY_X_FORWARDED_PREFIX",
    # storage.* are deliberately not mapped — they're env-only at runtime.
    # Applies.LIVE settings are not mapped either: they are read at use through
    # live_*() (env override first, then app_setting, then the default), so
    # app.config never holds a copy that could go stale.
}


# Registry key → env var that overrides it at runtime.
ENV_OVERRIDES: dict[str, str] = {
    "server.host": "HOST",
    "server.port": "PORT",
    "data.root": "DATA_ROOT",
    "auth.admin_group": "STRATA_ADMIN_GROUP",
    "gatekeeper.db_path": "GATEKEEPER_DB",
    "mail.sender": "MAIL_SENDER",
    "outbox.db_path": "OUTBOX_DB",
    "proxy.x_forwarded_for": "PROXY_X_FORWARDED_FOR",
    "proxy.x_forwarded_proto": "PROXY_X_FORWARDED_PROTO",
    "proxy.x_forwarded_host": "PROXY_X_FORWARDED_HOST",
    "proxy.x_forwarded_prefix": "PROXY_X_FORWARDED_PREFIX",
    "storage.strata_db": "STRATA_DB",
    "storage.strata_root": "STRATA_ROOT",
}


# Registry keys whose value is needed before the DB is open. The /admin/config
# form shows them read-only; the save handler skips them defensively.
BOOTSTRAP_ONLY: frozenset[str] = frozenset(
    {
        "storage.strata_db",
        "storage.strata_root",
    }
)


def resolve_effective(
    entry: ConfigEntry,
    env_get: Callable[[str], str | None],
) -> tuple[str, str]:
    """Return (display_value, source) for a registry entry.

    source is one of 'env', 'db', 'default'. The DB-row presence (not Flask's
    merged config view) is what makes a value 'db' vs 'default'.
    """
    from strata.models.app_setting import get_setting  # local: avoid import cycle

    env_name = ENV_OVERRIDES.get(entry.key)
    if env_name:
        raw = env_get(env_name)
        if raw is not None and raw != "":
            return raw, "env"
    stored_raw = get_setting(entry.key)
    if stored_raw is not None:
        return stored_raw, "db"
    return serialize_value(entry, entry.default), "default"


# ---------------------------------------------------------------------------
# Live settings: read from app_setting at use (Applies.LIVE only)
# ---------------------------------------------------------------------------

LIVE_CACHE_SECONDS = 5.0
_now = time.monotonic  # tests replace it to step past the cache

_live_lock = threading.Lock()
_live_cache: dict[str, tuple[float, dict[str, str]]] = {}  # DB path -> (read at, rows)
_live_generation = 0  # bumped by every write in this process


def forget_cached_settings() -> None:
    """Drop the cached settings; app_setting writes in this process call it, so the
    process that saved a change sees it at once. Other processes (the worker, the
    CLI's writes) see it within LIVE_CACHE_SECONDS."""
    global _live_generation
    with _live_lock:
        _live_cache.clear()
        _live_generation += 1


def _stored_values() -> dict[str, str]:
    from flask import current_app

    from strata.db import get_db

    db_path = current_app.config["DATABASE_PATH"]
    now = _now()
    with _live_lock:
        cached = _live_cache.get(db_path)
        generation = _live_generation
    if cached is not None and now - cached[0] < LIVE_CACHE_SECONDS:
        return cached[1]
    rows = get_db().execute("SELECT key, value FROM app_setting").fetchall()
    values = {str(k): str(v) for k, v in rows if str(k) in _REGISTRY_MAP}
    with _live_lock:
        # A write that landed while this read ran must not be shadowed by it.
        if generation == _live_generation:
            _live_cache[db_path] = (now, values)
    return values


def _live(key: str, expected: ConfigType) -> str | int | bool | list[str]:
    entry = _REGISTRY_MAP[key]
    if entry.applies is not Applies.LIVE:
        raise ValueError(f"{key} is not a live setting; read it from app.config")
    if entry.type is not expected:
        raise TypeError(f"{key} is a {entry.type.value} setting, not {expected.value}")
    # Same precedence as /admin/config shows (resolve_effective): env > DB > default.
    env_name = ENV_OVERRIDES.get(key)
    raw = os.environ.get(env_name) if env_name else None
    if not raw:
        raw = _stored_values().get(key)
    if not raw:  # unset, or a blank stored before blank meant "default" everywhere
        return entry.default
    try:
        return parse_value(entry, raw)
    except ValueError as exc:
        raise ValueError(f"the {key} setting {raw!r} is not a valid {entry.type.value}") from exc


def live_str(key: str) -> str:
    value = _live(key, ConfigType.STRING)
    assert isinstance(value, str)
    return value


def live_int(key: str) -> int:
    value = _live(key, ConfigType.INT)
    assert isinstance(value, int) and not isinstance(value, bool)
    return value


def live_bool(key: str) -> bool:
    value = _live(key, ConfigType.BOOL)
    assert isinstance(value, bool)
    return value


def live_list(key: str) -> list[str]:
    value = _live(key, ConfigType.STRING_LIST)
    assert isinstance(value, list)
    return list(value)
