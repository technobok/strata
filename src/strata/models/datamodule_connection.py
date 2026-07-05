"""Datamodule connection model — bind an external connection under a local_name.

At build time datamodule_service loads the referenced ``connection`` and calls
``connection_service.attach_into(conn, local_name, driver, params)``, so the
build script can pull from SQLite/Postgres (as ``local_name.table``) or ODBC.
"""

from dataclasses import dataclass
from typing import Any

from strata.db import get_db, transaction


@dataclass
class DatamoduleConnection:
    id: int
    datamodule_id: int
    connection_id: int
    local_name: str

    _COLUMNS = "id, datamodule_id, connection_id, local_name"

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> DatamoduleConnection:
        return DatamoduleConnection(
            id=int(row[0]),
            datamodule_id=int(row[1]),
            connection_id=int(row[2]),
            local_name=str(row[3]),
        )

    @staticmethod
    def get_by_id(link_id: int) -> DatamoduleConnection | None:
        db = get_db()
        row = db.execute(
            f"SELECT {DatamoduleConnection._COLUMNS} FROM datamodule_connection WHERE id = ?",
            (link_id,),
        ).fetchone()
        return DatamoduleConnection._from_row(row) if row else None

    @staticmethod
    def get_for_datamodule(datamodule_id: int) -> list[DatamoduleConnection]:
        db = get_db()
        rows = db.execute(
            f"SELECT {DatamoduleConnection._COLUMNS} FROM datamodule_connection "
            "WHERE datamodule_id = ? ORDER BY local_name",
            (datamodule_id,),
        ).fetchall()
        return [DatamoduleConnection._from_row(row) for row in rows]

    @staticmethod
    def create(datamodule_id: int, connection_id: int, local_name: str) -> DatamoduleConnection:
        with transaction() as cursor:
            cursor.execute(
                "INSERT INTO datamodule_connection (datamodule_id, connection_id, local_name) "
                "VALUES (?, ?, ?)",
                (datamodule_id, connection_id, local_name.strip()),
            )
            row = cursor.execute("SELECT last_insert_rowid()").fetchone()
            link_id = int(row[0]) if row else 0
        created = DatamoduleConnection.get_by_id(link_id)
        assert created is not None
        return created

    def delete(self) -> bool:
        with transaction() as cursor:
            cursor.execute("DELETE FROM datamodule_connection WHERE id = ?", (self.id,))
            return cursor.execute("SELECT changes()").fetchone()[0] > 0  # type: ignore[index]
