"""Report and check runs: run a report once, save its rows, record the run.

A run executes the report on one version of its datamodule with the given
argument values and saves every row as a Parquet snapshot
(``DATA_ROOT/<fan>/<dm>/runs/<run uuid>.parquet``), so the run page, its
downloads and (from C2) the notification mail all show exactly what that run
found, however the data changes later.

Which version a run uses depends on who runs it:

- ``version_for_web``: "Run now" in the web app follows the serve path (it never
  builds): a stale datamodule gets a rebuild request and a bounded wait, then
  the last good version.
- ``version_inline``: the worker refreshes a stale datamodule itself, under the
  build lease in the calling thread (a nested job could deadlock a small pool).
  If the refresh fails, the run uses the last good version.

Either way, a run on data that may be out of date says so in ``data_note``.
"""

import logging
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb

from strata.config import live_int
from strata.models.build_job import BuildJob
from strata.models.datamodule import Datamodule
from strata.models.datamodule_version import DatamoduleVersion
from strata.models.report import Report
from strata.models.report_run import ReportRun
from strata.services import (
    build_queue,
    datamodule_service,
    duck,
    query_filter,
    report_service,
)
from strata.services.report_service import ArgError, TableResult

log = logging.getLogger(__name__)

ORPHAN_MIN_AGE_SECONDS = 3600  # a snapshot file younger than this may be mid-run


def snapshot_file(dm_uuid: str, run: ReportRun) -> Path | None:
    if not run.snapshot_path:
        return None
    return datamodule_service.dm_dir(dm_uuid) / run.snapshot_path


# ---------------------------------------------------------------------------
# Which version a run uses
# ---------------------------------------------------------------------------


def version_for_web(report: Report, dm: Datamodule) -> tuple[DatamoduleVersion | None, str | None]:
    """The serve path's choice (never builds here): fresh within the wait, else the
    last good version with a note."""
    served = build_queue.servable_version(dm, report_service.effective_ttl(report, dm))
    if not served.stale:
        return served.version, None
    if served.failure:
        return served.version, f"refresh failed: {served.failure}"
    return served.version, "the data is past its TTL and its rebuild is still running"


def version_inline(report: Report, dm: Datamodule) -> tuple[DatamoduleVersion | None, str | None]:
    """Refresh a stale datamodule in this thread (worker runs); on failure fall back
    to the last good version with a note."""
    try:
        version = datamodule_service.ensure_fresh(
            dm,
            f"run:{report.macro_name}",
            report_service.effective_ttl(report, dm),
            run=datamodule_service.BuildRun(auto=True),
        )
        return version, None
    except Exception as exc:  # noqa: BLE001 -- a failed refresh never stops the run
        log.warning("run of report %s: refresh of %s failed: %s", report.uuid, dm.path, exc)
        current = Datamodule.get_by_id(dm.id) or dm
        return datamodule_service.live_version(current), f"refresh failed: {exc}"


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def run(
    report: Report,
    dm: Datamodule,
    arg_values: dict[str, list[str]],
    *,
    version: DatamoduleVersion | None,
    data_note: str | None,
    triggered_by: str,
    schedule_id: int | None = None,
) -> ReportRun:
    """Run ``report`` on ``version`` and record the run. Never raises for a query
    problem: a bad argument or a failing query is a run with status 'error'."""
    started_at = datetime.now(UTC).isoformat()
    run_uuid = ReportRun.new_uuid()
    status, row_count, error, snapshot_path = "error", None, None, None
    if version is None:
        error = "the datamodule has never been built"
    else:
        relative = f"runs/{run_uuid}.parquet"
        try:
            row_count = report_service.snapshot(
                report, dm, arg_values, version, datamodule_service.dm_dir(dm.uuid) / relative
            )
            status = "ok" if row_count else "empty"
            snapshot_path = relative
        except (ArgError, duck.SqlShapeError, duckdb.Error) as exc:
            error = str(exc)
    return ReportRun.create(
        run_uuid=run_uuid,
        report_id=report.id,
        schedule_id=schedule_id,
        version_id=version.id if version is not None else None,
        args=arg_values,
        status=status,
        row_count=row_count,
        data_as_of=version.build_ended_at if version is not None else None,
        data_note=data_note,
        error=error,
        triggered_by=triggered_by,
        started_at=started_at,
        finished_at=datetime.now(UTC).isoformat(),
        snapshot_path=snapshot_path,
    )


def run_job(job: BuildJob) -> ReportRun | None:
    """A claimed 'run' job (a due report schedule): refresh in this thread, run with
    the schedule's arguments, mail it, and record the outcome on the job."""
    from strata.models.schedule import Schedule
    from strata.services import notify_service

    report = Report.get_by_id(job.report_id) if job.report_id is not None else None
    dm = Datamodule.get_by_id(job.datamodule_id)
    if report is None or dm is None:
        job.finish("failed", error="the report or its datamodule was deleted")
        return None
    schedule = Schedule.get_by_id(job.schedule_id) if job.schedule_id is not None else None
    version, note = version_inline(report, dm)
    result = run(
        report,
        dm,
        schedule.parameters if schedule else {},
        version=version,
        data_note=note,
        triggered_by=job.reason,
        schedule_id=job.schedule_id,
    )
    notify_service.notify_run(result, report, dm, schedule)
    job.finish(
        "failed" if result.status == "error" else "done",
        version_id=result.version_id,
        error=result.error,
        run_id=result.id,
    )
    return result


# ---------------------------------------------------------------------------
# Reading a run's rows
# ---------------------------------------------------------------------------


def _snapshot_query(
    run: ReportRun, dm_uuid: str
) -> tuple[duckdb.DuckDBPyConnection, str, list[tuple[str, str]]]:
    path = snapshot_file(dm_uuid, run)
    if path is None or not path.exists():
        raise FileNotFoundError("this run's saved rows are gone")
    conn = duck.snapshot_conn(path)
    try:
        described = conn.execute("DESCRIBE SELECT * FROM snapshot").fetchall()
    except Exception:
        conn.close()
        raise
    return conn, "SELECT * FROM snapshot", [(str(r[0]), str(r[1])) for r in described]


def snapshot_table(run: ReportRun, dm_uuid: str, spec: query_filter.QuerySpec) -> TableResult:
    """One page of the run's saved rows (filters, search and sort as on a report)."""
    try:
        conn, final_sql, columns = _snapshot_query(run, dm_uuid)
    except (FileNotFoundError, duckdb.Error) as exc:
        return TableResult(error=str(exc))
    return report_service.page_of(conn, final_sql, [], columns, spec)


def snapshot_download(
    run: ReportRun, dm_uuid: str, report: Report, spec: query_filter.QuerySpec, fmt: str
):
    """Stream the run's saved rows (spec applied) as ``fmt``."""
    conn, final_sql, columns = _snapshot_query(run, dm_uuid)
    try:
        return report_service.stream_rows(
            conn, final_sql, [], columns, spec, fmt, f"{report.macro_name}-{run.uuid[:8]}"
        )
    except Exception:
        conn.close()
        raise


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def prune_runs(dry_run: bool = False, now: datetime | None = None) -> list[str]:
    """Delete runs older than ``runs.retention_days`` with their snapshot files.
    Returns one line per run removed (or that would be)."""
    days = live_int("runs.retention_days")
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=days)).isoformat()
    expired = ReportRun.started_before(cutoff)
    lines = [f"{run.uuid[:8]} ({run.started_at[:10]})" for run, _ in expired]
    if dry_run:
        return lines
    for run, dm_uuid in expired:
        path = snapshot_file(dm_uuid, run)
        if path is not None:
            path.unlink(missing_ok=True)
    ReportRun.delete_ids([run.id for run, _ in expired])
    return lines


def delete_files_for_report(report: Report, dm_uuid: str) -> None:
    """Remove a report's snapshot files (its run rows go with the report row)."""
    from strata.db import get_db

    for (relative,) in (
        get_db()
        .execute(
            "SELECT snapshot_path FROM report_run WHERE report_id = ? AND snapshot_path IS NOT NULL",
            (report.id,),
        )
        .fetchall()
    ):
        (datamodule_service.dm_dir(dm_uuid) / str(relative)).unlink(missing_ok=True)


def orphan_snapshot_files(dm: Datamodule, dry_run: bool) -> list[str]:
    """Snapshot files with no run row (reconcile's sweep)."""
    runs_dir = datamodule_service.dm_dir(dm.uuid) / "runs"
    if not runs_dir.is_dir():
        return []
    known = {r.snapshot_path for r in ReportRun.for_datamodule(dm.id) if r.snapshot_path}
    swept: list[str] = []
    now = time.time()
    for f in sorted(runs_dir.iterdir()):
        if f"runs/{f.name}" in known:
            continue
        try:
            # A run renames its file into place just before recording its row.
            if now - f.stat().st_mtime < ORPHAN_MIN_AGE_SECONDS:
                continue
        except OSError:
            continue
        swept.append(str(f))
        if not dry_run:
            if f.is_dir():
                shutil.rmtree(f, ignore_errors=True)
            else:
                f.unlink(missing_ok=True)
    return swept
