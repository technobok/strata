"""Configuration registry and type system.

Every configurable setting is declared here with its key, type, default,
description, and whether it contains a secret.  The registry is the single
source of truth for what settings exist.
"""

from dataclasses import dataclass
from enum import Enum


class ConfigType(Enum):
    STRING = "string"
    INT = "int"
    BOOL = "bool"
    STRING_LIST = "string_list"


@dataclass(frozen=True, slots=True)
class ConfigEntry:
    key: str
    type: ConfigType
    default: str | int | bool | list[str]
    description: str
    secret: bool = False


# ---------------------------------------------------------------------------
# Registry -- every known setting
# ---------------------------------------------------------------------------

REGISTRY: list[ConfigEntry] = [
    # -- server --
    ConfigEntry("server.host", ConfigType.STRING, "0.0.0.0", "Bind address for production server"),
    ConfigEntry("server.port", ConfigType.INT, 5000, "Port for production server"),
    ConfigEntry("server.dev_host", ConfigType.STRING, "127.0.0.1", "Bind address for dev server"),
    ConfigEntry("server.dev_port", ConfigType.INT, 5000, "Port for dev server"),
    ConfigEntry("server.debug", ConfigType.BOOL, False, "Enable Flask debug mode"),
    # -- cache --
    ConfigEntry("cache.directory", ConfigType.STRING, "instance/cache", "Result cache directory"),
    ConfigEntry(
        "cache.retention_days", ConfigType.INT, 30, "Days to keep cached results before cleanup"
    ),
    # -- worker --
    ConfigEntry(
        "worker.poll_interval", ConfigType.INT, 30, "Schedule worker poll interval in seconds"
    ),
    # -- proxy --
    ConfigEntry("proxy.x_forwarded_for", ConfigType.INT, 0, "Trust X-Forwarded-For (hop count)"),
    ConfigEntry(
        "proxy.x_forwarded_proto", ConfigType.INT, 0, "Trust X-Forwarded-Proto (hop count)"
    ),
    ConfigEntry("proxy.x_forwarded_host", ConfigType.INT, 0, "Trust X-Forwarded-Host (hop count)"),
    ConfigEntry(
        "proxy.x_forwarded_prefix", ConfigType.INT, 0, "Trust X-Forwarded-Prefix (hop count)"
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
    "cache.directory": "CACHE_DIRECTORY",
    "cache.retention_days": "CACHE_RETENTION_DAYS",
    "worker.poll_interval": "WORKER_POLL_INTERVAL",
    "proxy.x_forwarded_for": "PROXY_X_FORWARDED_FOR",
    "proxy.x_forwarded_proto": "PROXY_X_FORWARDED_PROTO",
    "proxy.x_forwarded_host": "PROXY_X_FORWARDED_HOST",
    "proxy.x_forwarded_prefix": "PROXY_X_FORWARDED_PREFIX",
}
