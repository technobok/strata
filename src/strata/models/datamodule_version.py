"""Datamodule version model — an immutable built snapshot.

Each successful build produces a ``versions/<uuid>.duckdb`` file and a
``ready`` row here; the datamodule's ``live_version_id`` points at the current
one. The success flip (version -> ready, datamodule.live_version_id, supersede
older versions) is performed atomically by ``datamodule_service`` in a single
SQLite transaction; this model covers creation, failure, and reads.
"""

import uuid as uuid_lib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from strata.db import get_db, transaction


@dataclass
class DatamoduleVersion:
    id: int
    uuid: str
    datamodule_id: int
    status: str
    build_started_at: str | None
    build_ended_at: str | None
    duration_ms: int | None
    triggered_by: str
    build_script_snapshot: str | None
    byte_size: int | None
    row_summary_json: str | None
    storage_version: str | None
    error_message: str | None
    created_at: str

    _COLUMNS = (
        "id, uuid, datamodule_id, status, build_started_at, build_ended_at, duration_ms, "
        "triggered_by, build_script_snapshot, byte_size, row_summary_json, storage_version, "
        "error_message, created_at"
    )

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> DatamoduleVersion:
        return DatamoduleVersion(
            id=int(row[0]),
            uuid=str(row[1]),
            datamodule_id=int(row[2]),
            status=str(row[3]),
            build_started_at=str(row[4]) if row[4] is not None else None,
            build_ended_at=str(row[5]) if row[5] is not None else None,
            duration_ms=int(row[6]) if row[6] is not None else None,
            triggered_by=str(row[7]),
            build_script_snapshot=str(row[8]) if row[8] is not None else None,
            byte_size=int(row[9]) if row[9] is not None else None,
            row_summary_json=str(row[10]) if row[10] is not None else None,
            storage_version=str(row[11]) if row[11] is not None else None,
            error_message=str(row[12]) if row[12] is not None else None,
            created_at=str(row[13]),
        )

    @staticmethod
    def get_by_id(version_id: int) -> DatamoduleVersion | None:
        db = get_db()
        row = db.execute(
            f"SELECT {DatamoduleVersion._COLUMNS} FROM datamodule_version WHERE id = ?",
            (version_id,),
        ).fetchone()
        return DatamoduleVersion._from_row(row) if row else None

    @staticmethod
    def get_for_datamodule(datamodule_id: int, version_uuid: str) -> DatamoduleVersion | None:
        """The version with this uuid iff it belongs to this datamodule (one query)."""
        db = get_db()
        row = db.execute(
            f"SELECT {DatamoduleVersion._COLUMNS} FROM datamodule_version "
            "WHERE datamodule_id = ? AND uuid = ?",
            (datamodule_id, version_uuid),
        ).fetchone()
        return DatamoduleVersion._from_row(row) if row else None

    @staticmethod
    def list_for(datamodule_id: int) -> list[DatamoduleVersion]:
        db = get_db()
        rows = db.execute(
            f"SELECT {DatamoduleVersion._COLUMNS} FROM datamodule_version "
            "WHERE datamodule_id = ? ORDER BY created_at DESC",
            (datamodule_id,),
        ).fetchall()
        return [DatamoduleVersion._from_row(row) for row in rows]

    @staticmethod
    def create_building(
        datamodule_id: int,
        triggered_by: str,
        build_script_snapshot: str,
    ) -> DatamoduleVersion:
        """Insert a ``building`` version row and return it (with its file uuid)."""
        now = datetime.now(UTC).isoformat()
        version_uuid = str(uuid_lib.uuid4())
        with transaction() as cursor:
            cursor.execute(
                "INSERT INTO datamodule_version (uuid, datamodule_id, status, build_started_at, "
                "triggered_by, build_script_snapshot, created_at) "
                "VALUES (?, ?, 'building', ?, ?, ?, ?)",
                (version_uuid, datamodule_id, now, triggered_by, build_script_snapshot, now),
            )
            row = cursor.execute("SELECT last_insert_rowid()").fetchone()
            version_id = int(row[0]) if row else 0
        created = DatamoduleVersion.get_by_id(version_id)
        assert created is not None
        return created

    def set_script_snapshot(self, script: str) -> None:
        """The script as it ran: a template build keeps its rendered text."""
        with transaction() as cursor:
            cursor.execute(
                "UPDATE datamodule_version SET build_script_snapshot = ? WHERE id = ?",
                (script, self.id),
            )
        self.build_script_snapshot = script

    def mark_failed(self, error_message: str, duration_ms: int | None) -> None:
        now = datetime.now(UTC).isoformat()
        with transaction() as cursor:
            cursor.execute(
                "UPDATE datamodule_version SET status = 'failed', build_ended_at = ?, "
                "duration_ms = ?, error_message = ? WHERE id = ?",
                (now, duration_ms, error_message, self.id),
            )
