"""API key model — hashed bearer tokens for unauthenticated report data access.

A key grants Gatekeeper-bypass access to a report's data formats via
``?key=<token>`` (Excel/PowerQuery). Only the sha256 of the token is stored;
the plaintext is returned exactly once from ``create``/``rotate``. Scope is a
whole datamodule or a single report; ``fixed_args_json`` values always win over
caller-supplied args and ``parameterised_args_json``, when set, whitelists the
arg names a caller may supply.
"""

import hashlib
import json
import secrets
import uuid as uuid_lib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from strata.db import get_db, transaction


def hash_token(token: str) -> str:
    """sha256 hex digest — the only form of the token ever stored."""
    return hashlib.sha256(token.encode()).hexdigest()


def _new_token() -> tuple[str, str, str]:
    """(plaintext, hash, display prefix) for a fresh bearer token."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token), token[:8]


@dataclass
class ApiKey:
    id: int
    uuid: str  # public management id (never the secret)
    token_hash: str
    token_prefix: str
    name: str
    scope_type: str  # 'datamodule' | 'report'
    datamodule_id: int | None  # set iff scope_type == 'datamodule'
    report_id: int | None  # set iff scope_type == 'report'
    fixed_args_json: str | None
    parameterised_args_json: str | None
    enabled: bool
    expires_at: str | None
    created_by: str
    created_at: str
    last_used_at: str | None
    use_count: int

    _COLUMNS = (
        "id, uuid, token_hash, token_prefix, name, scope_type, datamodule_id, "
        "report_id, fixed_args_json, parameterised_args_json, enabled, "
        "expires_at, created_by, created_at, last_used_at, use_count"
    )

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> ApiKey:
        return ApiKey(
            id=int(row[0]),
            uuid=str(row[1]),
            token_hash=str(row[2]),
            token_prefix=str(row[3]),
            name=str(row[4]),
            scope_type=str(row[5]),
            datamodule_id=int(row[6]) if row[6] is not None else None,
            report_id=int(row[7]) if row[7] is not None else None,
            fixed_args_json=str(row[8]) if row[8] is not None else None,
            parameterised_args_json=str(row[9]) if row[9] is not None else None,
            enabled=bool(row[10]),
            expires_at=str(row[11]) if row[11] is not None else None,
            created_by=str(row[12]),
            created_at=str(row[13]),
            last_used_at=str(row[14]) if row[14] is not None else None,
            use_count=int(row[15]),
        )

    @staticmethod
    def get_by_id(key_id: int) -> ApiKey | None:
        db = get_db()
        row = db.execute(
            f"SELECT {ApiKey._COLUMNS} FROM api_key WHERE id = ?", (key_id,)
        ).fetchone()
        return ApiKey._from_row(row) if row else None

    @staticmethod
    def get_by_uuid(key_uuid: str) -> ApiKey | None:
        db = get_db()
        row = db.execute(
            f"SELECT {ApiKey._COLUMNS} FROM api_key WHERE uuid = ?", (key_uuid,)
        ).fetchone()
        return ApiKey._from_row(row) if row else None

    @staticmethod
    def get_by_token_hash(token_hash: str) -> ApiKey | None:
        db = get_db()
        row = db.execute(
            f"SELECT {ApiKey._COLUMNS} FROM api_key WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        return ApiKey._from_row(row) if row else None

    @staticmethod
    def get_for_datamodule(datamodule_id: int) -> list[ApiKey]:
        """Keys scoped to this datamodule or to any of its reports."""
        db = get_db()
        rows = db.execute(
            f"SELECT {ApiKey._COLUMNS} FROM api_key "
            "WHERE datamodule_id = ? "
            "OR report_id IN (SELECT id FROM report WHERE datamodule_id = ?) "
            "ORDER BY created_at DESC, id DESC",
            (datamodule_id, datamodule_id),
        ).fetchall()
        return [ApiKey._from_row(row) for row in rows]

    @staticmethod
    def create(
        name: str,
        scope_type: str,
        datamodule_id: int | None,
        report_id: int | None,
        fixed_args_json: str | None,
        parameterised_args_json: str | None,
        expires_at: str | None,
        created_by: str,
    ) -> tuple[ApiKey, str]:
        """Insert a key and return (key, plaintext token) — the only time the
        plaintext exists server-side."""
        now = datetime.now(UTC).isoformat()
        key_uuid = str(uuid_lib.uuid4())
        token, token_h, prefix = _new_token()
        with transaction() as cursor:
            cursor.execute(
                "INSERT INTO api_key (uuid, token_hash, token_prefix, name, scope_type, "
                "datamodule_id, report_id, fixed_args_json, parameterised_args_json, "
                "enabled, expires_at, created_by, created_at, use_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 0)",
                (
                    key_uuid,
                    token_h,
                    prefix,
                    name.strip(),
                    scope_type,
                    datamodule_id,
                    report_id,
                    fixed_args_json,
                    parameterised_args_json,
                    expires_at,
                    created_by,
                    now,
                ),
            )
            row = cursor.execute("SELECT last_insert_rowid()").fetchone()
            key_id = int(row[0]) if row else 0
        created = ApiKey.get_by_id(key_id)
        assert created is not None
        return created, token

    def rotate(self) -> str:
        """Replace the secret (breaks existing workbooks); returns the new plaintext."""
        token, token_h, prefix = _new_token()
        with transaction() as cursor:
            cursor.execute(
                "UPDATE api_key SET token_hash = ?, token_prefix = ? WHERE id = ?",
                (token_h, prefix, self.id),
            )
        self.token_hash = token_h
        self.token_prefix = prefix
        return token

    def set_enabled(self, enabled: bool) -> None:
        with transaction() as cursor:
            cursor.execute(
                "UPDATE api_key SET enabled = ? WHERE id = ?", (1 if enabled else 0, self.id)
            )
        self.enabled = enabled

    def delete(self) -> bool:
        with transaction() as cursor:
            cursor.execute("DELETE FROM api_key WHERE id = ?", (self.id,))
            return cursor.execute("SELECT changes()").fetchone()[0] > 0  # type: ignore[index]

    def record_use(self) -> None:
        """Atomic success counter (use_count = use_count + 1 is race-safe)."""
        now = datetime.now(UTC).isoformat()
        with transaction() as cursor:
            cursor.execute(
                "UPDATE api_key SET use_count = use_count + 1, last_used_at = ? WHERE id = ?",
                (now, self.id),
            )

    def is_valid(self) -> bool:
        """Enabled and not expired; an unparseable expiry fails closed."""
        if not self.enabled:
            return False
        if self.expires_at is None:
            return True
        try:
            expires = datetime.fromisoformat(self.expires_at)
        except ValueError:
            return False
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return datetime.now(UTC) <= expires

    def fixed_args(self) -> dict[str, Any]:
        if not self.fixed_args_json:
            return {}
        try:
            parsed = json.loads(self.fixed_args_json)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def parameterised_args(self) -> list[str] | None:
        """Whitelist of caller-suppliable arg names; None = no restriction."""
        if not self.parameterised_args_json:
            return None
        try:
            parsed = json.loads(self.parameterised_args_json)
        except ValueError:
            return None
        return [str(v) for v in parsed] if isinstance(parsed, list) else None
