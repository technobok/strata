"""Check settings of a report (one-to-one): a check run that finds rows is an exception."""

from dataclasses import dataclass
from typing import Any

from strata.db import get_db, transaction

SEVERITIES = ("info", "warning", "error")


@dataclass
class ReportCheck:
    report_id: int
    severity: str
    gate: bool  # also run inside each build of the datamodule; rows fail the build

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> ReportCheck:
        return ReportCheck(report_id=int(row[0]), severity=str(row[1]), gate=bool(row[2]))

    @staticmethod
    def get(report_id: int) -> ReportCheck | None:
        row = (
            get_db()
            .execute(
                "SELECT report_id, severity, gate FROM report_check WHERE report_id = ?",
                (report_id,),
            )
            .fetchone()
        )
        return ReportCheck._from_row(row) if row else None

    @staticmethod
    def for_datamodule(datamodule_id: int) -> dict[int, ReportCheck]:
        """report_id -> check, for every check on the datamodule."""
        rows = (
            get_db()
            .execute(
                "SELECT c.report_id, c.severity, c.gate FROM report_check c "
                "JOIN report r ON r.id = c.report_id WHERE r.datamodule_id = ?",
                (datamodule_id,),
            )
            .fetchall()
        )
        checks = [ReportCheck._from_row(r) for r in rows]
        return {c.report_id: c for c in checks}

    @staticmethod
    def save(report_id: int, severity: str, gate: bool) -> None:
        with transaction() as cur:
            cur.execute(
                "INSERT INTO report_check (report_id, severity, gate) VALUES (?, ?, ?) "
                "ON CONFLICT(report_id) DO UPDATE SET severity = excluded.severity, "
                "gate = excluded.gate",
                (report_id, severity, int(gate)),
            )

    @staticmethod
    def clear(report_id: int) -> None:
        with transaction() as cur:
            cur.execute("DELETE FROM report_check WHERE report_id = ?", (report_id,))
