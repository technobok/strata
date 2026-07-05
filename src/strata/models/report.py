"""Report model — a DuckDB macro layered on a datamodule.

kind='query' is a parameterised data report whose ``body_sql`` is an author
SELECT; kind='view' (Phase 7) is a system-generated formatting macro. The macro
body is the source of truth in SQLite and is registered as a TEMP macro over the
read-only live version at serve time (see report_service), so editing a body is
an instant UPDATE with no rebuild.
"""

import uuid as uuid_lib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from strata.db import get_db, transaction


@dataclass
class Report:
    id: int
    uuid: str
    datamodule_id: int
    name: str
    macro_name: str
    kind: str
    description: str
    body_sql: str
    ttl_seconds: int | None
    default_format: str
    created_by: str
    modified_by: str
    created_at: str
    modified_at: str

    _COLUMNS = (
        "id, uuid, datamodule_id, name, macro_name, kind, description, body_sql, "
        "ttl_seconds, default_format, created_by, modified_by, created_at, modified_at"
    )

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> Report:
        return Report(
            id=int(row[0]),
            uuid=str(row[1]),
            datamodule_id=int(row[2]),
            name=str(row[3]),
            macro_name=str(row[4]),
            kind=str(row[5]),
            description=str(row[6]),
            body_sql=str(row[7]),
            ttl_seconds=int(row[8]) if row[8] is not None else None,
            default_format=str(row[9]),
            created_by=str(row[10]),
            modified_by=str(row[11]),
            created_at=str(row[12]),
            modified_at=str(row[13]),
        )

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
    def get_for_datamodule(datamodule_id: int) -> list[Report]:
        db = get_db()
        rows = db.execute(
            f"SELECT {Report._COLUMNS} FROM report WHERE datamodule_id = ? ORDER BY name",
            (datamodule_id,),
        ).fetchall()
        return [Report._from_row(row) for row in rows]

    @staticmethod
    def create(
        datamodule_id: int,
        name: str,
        macro_name: str,
        body_sql: str,
        created_by: str,
        kind: str = "query",
        description: str = "",
        ttl_seconds: int | None = None,
        default_format: str = "html",
    ) -> Report:
        now = datetime.now(UTC).isoformat()
        report_uuid = str(uuid_lib.uuid4())
        with transaction() as cursor:
            cursor.execute(
                "INSERT INTO report (uuid, datamodule_id, name, macro_name, kind, description, "
                "body_sql, ttl_seconds, default_format, created_by, modified_by, created_at, "
                "modified_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    report_uuid,
                    datamodule_id,
                    name.strip(),
                    macro_name.strip(),
                    kind,
                    description.strip(),
                    body_sql,
                    ttl_seconds,
                    default_format,
                    created_by,
                    created_by,
                    now,
                    now,
                ),
            )
            row = cursor.execute("SELECT last_insert_rowid()").fetchone()
            report_id = int(row[0]) if row else 0
        created = Report.get_by_id(report_id)
        assert created is not None
        return created

    _UNSET: object = object()

    def update(
        self,
        modified_by: str,
        name: str | None = None,
        macro_name: str | None = None,
        description: str | None = None,
        body_sql: str | None = None,
        ttl_seconds: object = _UNSET,
        default_format: str | None = None,
    ) -> bool:
        updates: list[str] = []
        params: list[Any] = []

        if name is not None:
            updates.append("name = ?")
            params.append(name.strip())
        if macro_name is not None:
            updates.append("macro_name = ?")
            params.append(macro_name.strip())
        if description is not None:
            updates.append("description = ?")
            params.append(description.strip())
        if body_sql is not None:
            updates.append("body_sql = ?")
            params.append(body_sql)
        if ttl_seconds is not Report._UNSET:
            updates.append("ttl_seconds = ?")
            params.append(ttl_seconds)
        if default_format is not None:
            updates.append("default_format = ?")
            params.append(default_format)

        if not updates:
            return False

        now = datetime.now(UTC).isoformat()
        updates.append("modified_by = ?")
        params.append(modified_by)
        updates.append("modified_at = ?")
        params.append(now)
        params.append(self.id)

        with transaction() as cursor:
            cursor.execute(f"UPDATE report SET {', '.join(updates)} WHERE id = ?", params)
        return True

    def delete(self) -> bool:
        with transaction() as cursor:
            cursor.execute("DELETE FROM report WHERE id = ?", (self.id,))
            return cursor.execute("SELECT changes()").fetchone()[0] > 0  # type: ignore[index]
