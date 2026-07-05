"""Reports blueprint — the public serve path for running a report macro.

``GET /r/<report_uuid>.<ext>`` renders (html) or streams (parquet) a report over
its datamodule's live version, rebuilding first if the datamodule is TTL-stale.
Arguments come from the query string. Authoring lives in
blueprints/datamodules.py; sort/filter/pagination + more formats arrive in
Phase 4, API-key access in Phase 6.
"""

import duckdb
from flask import Blueprint, render_template, request
from flask import Response as FlaskResponse
from werkzeug.exceptions import NotFound

from strata.blueprints.auth import login_required
from strata.models.report_arg import ReportArg
from strata.services import report_service
from strata.services.report_service import ArgError

bp = Blueprint("reports", __name__)

_SUPPORTED = {"html", "parquet"}


@bp.route("/r/<report_uuid>.<ext>")
@login_required
def serve(report_uuid: str, ext: str) -> FlaskResponse | str:
    resolved = report_service.resolve(report_uuid)
    if resolved is None:
        raise NotFound()
    report, dm = resolved

    if ext not in _SUPPORTED:
        return FlaskResponse(f"Unsupported format '{ext}'", status=400)

    arg_values = {k: request.args.getlist(k) for k in request.args}

    if ext == "parquet":
        try:
            return report_service.stream(report, dm, arg_values, "parquet")
        except (ArgError, duckdb.Error) as exc:
            return FlaskResponse(str(exc), status=400)

    # html
    args = ReportArg.get_for_report(report.id)
    options = report_service.arg_options_map(report, dm, args)

    # Run once args are submitted, or when nothing is required — a report with
    # required args shows its (options-populated) form before first running.
    result = None
    arg_error = None
    if request.args or not any(a.required for a in args):
        try:
            result = report_service.run_table(report, dm, arg_values)
        except ArgError as exc:
            arg_error = str(exc)

    return render_template(
        "reports/view.html",
        report=report,
        datamodule=dm,
        args=args,
        arg_values=arg_values,
        options=options,
        query_string=request.query_string.decode(),
        result=result,
        arg_error=arg_error,
    )
