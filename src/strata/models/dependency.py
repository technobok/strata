"""Dependency model — this datamodule ATTACHes another under a local_name.

At build time datamodule_service ensure_fresh-es the depended-on datamodule
(TTL pull) and ATTACHes its live version read-only as ``local_name``, so the
build script can query ``local_name.table``. The graph must stay acyclic;
``reaches`` supports add-time cycle rejection.
"""

from dataclasses import dataclass
from typing import Any

from strata.db import get_db, transaction


@dataclass
class Dependency:
    id: int
    datamodule_id: int
    depends_on_id: int
    local_name: str

    _COLUMNS = "id, datamodule_id, depends_on_id, local_name"

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> Dependency:
        return Dependency(
            id=int(row[0]),
            datamodule_id=int(row[1]),
            depends_on_id=int(row[2]),
            local_name=str(row[3]),
        )

    @staticmethod
    def get_by_id(dependency_id: int) -> Dependency | None:
        db = get_db()
        row = db.execute(
            f"SELECT {Dependency._COLUMNS} FROM dependency WHERE id = ?", (dependency_id,)
        ).fetchone()
        return Dependency._from_row(row) if row else None

    @staticmethod
    def get_for_datamodule(datamodule_id: int) -> list[Dependency]:
        db = get_db()
        rows = db.execute(
            f"SELECT {Dependency._COLUMNS} FROM dependency WHERE datamodule_id = ? "
            "ORDER BY local_name",
            (datamodule_id,),
        ).fetchall()
        return [Dependency._from_row(row) for row in rows]

    @staticmethod
    def create(datamodule_id: int, depends_on_id: int, local_name: str) -> Dependency:
        with transaction() as cursor:
            cursor.execute(
                "INSERT INTO dependency (datamodule_id, depends_on_id, local_name) "
                "VALUES (?, ?, ?)",
                (datamodule_id, depends_on_id, local_name.strip()),
            )
            row = cursor.execute("SELECT last_insert_rowid()").fetchone()
            dep_id = int(row[0]) if row else 0
        created = Dependency.get_by_id(dep_id)
        assert created is not None
        return created

    @staticmethod
    def reaches(from_id: int, target_id: int) -> bool:
        """True if ``from_id`` can reach ``target_id`` by following depends_on edges.

        Used before adding an edge A -> B: if B already reaches A, the edge would
        close a cycle and is rejected.
        """
        db = get_db()
        seen: set[int] = set()
        stack = [from_id]
        while stack:
            node = stack.pop()
            if node == target_id:
                return True
            if node in seen:
                continue
            seen.add(node)
            rows = db.execute(
                "SELECT depends_on_id FROM dependency WHERE datamodule_id = ?", (node,)
            ).fetchall()
            stack.extend(int(v) for (v,) in rows if v is not None)
        return False

    def delete(self) -> bool:
        with transaction() as cursor:
            cursor.execute("DELETE FROM dependency WHERE id = ?", (self.id,))
            return cursor.execute("SELECT changes()").fetchone()[0] > 0  # type: ignore[index]
