"""Access log for API-key-authenticated serves (one row per keyed request)."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from strata.db import get_db, transaction


@dataclass
class ApiKeyAccessLog:
    id: int
    api_key_id: int
    report_id: int | None
    accessed_at: str
    remote_addr: str | None
    format: str | None
    row_count: int | None
    bytes_sent: int | None
    duration_ms: int | None
    status: int | None

    _COLUMNS = (
        "id, api_key_id, report_id, accessed_at, remote_addr, format, "
        "row_count, bytes_sent, duration_ms, status"
    )

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> ApiKeyAccessLog:
        return ApiKeyAccessLog(
            id=int(row[0]),
            api_key_id=int(row[1]),
            report_id=int(row[2]) if row[2] is not None else None,
            accessed_at=str(row[3]),
            remote_addr=str(row[4]) if row[4] is not None else None,
            format=str(row[5]) if row[5] is not None else None,
            row_count=int(row[6]) if row[6] is not None else None,
            bytes_sent=int(row[7]) if row[7] is not None else None,
            duration_ms=int(row[8]) if row[8] is not None else None,
            status=int(row[9]) if row[9] is not None else None,
        )

    @staticmethod
    def record(
        api_key_id: int,
        report_id: int | None,
        remote_addr: str | None,
        fmt: str | None,
        row_count: int | None,
        bytes_sent: int | None,
        duration_ms: int | None,
        status: int,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with transaction() as cursor:
            cursor.execute(
                "INSERT INTO api_key_access_log (api_key_id, report_id, accessed_at, "
                "remote_addr, format, row_count, bytes_sent, duration_ms, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    api_key_id,
                    report_id,
                    now,
                    remote_addr,
                    fmt,
                    row_count,
                    bytes_sent,
                    duration_ms,
                    status,
                ),
            )

    @staticmethod
    def list_for_key(api_key_id: int, limit: int = 50) -> list[ApiKeyAccessLog]:
        db = get_db()
        rows = db.execute(
            f"SELECT {ApiKeyAccessLog._COLUMNS} FROM api_key_access_log "
            "WHERE api_key_id = ? ORDER BY accessed_at DESC, id DESC LIMIT ?",
            (api_key_id, limit),
        ).fetchall()
        return [ApiKeyAccessLog._from_row(row) for row in rows]
