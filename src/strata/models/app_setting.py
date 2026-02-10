"""App setting model for configuration storage."""

from strata.db import get_db, transaction


def get_setting(key: str) -> str | None:
    """Get a setting value by key."""
    db = get_db()
    row = db.execute("SELECT value FROM app_setting WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row else None


def set_setting(key: str, value: str, description: str = "") -> None:
    """Set a setting value (upsert)."""
    with transaction() as cursor:
        cursor.execute(
            "INSERT INTO app_setting (key, value, description) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value, description),
        )
