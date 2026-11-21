"""The exceptions overview: every check, its last run, and whether it is flagged.

A check is **flagged** when its latest run found rows. The overview also shows
when it runs next (its enabled schedules) and how healthy its datamodule's
data is (fresh, stale, failing, never built), because a check is only as
current as the data it reads.
"""

from dataclasses import dataclass

from strata.db import get_db
from strata.models.datamodule import Datamodule
from strata.models.report import Report
from strata.models.report_check import SEVERITIES, ReportCheck
from strata.models.report_run import ReportRun
from strata.services import datamodule_service

_SEVERITY_ORDER = {s: i for i, s in enumerate(reversed(SEVERITIES))}  # error first


@dataclass
class CheckRow:
    check: ReportCheck
    report: Report
    datamodule: Datamodule
    last_run: ReportRun | None
    next_run_at: str | None
    health: str  # fresh | stale | failing | never built
    health_note: str | None  # the last build error, for failing

    @property
    def flagged(self) -> bool:
        return self.last_run is not None and bool(self.last_run.row_count)


def _health(dm: Datamodule) -> tuple[str, str | None]:
    if dm.build_status == "failed" or dm.consecutive_failures:
        return "failing", dm.last_build_error
    live = datamodule_service.live_version(dm)
    if live is None:
        return "never built", None
    if datamodule_service.is_stale(dm, live, dm.ttl_seconds):
        return "stale", None
    return "fresh", None


def overview(severity: str | None = None, flagged_only: bool = False) -> list[CheckRow]:
    """Every check (optionally one severity / only flagged), flagged first, then by
    severity (error first) and name."""
    db = get_db()
    checks = [
        ReportCheck._from_row(r)
        for r in db.execute("SELECT report_id, severity, gate FROM report_check").fetchall()
    ]
    latest = {
        run.report_id: run
        for run in (
            ReportRun.get_by_id(int(rid))
            for (rid,) in db.execute("SELECT max(id) FROM report_run GROUP BY report_id")
        )
        if run is not None
    }
    next_runs = {
        int(rid): str(nxt)
        for rid, nxt in db.execute(
            "SELECT report_id, min(next_run_at) FROM schedule WHERE action = 'email' "
            "AND enabled = 1 AND next_run_at IS NOT NULL GROUP BY report_id"
        )
    }
    datamodules: dict[int, Datamodule | None] = {}
    rows: list[CheckRow] = []
    for check in checks:
        if severity and check.severity != severity:
            continue
        report = Report.get_by_id(check.report_id)
        if report is None:
            continue
        if report.datamodule_id not in datamodules:
            datamodules[report.datamodule_id] = Datamodule.get_by_id(report.datamodule_id)
        dm = datamodules[report.datamodule_id]
        if dm is None:
            continue
        health, note = _health(dm)
        row = CheckRow(
            check, report, dm, latest.get(report.id), next_runs.get(report.id), health, note
        )
        if flagged_only and not row.flagged:
            continue
        rows.append(row)
    rows.sort(
        key=lambda r: (not r.flagged, _SEVERITY_ORDER[r.check.severity], r.report.name.lower())
    )
    return rows
