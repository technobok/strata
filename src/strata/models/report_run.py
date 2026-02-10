"""Report run history model."""

import json
import uuid as uuid_lib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from strata.db import get_db, transaction


@dataclass
class ReportRun:
    id: int
    uuid: str
    report_id: int
    parameters_json: str | None
    status: str
    row_count: int | None
    column_info_json: str | None
    result_hash: str | None
    error_message: str | None
    run_by: str
    started_at: str
    completed_at: str | None
    duration_ms: int | None

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> ReportRun:
        return ReportRun(
            id=int(row[0]),
            uuid=str(row[1]),
            report_id=int(row[2]),
            parameters_json=str(row[3]) if row[3] is not None else None,
            status=str(row[4]),
            row_count=int(row[5]) if row[5] is not None else None,
            column_info_json=str(row[6]) if row[6] is not None else None,
            result_hash=str(row[7]) if row[7] is not None else None,
            error_message=str(row[8]) if row[8] is not None else None,
            run_by=str(row[9]),
            started_at=str(row[10]),
            completed_at=str(row[11]) if row[11] is not None else None,
            duration_ms=int(row[12]) if row[12] is not None else None,
        )

    _COLUMNS = (
        "id, uuid, report_id, parameters_json, status, row_count, "
        "column_info_json, result_hash, error_message, run_by, "
        "started_at, completed_at, duration_ms"
    )

    @staticmethod
    def get_by_uuid(run_uuid: str) -> ReportRun | None:
        db = get_db()
        row = db.execute(
            f"SELECT {ReportRun._COLUMNS} FROM report_run WHERE uuid = ?",
            (run_uuid,),
        ).fetchone()
        return ReportRun._from_row(row) if row else None

    @staticmethod
    def get_for_report(report_id: int, limit: int = 50) -> list[ReportRun]:
        db = get_db()
        rows = db.execute(
            f"SELECT {ReportRun._COLUMNS} FROM report_run "
            "WHERE report_id = ? ORDER BY started_at DESC LIMIT ?",
            (report_id, limit),
        ).fetchall()
        return [ReportRun._from_row(row) for row in rows]

    @staticmethod
    def get_recent(limit: int = 20) -> list[ReportRun]:
        db = get_db()
        rows = db.execute(
            f"SELECT {ReportRun._COLUMNS} FROM report_run ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [ReportRun._from_row(row) for row in rows]

    @staticmethod
    def create_running(
        report_id: int,
        run_by: str,
        parameters: dict[str, Any] | None = None,
    ) -> ReportRun:
        now = datetime.now(UTC).isoformat()
        run_uuid = str(uuid_lib.uuid4())
        params_json = json.dumps(parameters) if parameters else None

        with transaction() as cursor:
            cursor.execute(
                "INSERT INTO report_run "
                "(uuid, report_id, parameters_json, status, run_by, started_at) "
                "VALUES (?, ?, ?, 'running', ?, ?)",
                (run_uuid, report_id, params_json, run_by, now),
            )
            row = cursor.execute("SELECT last_insert_rowid()").fetchone()
            run_id = int(row[0]) if row else 0

        return ReportRun(
            id=run_id,
            uuid=run_uuid,
            report_id=report_id,
            parameters_json=params_json,
            status="running",
            row_count=None,
            column_info_json=None,
            result_hash=None,
            error_message=None,
            run_by=run_by,
            started_at=now,
            completed_at=None,
            duration_ms=None,
        )

    def mark_completed(
        self,
        row_count: int,
        column_info: list[dict[str, str]],
        result_hash: str,
        duration_ms: int,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        col_json = json.dumps(column_info)

        with transaction() as cursor:
            cursor.execute(
                "UPDATE report_run SET status = 'completed', row_count = ?, "
                "column_info_json = ?, result_hash = ?, "
                "completed_at = ?, duration_ms = ? WHERE id = ?",
                (row_count, col_json, result_hash, now, duration_ms, self.id),
            )

        self.status = "completed"
        self.row_count = row_count
        self.column_info_json = col_json
        self.result_hash = result_hash
        self.completed_at = now
        self.duration_ms = duration_ms

    def mark_failed(self, error_message: str, duration_ms: int) -> None:
        now = datetime.now(UTC).isoformat()

        with transaction() as cursor:
            cursor.execute(
                "UPDATE report_run SET status = 'failed', error_message = ?, "
                "completed_at = ?, duration_ms = ? WHERE id = ?",
                (error_message, now, duration_ms, self.id),
            )

        self.status = "failed"
        self.error_message = error_message
        self.completed_at = now
        self.duration_ms = duration_ms

    @staticmethod
    def purge_old(days: int) -> int:
        """Delete runs older than the given number of days. Returns count deleted."""
        from datetime import timedelta

        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()

        with transaction() as cursor:
            cursor.execute("DELETE FROM report_run WHERE completed_at < ?", (cutoff,))
            deleted = cursor.execute("SELECT changes()").fetchone()
            count = int(deleted[0]) if deleted else 0

        return count

    def get_column_info(self) -> list[dict[str, str]]:
        if not self.column_info_json:
            return []
        return json.loads(self.column_info_json)

    def get_parameters(self) -> dict[str, Any]:
        if not self.parameters_json:
            return {}
        return json.loads(self.parameters_json)
