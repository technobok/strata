"""Connection model: encrypted external-database connection definitions."""

from __future__ import annotations

import uuid as uuid_lib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from strata.db import get_db, transaction
from strata.services.connection_service import decrypt_params, encrypt_params


@dataclass
class Connection:
    id: int
    uuid: str
    name: str
    driver: str
    description: str
    created_by: str
    modified_by: str
    created_at: str
    modified_at: str
    params: dict = field(default_factory=dict)

    _COLUMNS = (
        "id, uuid, name, driver, params_encrypted, description, "
        "created_by, modified_by, created_at, modified_at"
    )

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> Connection:
        return Connection(
            id=int(row[0]),
            uuid=str(row[1]),
            name=str(row[2]),
            driver=str(row[3]),
            params=decrypt_params(str(row[4])),
            description=str(row[5]),
            created_by=str(row[6]),
            modified_by=str(row[7]),
            created_at=str(row[8]),
            modified_at=str(row[9]),
        )

    @staticmethod
    def get_by_id(connection_id: int) -> Connection | None:
        db = get_db()
        row = db.execute(
            f"SELECT {Connection._COLUMNS} FROM connection WHERE id = ?", (connection_id,)
        ).fetchone()
        return Connection._from_row(row) if row else None

    @staticmethod
    def get_by_uuid(connection_uuid: str) -> Connection | None:
        db = get_db()
        row = db.execute(
            f"SELECT {Connection._COLUMNS} FROM connection WHERE uuid = ?", (connection_uuid,)
        ).fetchone()
        return Connection._from_row(row) if row else None

    @staticmethod
    def get_by_name(name: str) -> Connection | None:
        db = get_db()
        row = db.execute(
            f"SELECT {Connection._COLUMNS} FROM connection WHERE name = ?", (name,)
        ).fetchone()
        return Connection._from_row(row) if row else None

    @staticmethod
    def get_all() -> list[Connection]:
        db = get_db()
        rows = db.execute(f"SELECT {Connection._COLUMNS} FROM connection ORDER BY name").fetchall()
        return [Connection._from_row(row) for row in rows]

    @staticmethod
    def create(
        name: str,
        driver: str,
        params: dict,
        created_by: str,
        description: str = "",
    ) -> Connection:
        now = datetime.now(UTC).isoformat()
        conn_uuid = str(uuid_lib.uuid4())
        encrypted = encrypt_params(params)

        with transaction() as cursor:
            cursor.execute(
                "INSERT INTO connection (uuid, name, driver, params_encrypted, description, "
                "created_by, modified_by, created_at, modified_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    conn_uuid,
                    name.strip(),
                    driver,
                    encrypted,
                    description.strip(),
                    created_by,
                    created_by,
                    now,
                    now,
                ),
            )
            row = cursor.execute("SELECT last_insert_rowid()").fetchone()
            conn_id = int(row[0]) if row else 0

        return Connection(
            id=conn_id,
            uuid=conn_uuid,
            name=name.strip(),
            driver=driver,
            params=params,
            description=description.strip(),
            created_by=created_by,
            modified_by=created_by,
            created_at=now,
            modified_at=now,
        )

    def update(
        self,
        modified_by: str,
        name: str | None = None,
        driver: str | None = None,
        params: dict | None = None,
        description: str | None = None,
    ) -> bool:
        updates: list[str] = []
        sql_params: list[Any] = []

        if name is not None:
            updates.append("name = ?")
            sql_params.append(name.strip())
        if driver is not None:
            updates.append("driver = ?")
            sql_params.append(driver)
        if params is not None:
            updates.append("params_encrypted = ?")
            sql_params.append(encrypt_params(params))
        if description is not None:
            updates.append("description = ?")
            sql_params.append(description.strip())

        if not updates:
            return False

        now = datetime.now(UTC).isoformat()
        updates.append("modified_by = ?")
        sql_params.append(modified_by)
        updates.append("modified_at = ?")
        sql_params.append(now)
        sql_params.append(self.id)

        with transaction() as cursor:
            cursor.execute(
                f"UPDATE connection SET {', '.join(updates)} WHERE id = ?",
                sql_params,
            )

        if name is not None:
            self.name = name.strip()
        if driver is not None:
            self.driver = driver
        if params is not None:
            self.params = params
        if description is not None:
            self.description = description.strip()
        self.modified_by = modified_by
        self.modified_at = now
        return True

    def delete(self) -> bool:
        with transaction() as cursor:
            cursor.execute("DELETE FROM connection WHERE id = ?", (self.id,))
            return cursor.execute("SELECT changes()").fetchone()[0] > 0  # type: ignore[index]
