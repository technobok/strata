"""Schedule model for report scheduling."""

import json
import uuid as uuid_lib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from strata.db import get_db, transaction


@dataclass
class Schedule:
    id: int
    uuid: str
    report_id: int
    name: str
    enabled: bool
    schedule_definition: dict[str, Any]
    parameters_json: str | None
    recipients_json: str
    max_inline_rows: int
    created_by: str
    created_at: str
    modified_at: str
    last_run_at: str | None
    next_run_at: str | None

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> Schedule:
        return Schedule(
            id=int(row[0]),
            uuid=str(row[1]),
            report_id=int(row[2]),
            name=str(row[3]),
            enabled=bool(row[4]),
            schedule_definition=json.loads(str(row[5])),
            parameters_json=str(row[6]) if row[6] is not None else None,
            recipients_json=str(row[7]),
            max_inline_rows=int(row[8]),
            created_by=str(row[9]),
            created_at=str(row[10]),
            modified_at=str(row[11]),
            last_run_at=str(row[12]) if row[12] is not None else None,
            next_run_at=str(row[13]) if row[13] is not None else None,
        )

    _COLUMNS = (
        "id, uuid, report_id, name, enabled, schedule_definition, "
        "parameters_json, recipients_json, max_inline_rows, created_by, "
        "created_at, modified_at, last_run_at, next_run_at"
    )

    @staticmethod
    def get_by_uuid(schedule_uuid: str) -> Schedule | None:
        db = get_db()
        row = db.execute(
            f"SELECT {Schedule._COLUMNS} FROM schedule WHERE uuid = ?",
            (schedule_uuid,),
        ).fetchone()
        return Schedule._from_row(row) if row else None

    @staticmethod
    def get_for_report(report_id: int) -> list[Schedule]:
        db = get_db()
        rows = db.execute(
            f"SELECT {Schedule._COLUMNS} FROM schedule WHERE report_id = ? ORDER BY name",
            (report_id,),
        ).fetchall()
        return [Schedule._from_row(row) for row in rows]

    @staticmethod
    def get_due(now_iso: str) -> list[Schedule]:
        """Get all enabled schedules that are due to run."""
        db = get_db()
        rows = db.execute(
            f"SELECT {Schedule._COLUMNS} FROM schedule "
            "WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?",
            (now_iso,),
        ).fetchall()
        return [Schedule._from_row(row) for row in rows]

    @staticmethod
    def create(
        report_id: int,
        name: str,
        schedule_definition: dict[str, Any],
        recipients: list[str],
        created_by: str,
        parameters: dict[str, Any] | None = None,
        max_inline_rows: int = 100,
    ) -> Schedule:
        now = datetime.now(UTC).isoformat()
        schedule_uuid = str(uuid_lib.uuid4())
        def_json = json.dumps(schedule_definition)
        params_json = json.dumps(parameters) if parameters else None
        recipients_json = json.dumps(recipients)

        # Calculate initial next_run_at
        from strata.services.schedule_service import next_run

        next_run_at = next_run(schedule_definition, datetime.now(UTC))
        next_run_iso = next_run_at.isoformat() if next_run_at else None

        with transaction() as cursor:
            cursor.execute(
                "INSERT INTO schedule "
                "(uuid, report_id, name, enabled, schedule_definition, "
                "parameters_json, recipients_json, max_inline_rows, "
                "created_by, created_at, modified_at, next_run_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    schedule_uuid,
                    report_id,
                    name.strip(),
                    def_json,
                    params_json,
                    recipients_json,
                    max_inline_rows,
                    created_by,
                    now,
                    now,
                    next_run_iso,
                ),
            )
            row = cursor.execute("SELECT last_insert_rowid()").fetchone()
            schedule_id = int(row[0]) if row else 0

        return Schedule(
            id=schedule_id,
            uuid=schedule_uuid,
            report_id=report_id,
            name=name.strip(),
            enabled=True,
            schedule_definition=schedule_definition,
            parameters_json=params_json,
            recipients_json=recipients_json,
            max_inline_rows=max_inline_rows,
            created_by=created_by,
            created_at=now,
            modified_at=now,
            last_run_at=None,
            next_run_at=next_run_iso,
        )

    def update(
        self,
        name: str | None = None,
        schedule_definition: dict[str, Any] | None = None,
        recipients: list[str] | None = None,
        parameters: dict[str, Any] | None = ...,  # type: ignore[assignment]
        max_inline_rows: int | None = None,
        enabled: bool | None = None,
    ) -> bool:
        updates: list[str] = []
        params: list[Any] = []

        if name is not None:
            updates.append("name = ?")
            params.append(name.strip())
        if schedule_definition is not None:
            updates.append("schedule_definition = ?")
            params.append(json.dumps(schedule_definition))
        if recipients is not None:
            updates.append("recipients_json = ?")
            params.append(json.dumps(recipients))
        if parameters is not ...:
            updates.append("parameters_json = ?")
            params.append(json.dumps(parameters) if parameters else None)
        if max_inline_rows is not None:
            updates.append("max_inline_rows = ?")
            params.append(max_inline_rows)
        if enabled is not None:
            updates.append("enabled = ?")
            params.append(int(enabled))

        if not updates:
            return False

        now = datetime.now(UTC).isoformat()
        updates.append("modified_at = ?")
        params.append(now)

        # Recalculate next_run_at if schedule definition changed
        if schedule_definition is not None:
            from strata.services.schedule_service import next_run

            next_run_dt = next_run(schedule_definition, datetime.now(UTC))
            updates.append("next_run_at = ?")
            params.append(next_run_dt.isoformat() if next_run_dt else None)

        params.append(self.id)

        with transaction() as cursor:
            cursor.execute(
                f"UPDATE schedule SET {', '.join(updates)} WHERE id = ?",
                params,
            )

        if name is not None:
            self.name = name.strip()
        if schedule_definition is not None:
            self.schedule_definition = schedule_definition
        self.modified_at = now
        return True

    def update_after_run(self, next_run_iso: str | None) -> None:
        """Update schedule after a successful run."""
        now = datetime.now(UTC).isoformat()
        enabled = 1 if next_run_iso else 0

        with transaction() as cursor:
            cursor.execute(
                "UPDATE schedule SET last_run_at = ?, next_run_at = ?, enabled = ? WHERE id = ?",
                (now, next_run_iso, enabled, self.id),
            )

        self.last_run_at = now
        self.next_run_at = next_run_iso
        self.enabled = bool(enabled)

    def delete(self) -> bool:
        with transaction() as cursor:
            cursor.execute("DELETE FROM schedule WHERE id = ?", (self.id,))
            return cursor.execute("SELECT changes()").fetchone()[0] > 0  # type: ignore[index]

    def get_recipients(self) -> list[str]:
        return json.loads(self.recipients_json)

    def get_parameters(self) -> dict[str, Any]:
        if not self.parameters_json:
            return {}
        return json.loads(self.parameters_json)
