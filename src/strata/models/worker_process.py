"""Worker process heartbeat rows: which strata-worker processes are alive."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from strata.db import get_db, transaction

# A worker not seen for this long is treated as gone (it heartbeats every few seconds).
STALE_SECONDS = 120


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class WorkerProcess:
    id: str
    hostname: str
    pid: int
    queues: str
    concurrency: int
    started_at: str
    last_seen_at: str

    _COLUMNS = "id, hostname, pid, queues, concurrency, started_at, last_seen_at"

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> WorkerProcess:
        return WorkerProcess(
            id=str(row[0]),
            hostname=str(row[1]),
            pid=int(row[2]),
            queues=str(row[3]),
            concurrency=int(row[4]),
            started_at=str(row[5]),
            last_seen_at=str(row[6]),
        )

    @staticmethod
    def register(
        worker_id: str, hostname: str, pid: int, queues: list[str], concurrency: int
    ) -> None:
        now = _now().isoformat()
        with transaction() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO worker_process (id, hostname, pid, queues, concurrency, "
                "started_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (worker_id, hostname, pid, ",".join(queues), concurrency, now, now),
            )

    @staticmethod
    def heartbeat(worker_id: str) -> None:
        with transaction() as cur:
            cur.execute(
                "UPDATE worker_process SET last_seen_at = ? WHERE id = ?",
                (_now().isoformat(), worker_id),
            )

    @staticmethod
    def unregister(worker_id: str) -> None:
        with transaction() as cur:
            cur.execute("DELETE FROM worker_process WHERE id = ?", (worker_id,))

    @staticmethod
    def alive() -> list[WorkerProcess]:
        cutoff = (_now() - timedelta(seconds=STALE_SECONDS)).isoformat()
        rows = (
            get_db()
            .execute(
                f"SELECT {WorkerProcess._COLUMNS} FROM worker_process "
                "WHERE last_seen_at >= ? ORDER BY started_at",
                (cutoff,),
            )
            .fetchall()
        )
        return [WorkerProcess._from_row(r) for r in rows]

    @staticmethod
    def forget_stale(older_than_seconds: int = 86400) -> int:
        cutoff = (_now() - timedelta(seconds=older_than_seconds)).isoformat()
        with transaction() as cur:
            cur.execute("DELETE FROM worker_process WHERE last_seen_at < ?", (cutoff,))
            return int(cur.execute("SELECT changes()").fetchone()[0])  # type: ignore[index]
