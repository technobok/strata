"""Saved runs of reports and checks: "Run now", the run page and its downloads.

A run's rows are a Parquet snapshot (services/run_service), so the run page
shows exactly what that run found, with the same sort/search/filter/pager and
export links as a live report.
"""

from urllib.parse import urlencode
from uuid import UUID

from flask import Blueprint, Response, g, redirect, render_template, request, url_for
from werkzeug.exceptions import NotFound
from werkzeug.wrappers import Response as WerkzeugResponse

from strata.blueprints.auth import forbid, login_required, owner_required
from strata.blueprints.reports import build_ui
from strata.db import get_db
from strata.models.datamodule import Datamodule
from strata.models.report import Report
from strata.models.report_arg import ReportArg
from strata.models.report_check import ReportCheck
from strata.models.report_run import ReportRun
from strata.services import access, query_filter, report_service, run_service, stream_service
from strata.services.query_filter import FilterError

bp = Blueprint("runs", __name__)


def _load(run_uuid: UUID) -> tuple[ReportRun, Report, Datamodule]:
    run = ReportRun.get_by_uuid(str(run_uuid))
    report = Report.get_by_id(run.report_id) if run else None
    dm = Datamodule.get_by_id(report.datamodule_id) if report else None
    if run is None or report is None or dm is None:
        raise NotFound()
    if not access.can_view(dm):
        forbid("You are not one of this report's viewers; its owners can add you.")
    return run, report, dm


def _run_url(run: ReportRun, ext: str, pairs: list[tuple[str, str]]) -> str:
    if ext == "html":
        base = url_for("runs.view", run_uuid=run.uuid)
    else:
        base = url_for("runs.download", run_uuid=run.uuid, ext=ext)
    return f"{base}?{urlencode(pairs)}" if pairs else base


def _arg_pairs(args: dict[str, list[str]]) -> list[tuple[str, str]]:
    return [(name, v) for name, values in args.items() for v in values]


@bp.route("/reports/<report_uuid>/run", methods=["POST"])
@owner_required
def run_now(report_uuid: str) -> WerkzeugResponse:
    """Run the report now with the posted argument values (else the defaults) and
    open the saved run. Follows the serve path: it never builds in the web app."""
    resolved = report_service.resolve(report_uuid)
    if resolved is None:
        raise NotFound()
    report, dm = resolved
    names = {a.name for a in ReportArg.get_for_report(report.id)}
    arg_values: dict[str, list[str]] = {}
    for key, value in request.form.items(multi=True):
        if key in names and value != "":
            arg_values.setdefault(key, []).append(value)
    version, note = run_service.version_for_web(report, dm)
    run = run_service.run(
        report, dm, arg_values, version=version, data_note=note, triggered_by=g.user.username
    )
    return redirect(url_for("runs.view", run_uuid=run.uuid))


@bp.route("/runs/<uuid:run_uuid>")
@login_required
def view(run_uuid: UUID) -> str:
    run, report, dm = _load(run_uuid)
    pairs = list(request.args.items(multi=True))
    result = None
    arg_error = None
    spec = None
    try:
        # Every key is a filter/sort/search/page: a run's arguments are fixed.
        _args, spec = query_filter.parse_params(pairs, set())
        if run.snapshot_path:
            result = run_service.snapshot_table(run, dm.uuid, spec)
    except FilterError as exc:
        arg_error = str(exc)
    schedule_name = None
    if run.schedule_id is not None:
        row = (
            get_db()
            .execute("SELECT name FROM schedule WHERE id = ?", (run.schedule_id,))
            .fetchone()
        )
        schedule_name = str(row[0]) if row else None
    current = url_for("reports.serve", report_uuid=report.uuid, ext="html")
    if run.args:
        current += "?" + urlencode(_arg_pairs(run.args))
    context = dict(
        run=run,
        report=report,
        datamodule=dm,
        check=ReportCheck.get(report.id),
        schedule_name=schedule_name,
        current_url=current,
        result=result,
        arg_error=arg_error,
        ui=build_ui(lambda ext, p: _run_url(run, ext, p), pairs, spec, result),
    )
    if request.headers.get("HX-Request") and not request.headers.get("HX-History-Restore-Request"):
        return render_template("reports/_table.html", **context)
    return render_template("runs/view.html", **context)


@bp.route("/runs/<uuid:run_uuid>.<ext>")
@login_required
def download(run_uuid: UUID, ext: str) -> Response:
    run, report, dm = _load(run_uuid)
    if ext not in stream_service.STREAM_FORMATS:
        return Response(f"Unsupported format '{ext}'", status=400)
    try:
        _args, spec = query_filter.parse_params(list(request.args.items(multi=True)), set())
        return run_service.snapshot_download(run, dm.uuid, report, spec, ext)
    except FilterError as exc:
        return Response(str(exc), status=400)
    except FileNotFoundError:
        raise NotFound() from None
