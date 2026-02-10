"""Report access control model."""

from dataclasses import dataclass
from typing import Any

from strata.db import get_db, transaction

VALID_ACCESS_TYPES = ("user", "group")
VALID_PERMISSIONS = ("run", "edit")


@dataclass
class ReportAccess:
    id: int
    report_id: int
    access_type: str
    access_target: str
    permission: str

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> ReportAccess:
        return ReportAccess(
            id=int(row[0]),
            report_id=int(row[1]),
            access_type=str(row[2]),
            access_target=str(row[3]),
            permission=str(row[4]),
        )

    @staticmethod
    def get_for_report(report_id: int) -> list[ReportAccess]:
        db = get_db()
        rows = db.execute(
            "SELECT id, report_id, access_type, access_target, permission "
            "FROM report_access WHERE report_id = ? "
            "ORDER BY access_type, access_target",
            (report_id,),
        ).fetchall()
        return [ReportAccess._from_row(row) for row in rows]

    @staticmethod
    def create(
        report_id: int,
        access_type: str,
        access_target: str,
        permission: str,
    ) -> ReportAccess:
        with transaction() as cursor:
            cursor.execute(
                "INSERT OR IGNORE INTO report_access "
                "(report_id, access_type, access_target, permission) "
                "VALUES (?, ?, ?, ?)",
                (report_id, access_type, access_target, permission),
            )
            row = cursor.execute("SELECT last_insert_rowid()").fetchone()
            acl_id = int(row[0]) if row else 0

        return ReportAccess(
            id=acl_id,
            report_id=report_id,
            access_type=access_type,
            access_target=access_target,
            permission=permission,
        )

    def delete(self) -> bool:
        with transaction() as cursor:
            cursor.execute("DELETE FROM report_access WHERE id = ?", (self.id,))
            return cursor.execute("SELECT changes()").fetchone()[0] > 0  # type: ignore[index]
