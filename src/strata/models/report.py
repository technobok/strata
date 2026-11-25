"""Report model — a DuckDB macro layered on a datamodule.

kind='query' is a parameterised data report whose ``body_sql`` is an author
SELECT; kind='view' is reserved and unused (how rows are shown is the report's
``presentation``, rendered by the server). The macro
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
    created_by: str
    modified_by: str
    created_at: str
    modified_at: str
    # Why it does not run on the live data (migration 0014); None = it does, or unchecked.
    problem: str | None = None
    # How its rows are shown (migration 0015): Presentation JSON, None = no settings;
    # read it with services/presentation.of(report).
    presentation: str | None = None
    # What in those settings no longer fits the live columns; None = it all fits.
    presentation_problem: str | None = None

    _COLUMNS = (
        "id, uuid, datamodule_id, name, macro_name, kind, description, body_sql, "
        "created_by, modified_by, created_at, modified_at, problem, presentation, "
        "presentation_problem"
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
            created_by=str(row[8]),
            modified_by=str(row[9]),
            created_at=str(row[10]),
            modified_at=str(row[11]),
            problem=str(row[12]) if row[12] is not None else None,
            presentation=str(row[13]) if row[13] is not None else None,
            presentation_problem=str(row[14]) if row[14] is not None else None,
        )

    @staticmethod
    def set_problem(report_id: int, problem: str | None) -> None:
        """Record why the report does not run on the live data (None: it does)."""
        with transaction() as cur:
            cur.execute("UPDATE report SET problem = ? WHERE id = ?", (problem, report_id))

    @staticmethod
    def set_presentation(report_id: int, document: str | None, modified_by: str) -> None:
        """Store the report's presentation JSON (None: no settings); a change of look is
        a change of the report."""
        now = datetime.now(UTC).isoformat()
        with transaction() as cur:
            cur.execute(
                "UPDATE report SET presentation = ?, modified_by = ?, modified_at = ? WHERE id = ?",
                (document, modified_by, now, report_id),
            )

    @staticmethod
    def set_presentation_problem(report_id: int, problem: str | None) -> None:
        """Record what in the settings no longer fits the live columns (None: it fits)."""
        with transaction() as cur:
            cur.execute(
                "UPDATE report SET presentation_problem = ? WHERE id = ?", (problem, report_id)
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
    ) -> Report:
        now = datetime.now(UTC).isoformat()
        report_uuid = str(uuid_lib.uuid4())
        with transaction() as cursor:
            cursor.execute(
                "INSERT INTO report (uuid, datamodule_id, name, macro_name, kind, description, "
                "body_sql, created_by, modified_by, created_at, modified_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    report_uuid,
                    datamodule_id,
                    name.strip(),
                    macro_name.strip(),
                    kind,
                    description.strip(),
                    body_sql,
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

    def update(
        self,
        modified_by: str,
        name: str | None = None,
        macro_name: str | None = None,
        description: str | None = None,
        body_sql: str | None = None,
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
