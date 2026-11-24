"""The read audit's rows (migration 0013): who read what, when, and what came back.

Rows are written by ``services/read_audit`` and never updated. They carry the
names of what they read beside its ids (no foreign keys), so a deleted key,
report or datamodule keeps its history.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from strata.db import get_db, transaction

PAGE_SIZE = 100


@dataclass
class ReadLog:
    id: int
    at: str
    kind: str
    username: str | None
    api_key_id: int | None
    key_name: str | None
    token_prefix: str | None
    datamodule_id: int | None
    datamodule_path: str | None
    report_id: int | None
    report_name: str | None
    run_uuid: str | None
    version_uuid: str | None
    format: str | None
    query: str | None
    status: int | None
    row_count: int | None
    bytes_sent: int | None
    duration_ms: int | None
    remote_addr: str | None
    user_agent: str | None
    detail: str | None = None  # a refusal's own text (migration 0014)

    FIELDS = (
        "id",
        "at",
        "kind",
        "username",
        "api_key_id",
        "key_name",
        "token_prefix",
        "datamodule_id",
        "datamodule_path",
        "report_id",
        "report_name",
        "run_uuid",
        "version_uuid",
        "format",
        "query",
        "status",
        "row_count",
        "bytes_sent",
        "duration_ms",
        "remote_addr",
        "user_agent",
        "detail",
    )

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> ReadLog:
        return ReadLog(*row)

    @property
    def refused(self) -> bool:
        return self.status is not None and self.status >= 400

    @staticmethod
    def record(**values: Any) -> None:
        names = [n for n in ReadLog.FIELDS if n != "id" and n in values]
        with transaction() as cursor:
            cursor.execute(
                f"INSERT INTO read_log ({', '.join(names)}) "
                f"VALUES ({', '.join('?' for _ in names)})",
                tuple(values[n] for n in names),
            )

    @staticmethod
    def search(filters: ReadFilter, before_id: int | None = None) -> tuple[list[ReadLog], bool]:
        """One page, newest first, older than ``before_id``; and whether more follow."""
        where, params = filters.where()
        if before_id is not None:
            where.append("id < ?")
            params.append(before_id)
        sql = f"SELECT {', '.join(ReadLog.FIELDS)} FROM read_log"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        rows = get_db().execute(sql, (*params, PAGE_SIZE + 1)).fetchall()
        return [ReadLog._from_row(r) for r in rows[:PAGE_SIZE]], len(rows) > PAGE_SIZE

    @staticmethod
    def iterate(filters: ReadFilter) -> Iterator[ReadLog]:
        """Every matching row, newest first (the CSV download)."""
        where, params = filters.where()
        sql = f"SELECT {', '.join(ReadLog.FIELDS)} FROM read_log"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC"
        for row in get_db().execute(sql, tuple(params)):
            yield ReadLog._from_row(row)

    @staticmethod
    def datamodules() -> list[tuple[int, str]]:
        """Every datamodule the log names, by its latest recorded path."""
        rows = (
            get_db()
            .execute(
                "SELECT datamodule_id, datamodule_path FROM read_log WHERE id IN ("
                "SELECT MAX(id) FROM read_log WHERE datamodule_id IS NOT NULL "
                "GROUP BY datamodule_id) ORDER BY datamodule_path"
            )
            .fetchall()
        )
        return [_id_and_name(row) for row in rows]

    @staticmethod
    def reports(datamodule_id: int) -> list[tuple[int, str]]:
        rows = (
            get_db()
            .execute(
                "SELECT report_id, report_name FROM read_log WHERE id IN ("
                "SELECT MAX(id) FROM read_log WHERE datamodule_id = ? AND report_id IS NOT NULL "
                "GROUP BY report_id) ORDER BY report_name",
                (datamodule_id,),
            )
            .fetchall()
        )
        return [_id_and_name(row) for row in rows]

    @staticmethod
    def older_than(cutoff: str) -> int:
        row = get_db().execute("SELECT COUNT(*) FROM read_log WHERE at < ?", (cutoff,)).fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def delete_older_than(cutoff: str) -> int:
        with transaction() as cursor:
            cursor.execute("DELETE FROM read_log WHERE at < ?", (cutoff,))
            row = cursor.execute("SELECT changes()").fetchone()
        return int(row[0]) if row else 0


def _id_and_name(row: tuple[Any, ...]) -> tuple[int, str]:
    return int(row[0]), str(row[1] or "")


@dataclass
class ReadFilter:
    """What the reads page shows; every field is optional."""

    datamodule_id: int | None = None
    report_id: int | None = None
    username: str | None = None
    api_key_id: int | None = None
    kind: str | None = None
    refused: bool = False
    since: date | None = None
    until: date | None = None  # inclusive

    def where(self) -> tuple[list[str], list[Any]]:
        where: list[str] = []
        params: list[Any] = []
        if self.datamodule_id is not None:
            where.append("datamodule_id = ?")
            params.append(self.datamodule_id)
        if self.report_id is not None:
            where.append("report_id = ?")
            params.append(self.report_id)
        if self.username:
            where.append("username = ? COLLATE NOCASE")
            params.append(self.username)
        if self.api_key_id is not None:
            where.append("api_key_id = ?")
            params.append(self.api_key_id)
        if self.kind:
            where.append("kind = ?")
            params.append(self.kind)
        if self.refused:
            where.append("status >= 400")
        if self.since is not None:
            where.append("at >= ?")
            params.append(self.since.isoformat())
        if self.until is not None:
            where.append("at < ?")
            params.append((self.until + timedelta(days=1)).isoformat())
        return where, params
