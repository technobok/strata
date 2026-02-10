"""Report model for report definitions."""

import uuid as uuid_lib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from strata.db import get_db, transaction


@dataclass
class Report:
    id: int
    uuid: str
    name: str
    description: str
    sql_template: str
    created_by: str
    modified_by: str
    created_at: str
    modified_at: str

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> Report:
        return Report(
            id=int(row[0]),
            uuid=str(row[1]),
            name=str(row[2]),
            description=str(row[3]),
            sql_template=str(row[4]),
            created_by=str(row[5]),
            modified_by=str(row[6]),
            created_at=str(row[7]),
            modified_at=str(row[8]),
        )

    _COLUMNS = "id, uuid, name, description, sql_template, created_by, modified_by, created_at, modified_at"

    @staticmethod
    def get_by_id(report_id: int) -> Report | None:
        db = get_db()
        row = db.execute(
            f"SELECT {Report._COLUMNS} FROM report WHERE id = ?", (report_id,)
        ).fetchone()
        return Report._from_row(row) if row else None

    @staticmethod
    def get_by_uuid(report_uuid: str) -> Report | None:
        db = get_db()
        row = db.execute(
            f"SELECT {Report._COLUMNS} FROM report WHERE uuid = ?", (report_uuid,)
        ).fetchone()
        return Report._from_row(row) if row else None

    @staticmethod
    def get_all() -> list[Report]:
        db = get_db()
        rows = db.execute(
            f"SELECT {Report._COLUMNS} FROM report ORDER BY modified_at DESC"
        ).fetchall()
        return [Report._from_row(row) for row in rows]

    @staticmethod
    def create(
        name: str,
        sql_template: str,
        created_by: str,
        description: str = "",
    ) -> Report:
        now = datetime.now(UTC).isoformat()
        report_uuid = str(uuid_lib.uuid4())

        with transaction() as cursor:
            cursor.execute(
                "INSERT INTO report (uuid, name, description, sql_template, "
                "created_by, modified_by, created_at, modified_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    report_uuid,
                    name.strip(),
                    description.strip(),
                    sql_template,
                    created_by,
                    created_by,
                    now,
                    now,
                ),
            )
            row = cursor.execute("SELECT last_insert_rowid()").fetchone()
            report_id = int(row[0]) if row else 0

        return Report(
            id=report_id,
            uuid=report_uuid,
            name=name.strip(),
            description=description.strip(),
            sql_template=sql_template,
            created_by=created_by,
            modified_by=created_by,
            created_at=now,
            modified_at=now,
        )

    def update(
        self,
        modified_by: str,
        name: str | None = None,
        description: str | None = None,
        sql_template: str | None = None,
    ) -> bool:
        updates: list[str] = []
        params: list[Any] = []

        if name is not None:
            updates.append("name = ?")
            params.append(name.strip())
        if description is not None:
            updates.append("description = ?")
            params.append(description.strip())
        if sql_template is not None:
            updates.append("sql_template = ?")
            params.append(sql_template)

        if not updates:
            return False

        now = datetime.now(UTC).isoformat()
        updates.append("modified_by = ?")
        params.append(modified_by)
        updates.append("modified_at = ?")
        params.append(now)
        params.append(self.id)

        with transaction() as cursor:
            cursor.execute(
                f"UPDATE report SET {', '.join(updates)} WHERE id = ?",
                params,
            )

        if name is not None:
            self.name = name.strip()
        if description is not None:
            self.description = description.strip()
        if sql_template is not None:
            self.sql_template = sql_template
        self.modified_by = modified_by
        self.modified_at = now
        return True

    def delete(self) -> bool:
        with transaction() as cursor:
            cursor.execute("DELETE FROM report WHERE id = ?", (self.id,))
            return cursor.execute("SELECT changes()").fetchone()[0] > 0  # type: ignore[index]
