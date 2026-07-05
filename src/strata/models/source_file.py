"""Source file model — an uploaded build input for a datamodule.

Files live under the datamodule's ``sources/`` dir on disk (``stored_name``);
build scripts reference them by their original ``filename`` via the
``source('filename')`` helper (see datamodule_service). Filesystem writes/reads
are done by datamodule_service; this model owns the metadata rows.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from strata.db import get_db, transaction


@dataclass
class SourceFile:
    id: int
    datamodule_id: int
    filename: str
    stored_name: str
    byte_size: int | None
    content_type: str | None
    uploaded_by: str
    uploaded_at: str

    _COLUMNS = (
        "id, datamodule_id, filename, stored_name, byte_size, content_type, "
        "uploaded_by, uploaded_at"
    )

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> SourceFile:
        return SourceFile(
            id=int(row[0]),
            datamodule_id=int(row[1]),
            filename=str(row[2]),
            stored_name=str(row[3]),
            byte_size=int(row[4]) if row[4] is not None else None,
            content_type=str(row[5]) if row[5] is not None else None,
            uploaded_by=str(row[6]),
            uploaded_at=str(row[7]),
        )

    @staticmethod
    def get_by_id(source_id: int) -> SourceFile | None:
        db = get_db()
        row = db.execute(
            f"SELECT {SourceFile._COLUMNS} FROM source_file WHERE id = ?", (source_id,)
        ).fetchone()
        return SourceFile._from_row(row) if row else None

    @staticmethod
    def get_by_filename(datamodule_id: int, filename: str) -> SourceFile | None:
        db = get_db()
        row = db.execute(
            f"SELECT {SourceFile._COLUMNS} FROM source_file "
            "WHERE datamodule_id = ? AND filename = ?",
            (datamodule_id, filename),
        ).fetchone()
        return SourceFile._from_row(row) if row else None

    @staticmethod
    def get_for_datamodule(datamodule_id: int) -> list[SourceFile]:
        db = get_db()
        rows = db.execute(
            f"SELECT {SourceFile._COLUMNS} FROM source_file WHERE datamodule_id = ? "
            "ORDER BY filename",
            (datamodule_id,),
        ).fetchall()
        return [SourceFile._from_row(row) for row in rows]

    @staticmethod
    def create(
        datamodule_id: int,
        filename: str,
        stored_name: str,
        byte_size: int,
        content_type: str | None,
        uploaded_by: str,
    ) -> SourceFile:
        now = datetime.now(UTC).isoformat()
        with transaction() as cursor:
            cursor.execute(
                "INSERT INTO source_file (datamodule_id, filename, stored_name, byte_size, "
                "content_type, uploaded_by, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (datamodule_id, filename, stored_name, byte_size, content_type, uploaded_by, now),
            )
            row = cursor.execute("SELECT last_insert_rowid()").fetchone()
            source_id = int(row[0]) if row else 0
        created = SourceFile.get_by_id(source_id)
        assert created is not None
        return created

    def delete(self) -> bool:
        with transaction() as cursor:
            cursor.execute("DELETE FROM source_file WHERE id = ?", (self.id,))
            return cursor.execute("SELECT changes()").fetchone()[0] > 0  # type: ignore[index]
