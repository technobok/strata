"""Schedule model — when a datamodule is rebuilt (services/schedule_service).

Uses the baseline ``schedule`` table. Phase B runs ``action='rebuild'``
only; the email columns are reserved for Phase C's notify checks.
``next_run_at``/``last_run_at`` are UTC ISO strings.
"""

import uuid as uuid_lib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from strata.db import get_db, transaction


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class Schedule:
    id: int
    uuid: str
    datamodule_id: int
    name: str
    action: str
    enabled: bool
    schedule_definition: str
    created_by: str
    created_at: str
    modified_at: str
    last_run_at: str | None
    next_run_at: str | None

    _COLUMNS = (
        "id, uuid, datamodule_id, name, action, enabled, schedule_definition, created_by, "
        "created_at, modified_at, last_run_at, next_run_at"
    )

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> Schedule:
        return Schedule(
            id=int(row[0]),
            uuid=str(row[1]),
            datamodule_id=int(row[2]),
            name=str(row[3]),
            action=str(row[4]),
            enabled=bool(row[5]),
            schedule_definition=str(row[6]),
            created_by=str(row[7]),
            created_at=str(row[8]),
            modified_at=str(row[9]),
            last_run_at=str(row[10]) if row[10] is not None else None,
            next_run_at=str(row[11]) if row[11] is not None else None,
        )

    @staticmethod
    def get_by_uuid(schedule_uuid: str) -> Schedule | None:
        row = (
            get_db()
            .execute(f"SELECT {Schedule._COLUMNS} FROM schedule WHERE uuid = ?", (schedule_uuid,))
            .fetchone()
        )
        return Schedule._from_row(row) if row else None

    @staticmethod
    def for_datamodule(datamodule_id: int) -> list[Schedule]:
        rows = (
            get_db()
            .execute(
                f"SELECT {Schedule._COLUMNS} FROM schedule WHERE datamodule_id = ? ORDER BY name",
                (datamodule_id,),
            )
            .fetchall()
        )
        return [Schedule._from_row(r) for r in rows]

    @staticmethod
    def due(now: str) -> list[Schedule]:
        rows = (
            get_db()
            .execute(
                f"SELECT {Schedule._COLUMNS} FROM schedule WHERE enabled = 1 "
                "AND action = 'rebuild' AND next_run_at IS NOT NULL AND next_run_at <= ? "
                "ORDER BY next_run_at",
                (now,),
            )
            .fetchall()
        )
        return [Schedule._from_row(r) for r in rows]

    @staticmethod
    def create(
        datamodule_id: int,
        name: str,
        definition_json: str,
        next_run_at: str | None,
        created_by: str,
        enabled: bool = True,
    ) -> Schedule:
        now = _now()
        schedule_uuid = str(uuid_lib.uuid4())
        with transaction() as cur:
            cur.execute(
                "INSERT INTO schedule (uuid, datamodule_id, name, action, enabled, "
                "schedule_definition, created_by, created_at, modified_at, next_run_at) "
                "VALUES (?, ?, ?, 'rebuild', ?, ?, ?, ?, ?, ?)",
                (
                    schedule_uuid,
                    datamodule_id,
                    name,
                    int(enabled),
                    definition_json,
                    created_by,
                    now,
                    now,
                    next_run_at,
                ),
            )
        created = Schedule.get_by_uuid(schedule_uuid)
        assert created is not None
        return created

    def update(
        self, name: str, definition_json: str, next_run_at: str | None, enabled: bool
    ) -> None:
        with transaction() as cur:
            cur.execute(
                "UPDATE schedule SET name = ?, schedule_definition = ?, next_run_at = ?, "
                "enabled = ?, modified_at = ? WHERE id = ?",
                (name, definition_json, next_run_at, int(enabled), _now(), self.id),
            )

    def set_enabled(self, enabled: bool, next_run_at: str | None) -> None:
        with transaction() as cur:
            cur.execute(
                "UPDATE schedule SET enabled = ?, next_run_at = ?, modified_at = ? WHERE id = ?",
                (int(enabled), next_run_at, _now(), self.id),
            )

    def claim_run(self, ran_at: str, next_run_at: str | None) -> bool:
        """Record this run, if no other worker has (compare-and-set on next_run_at)."""
        with transaction() as cur:
            cur.execute(
                "UPDATE schedule SET last_run_at = ?, next_run_at = ? "
                "WHERE id = ? AND next_run_at IS ?",
                (ran_at, next_run_at, self.id, self.next_run_at),
            )
            return int(cur.execute("SELECT changes()").fetchone()[0]) > 0  # type: ignore[index]

    def delete(self) -> None:
        with transaction() as cur:
            cur.execute("DELETE FROM schedule WHERE id = ?", (self.id,))
