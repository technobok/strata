"""Build job — a requested build of a datamodule, run by a worker.

Requests coalesce: at most one *queued* job exists per datamodule (a partial
unique index enforces it). A request while one is queued joins that job (a
forced or cascading request upgrades it). ``cascade`` = "build with all
dependencies": force-rebuild every dependency too. A non-forced request while a job is
running joins the running job; a forced one queues a single follow-up, so an
edit made during a build is still built.

Workers claim the oldest due job in their queues whose datamodule has no job
running, in one atomic UPDATE ... RETURNING.

A ``run`` job (kind = 'run') is a scheduled run of a report or check: it never
coalesces, but shares the one-running-job-per-datamodule rule with builds, so a
run that refreshes its datamodule in-thread never races a build of it.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from strata.db import get_db, transaction

ACTIVE = ("queued", "running")


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class BuildJob:
    id: int
    datamodule_id: int
    queue: str
    reason: str
    force: bool
    status: str
    requested_at: str
    run_after: str
    started_at: str | None
    finished_at: str | None
    worker_id: str | None
    version_id: int | None
    error: str | None
    cascade: bool = False
    kind: str = "build"  # build | run (a scheduled run of report_id)
    report_id: int | None = None
    schedule_id: int | None = None
    run_id: int | None = None  # the report_run a run job produced

    _COLUMNS = (
        "id, datamodule_id, queue, reason, force, status, requested_at, run_after, "
        "started_at, finished_at, worker_id, version_id, error, cascade, kind, report_id, "
        "schedule_id, run_id"
    )

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> BuildJob:
        return BuildJob(
            id=int(row[0]),
            datamodule_id=int(row[1]),
            queue=str(row[2]),
            reason=str(row[3]),
            force=bool(row[4]),
            status=str(row[5]),
            requested_at=str(row[6]),
            run_after=str(row[7]),
            started_at=str(row[8]) if row[8] is not None else None,
            finished_at=str(row[9]) if row[9] is not None else None,
            worker_id=str(row[10]) if row[10] is not None else None,
            version_id=int(row[11]) if row[11] is not None else None,
            error=str(row[12]) if row[12] is not None else None,
            cascade=bool(row[13]),
            kind=str(row[14]),
            report_id=int(row[15]) if row[15] is not None else None,
            schedule_id=int(row[16]) if row[16] is not None else None,
            run_id=int(row[17]) if row[17] is not None else None,
        )

    @property
    def active(self) -> bool:
        return self.status in ACTIVE

    @staticmethod
    def get_by_id(job_id: int) -> BuildJob | None:
        row = (
            get_db()
            .execute(f"SELECT {BuildJob._COLUMNS} FROM build_job WHERE id = ?", (job_id,))
            .fetchone()
        )
        return BuildJob._from_row(row) if row else None

    @staticmethod
    def recent_for_datamodule(datamodule_id: int, limit: int = 10) -> list[BuildJob]:
        rows = (
            get_db()
            .execute(
                f"SELECT {BuildJob._COLUMNS} FROM build_job WHERE datamodule_id = ? "
                "ORDER BY requested_at DESC, id DESC LIMIT ?",
                (datamodule_id, limit),
            )
            .fetchall()
        )
        return [BuildJob._from_row(r) for r in rows]

    @staticmethod
    def active_for_datamodule(datamodule_id: int) -> list[BuildJob]:
        rows = (
            get_db()
            .execute(
                f"SELECT {BuildJob._COLUMNS} FROM build_job "
                "WHERE datamodule_id = ? AND status IN ('queued', 'running') ORDER BY id",
                (datamodule_id,),
            )
            .fetchall()
        )
        return [BuildJob._from_row(r) for r in rows]

    @staticmethod
    def counts_by_status() -> dict[str, int]:
        rows = get_db().execute("SELECT status, COUNT(*) FROM build_job GROUP BY status")
        return {str(status): int(cast(int, count)) for status, count in rows}

    @staticmethod
    def enqueue(
        datamodule_id: int, queue: str, reason: str, force: bool, cascade: bool = False
    ) -> BuildJob:
        """Request a build, coalescing with an active job (see the module docstring)."""
        force = force or cascade
        with transaction() as cur:
            row = cur.execute(
                "SELECT id FROM build_job WHERE datamodule_id = ? AND status = 'queued' "
                "AND kind = 'build'",
                (datamodule_id,),
            ).fetchone()
            if row:
                if force:
                    cur.execute("UPDATE build_job SET force = 1 WHERE id = ?", (row[0],))
                if cascade:
                    cur.execute("UPDATE build_job SET cascade = 1 WHERE id = ?", (row[0],))
                job_id = int(row[0])
            else:
                running = None
                if not force:
                    running = cur.execute(
                        "SELECT id FROM build_job WHERE datamodule_id = ? AND status = 'running' "
                        "AND kind = 'build'",
                        (datamodule_id,),
                    ).fetchone()
                if running:
                    job_id = int(running[0])
                else:
                    now = _now()
                    cur.execute(
                        "INSERT INTO build_job (datamodule_id, queue, reason, force, cascade, "
                        "status, requested_at, run_after) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)",
                        (datamodule_id, queue, reason, int(force), int(cascade), now, now),
                    )
                    job_id = int(cur.execute("SELECT last_insert_rowid()").fetchone()[0])  # type: ignore[index]
        job = BuildJob.get_by_id(job_id)
        assert job is not None
        return job

    @staticmethod
    def enqueue_run(
        datamodule_id: int, queue: str, reason: str, report_id: int, schedule_id: int | None
    ) -> BuildJob:
        """Queue a run of ``report_id`` (never coalesced: each due time is its own run)."""
        now = _now()
        with transaction() as cur:
            cur.execute(
                "INSERT INTO build_job (datamodule_id, queue, reason, force, status, requested_at, "
                "run_after, kind, report_id, schedule_id) "
                "VALUES (?, ?, ?, 0, 'queued', ?, ?, 'run', ?, ?)",
                (datamodule_id, queue, reason, now, now, report_id, schedule_id),
            )
            job_id = int(cur.execute("SELECT last_insert_rowid()").fetchone()[0])  # type: ignore[index]
        job = BuildJob.get_by_id(job_id)
        assert job is not None
        return job

    @staticmethod
    def claim_next(queues: list[str], worker_id: str) -> BuildJob | None:
        """Atomically take the oldest due job in ``queues`` (one per datamodule)."""
        now = _now()
        marks = ", ".join("?" for _ in queues)
        with transaction() as cur:
            row = cur.execute(
                "UPDATE build_job SET status = 'running', started_at = ?, worker_id = ? "
                "WHERE id = (SELECT j.id FROM build_job j WHERE j.status = 'queued' "
                f"AND j.queue IN ({marks}) AND j.run_after <= ? "
                "AND NOT EXISTS (SELECT 1 FROM build_job r WHERE r.datamodule_id = j.datamodule_id "
                "AND r.status = 'running') ORDER BY j.requested_at, j.id LIMIT 1) RETURNING id",
                (now, worker_id, *queues, now),
            ).fetchone()
        return BuildJob.get_by_id(int(row[0])) if row else None

    def claim(self, worker_id: str) -> bool:
        """Take this specific queued job (inline mode); False if it is no longer queued."""
        with transaction() as cur:
            cur.execute(
                "UPDATE build_job SET status = 'running', started_at = ?, worker_id = ? "
                "WHERE id = ? AND status = 'queued' AND NOT EXISTS (SELECT 1 FROM build_job r "
                "WHERE r.datamodule_id = ? AND r.status = 'running')",
                (_now(), worker_id, self.id, self.datamodule_id),
            )
            return int(cur.execute("SELECT changes()").fetchone()[0]) > 0  # type: ignore[index]

    def finish(
        self,
        status: str,
        version_id: int | None = None,
        error: str | None = None,
        run_id: int | None = None,
    ) -> None:
        with transaction() as cur:
            cur.execute(
                "UPDATE build_job SET status = ?, finished_at = ?, version_id = ?, error = ?, "
                "run_id = ? WHERE id = ?",
                (status, _now(), version_id, error, run_id, self.id),
            )

    @staticmethod
    def reap_orphans(live_worker_ids: list[str], started_before: str) -> int:
        """Fail running jobs whose worker is gone and whose datamodule nobody is building.

        A job runs under its datamodule's build lease, so a valid lease means the
        build is still alive somewhere (a worker, or an inline web request).
        """
        # No live workers at all: every running job's worker is gone. (Never
        # "NOT IN (NULL)": that is never true, so nothing would be reaped.)
        alive_clause = (
            f"AND worker_id NOT IN ({', '.join('?' for _ in live_worker_ids)}) "
            if live_worker_ids
            else ""
        )
        with transaction() as cur:
            cur.execute(
                "UPDATE build_job SET status = 'failed', finished_at = ?, "
                "error = 'the process running this build stopped' "
                f"WHERE status = 'running' AND started_at < ? {alive_clause}"
                "AND NOT EXISTS (SELECT 1 FROM datamodule d WHERE d.id = build_job.datamodule_id "
                "AND d.build_token IS NOT NULL AND d.build_lease_until >= ?)",
                (_now(), started_before, *live_worker_ids, _now()),
            )
            return int(cur.execute("SELECT changes()").fetchone()[0])  # type: ignore[index]
