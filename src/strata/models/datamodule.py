"""Datamodule model — the core object.

A datamodule is a persistent, versioned DuckDB database built by a script. The
live data is a pointer (``live_version_id``) to an immutable snapshot under
``versions/`` on disk, never a mutated-in-place file. Author-editable fields
(path, description, build_script, ttl, retention) are updated via ``update``;
the build-state fields (``build_status``, ``live_version_id``, lease columns)
are managed by ``datamodule_service`` under its build transactions.
"""

import uuid as uuid_lib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from strata.db import get_db, transaction


@dataclass
class Datamodule:
    id: int
    uuid: str
    path: str
    description: str
    build_script: str
    ttl_seconds: int | None
    valid_until: str | None
    retention_kind: str
    retention_count: int
    retention_age_seconds: int | None
    storage_version: str | None
    live_version_id: int | None
    build_status: str
    build_token: str | None
    build_lease_until: str | None
    last_build_error: str | None
    created_by: str
    modified_by: str
    created_at: str
    modified_at: str
    build_queue: str = "default"

    _COLUMNS = (
        "id, uuid, path, description, build_script, ttl_seconds, valid_until, "
        "retention_kind, retention_count, retention_age_seconds, storage_version, "
        "live_version_id, build_status, build_token, build_lease_until, last_build_error, "
        "created_by, modified_by, created_at, modified_at, build_queue"
    )

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> Datamodule:
        return Datamodule(
            id=int(row[0]),
            uuid=str(row[1]),
            path=str(row[2]),
            description=str(row[3]),
            build_script=str(row[4]),
            ttl_seconds=int(row[5]) if row[5] is not None else None,
            valid_until=str(row[6]) if row[6] is not None else None,
            retention_kind=str(row[7]),
            retention_count=int(row[8]),
            retention_age_seconds=int(row[9]) if row[9] is not None else None,
            storage_version=str(row[10]) if row[10] is not None else None,
            live_version_id=int(row[11]) if row[11] is not None else None,
            build_status=str(row[12]),
            build_token=str(row[13]) if row[13] is not None else None,
            build_lease_until=str(row[14]) if row[14] is not None else None,
            last_build_error=str(row[15]) if row[15] is not None else None,
            created_by=str(row[16]),
            modified_by=str(row[17]),
            created_at=str(row[18]),
            modified_at=str(row[19]),
            build_queue=str(row[20]),
        )

    @staticmethod
    def get_by_id(datamodule_id: int) -> Datamodule | None:
        db = get_db()
        row = db.execute(
            f"SELECT {Datamodule._COLUMNS} FROM datamodule WHERE id = ?", (datamodule_id,)
        ).fetchone()
        return Datamodule._from_row(row) if row else None

    @staticmethod
    def get_by_uuid(datamodule_uuid: str) -> Datamodule | None:
        db = get_db()
        row = db.execute(
            f"SELECT {Datamodule._COLUMNS} FROM datamodule WHERE uuid = ?", (datamodule_uuid,)
        ).fetchone()
        return Datamodule._from_row(row) if row else None

    @staticmethod
    def get_by_path(path: str) -> Datamodule | None:
        db = get_db()
        row = db.execute(
            f"SELECT {Datamodule._COLUMNS} FROM datamodule WHERE path = ?", (path.strip(),)
        ).fetchone()
        return Datamodule._from_row(row) if row else None

    @staticmethod
    def get_all() -> list[Datamodule]:
        db = get_db()
        rows = db.execute(f"SELECT {Datamodule._COLUMNS} FROM datamodule ORDER BY path").fetchall()
        return [Datamodule._from_row(row) for row in rows]

    @staticmethod
    def create(
        path: str,
        created_by: str,
        description: str = "",
        build_script: str = "",
        ttl_seconds: int | None = None,
    ) -> Datamodule:
        now = datetime.now(UTC).isoformat()
        dm_uuid = str(uuid_lib.uuid4())

        with transaction() as cursor:
            cursor.execute(
                "INSERT INTO datamodule (uuid, path, description, build_script, ttl_seconds, "
                "build_status, created_by, modified_by, created_at, modified_at) "
                "VALUES (?, ?, ?, ?, ?, 'never', ?, ?, ?, ?)",
                (
                    dm_uuid,
                    path.strip(),
                    description.strip(),
                    build_script,
                    ttl_seconds,
                    created_by,
                    created_by,
                    now,
                    now,
                ),
            )
            row = cursor.execute("SELECT last_insert_rowid()").fetchone()
            dm_id = int(row[0]) if row else 0

        created = Datamodule.get_by_id(dm_id)
        assert created is not None
        return created

    _UNSET: object = object()

    def update(
        self,
        modified_by: str,
        path: str | None = None,
        description: str | None = None,
        build_script: str | None = None,
        ttl_seconds: object = _UNSET,
        retention_kind: str | None = None,
        retention_count: int | None = None,
        retention_age_seconds: object = _UNSET,
        build_queue: str | None = None,
    ) -> bool:
        """Update author-editable fields. Build-state fields are service-managed."""
        updates: list[str] = []
        params: list[Any] = []

        if path is not None:
            updates.append("path = ?")
            params.append(path.strip())
        if description is not None:
            updates.append("description = ?")
            params.append(description.strip())
        if build_script is not None:
            updates.append("build_script = ?")
            params.append(build_script)
        if ttl_seconds is not Datamodule._UNSET:
            updates.append("ttl_seconds = ?")
            params.append(ttl_seconds)
        if retention_kind is not None:
            updates.append("retention_kind = ?")
            params.append(retention_kind)
        if retention_count is not None:
            updates.append("retention_count = ?")
            params.append(retention_count)
        if retention_age_seconds is not Datamodule._UNSET:
            updates.append("retention_age_seconds = ?")
            params.append(retention_age_seconds)
        if build_queue is not None:
            updates.append("build_queue = ?")
            params.append(build_queue)

        if not updates:
            return False

        now = datetime.now(UTC).isoformat()
        updates.append("modified_by = ?")
        params.append(modified_by)
        updates.append("modified_at = ?")
        params.append(now)
        params.append(self.id)

        with transaction() as cursor:
            cursor.execute(f"UPDATE datamodule SET {', '.join(updates)} WHERE id = ?", params)
        return True

    def delete(self) -> bool:
        with transaction() as cursor:
            cursor.execute("DELETE FROM datamodule WHERE id = ?", (self.id,))
            return cursor.execute("SELECT changes()").fetchone()[0] > 0  # type: ignore[index]
