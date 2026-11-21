"""One run of a report or check, with its rows saved as a Parquet snapshot."""

import json
import uuid as uuid_lib
from dataclasses import dataclass
from typing import Any

from strata.db import get_db, transaction


@dataclass
class ReportRun:
    id: int
    uuid: str
    report_id: int
    schedule_id: int | None
    version_id: int | None
    args: dict[str, list[str]]
    status: str  # ok (rows) | empty (no rows) | error (the query failed)
    row_count: int | None
    data_as_of: str | None
    data_note: str | None
    error: str | None
    triggered_by: str
    started_at: str
    finished_at: str
    notified_at: str | None
    snapshot_path: str | None
    notify_note: str | None = None  # who the mail went to, who could not be resolved, or why not

    _COLUMNS = (
        "id, uuid, report_id, schedule_id, version_id, args_json, status, row_count, "
        "data_as_of, data_note, error, triggered_by, started_at, finished_at, notified_at, "
        "snapshot_path, notify_note"
    )

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> ReportRun:
        return ReportRun(
            id=int(row[0]),
            uuid=str(row[1]),
            report_id=int(row[2]),
            schedule_id=int(row[3]) if row[3] is not None else None,
            version_id=int(row[4]) if row[4] is not None else None,
            args=json.loads(row[5]) if row[5] else {},
            status=str(row[6]),
            row_count=int(row[7]) if row[7] is not None else None,
            data_as_of=row[8],
            data_note=row[9],
            error=row[10],
            triggered_by=str(row[11]),
            started_at=str(row[12]),
            finished_at=str(row[13]),
            notified_at=row[14],
            snapshot_path=row[15],
            notify_note=row[16],
        )

    @staticmethod
    def new_uuid() -> str:
        return str(uuid_lib.uuid4())

    @staticmethod
    def create(
        *,
        run_uuid: str,
        report_id: int,
        schedule_id: int | None,
        version_id: int | None,
        args: dict[str, list[str]],
        status: str,
        row_count: int | None,
        data_as_of: str | None,
        data_note: str | None,
        error: str | None,
        triggered_by: str,
        started_at: str,
        finished_at: str,
        snapshot_path: str | None,
    ) -> ReportRun:
        with transaction() as cur:
            cur.execute(
                "INSERT INTO report_run (uuid, report_id, schedule_id, version_id, args_json, "
                "status, row_count, data_as_of, data_note, error, triggered_by, started_at, "
                "finished_at, snapshot_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_uuid,
                    report_id,
                    schedule_id,
                    version_id,
                    json.dumps(args, sort_keys=True),
                    status,
                    row_count,
                    data_as_of,
                    data_note,
                    error,
                    triggered_by,
                    started_at,
                    finished_at,
                    snapshot_path,
                ),
            )
        run = ReportRun.get_by_uuid(run_uuid)
        assert run is not None
        return run

    def set_notified(self, notified_at: str | None, note: str) -> None:
        with transaction() as cur:
            cur.execute(
                "UPDATE report_run SET notified_at = ?, notify_note = ? WHERE id = ?",
                (notified_at, note, self.id),
            )
        self.notified_at, self.notify_note = notified_at, note

    @staticmethod
    def get_by_id(run_id: int) -> ReportRun | None:
        row = (
            get_db()
            .execute(f"SELECT {ReportRun._COLUMNS} FROM report_run WHERE id = ?", (run_id,))
            .fetchone()
        )
        return ReportRun._from_row(row) if row else None

    @staticmethod
    def get_by_uuid(run_uuid: str) -> ReportRun | None:
        row = (
            get_db()
            .execute(f"SELECT {ReportRun._COLUMNS} FROM report_run WHERE uuid = ?", (run_uuid,))
            .fetchone()
        )
        return ReportRun._from_row(row) if row else None

    @staticmethod
    def uuids_for(ids: list[int]) -> dict[int, str]:
        if not ids:
            return {}
        marks = ", ".join("?" for _ in ids)
        rows = get_db().execute(f"SELECT id, uuid FROM report_run WHERE id IN ({marks})", ids)
        return {int(i): str(u) for i, u in rows}

    @staticmethod
    def recent_for_report(report_id: int, limit: int = 20) -> list[ReportRun]:
        rows = (
            get_db()
            .execute(
                f"SELECT {ReportRun._COLUMNS} FROM report_run WHERE report_id = ? "
                "ORDER BY started_at DESC, id DESC LIMIT ?",
                (report_id, limit),
            )
            .fetchall()
        )
        return [ReportRun._from_row(r) for r in rows]

    @staticmethod
    def for_datamodule(datamodule_id: int) -> list[ReportRun]:
        rows = (
            get_db()
            .execute(
                f"SELECT {', '.join('rr.' + c.strip() for c in ReportRun._COLUMNS.split(','))} "
                "FROM report_run rr JOIN report r ON r.id = rr.report_id "
                "WHERE r.datamodule_id = ?",
                (datamodule_id,),
            )
            .fetchall()
        )
        return [ReportRun._from_row(r) for r in rows]

    @staticmethod
    def started_before(cutoff: str) -> list[tuple[ReportRun, str]]:
        """(run, datamodule uuid) for every run that started before ``cutoff``."""
        cols = ", ".join("rr." + c.strip() for c in ReportRun._COLUMNS.split(","))
        rows = (
            get_db()
            .execute(
                f"SELECT {cols}, d.uuid FROM report_run rr JOIN report r ON r.id = rr.report_id "
                "JOIN datamodule d ON d.id = r.datamodule_id WHERE rr.started_at < ?",
                (cutoff,),
            )
            .fetchall()
        )
        return [(ReportRun._from_row(r[:-1]), str(r[-1])) for r in rows]

    @staticmethod
    def delete_ids(ids: list[int]) -> int:
        if not ids:
            return 0
        with transaction() as cur:
            cur.executemany("DELETE FROM report_run WHERE id = ?", [(i,) for i in ids])
        return len(ids)
