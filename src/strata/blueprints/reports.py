"""Reports blueprint — CRUD, run, results, run history, and export."""

from flask import (
    Blueprint,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)
from flask import (
    Response as FlaskResponse,
)
from werkzeug.wrappers import Response

from strata.blueprints.auth import login_required
from strata.models.parameter import Parameter
from strata.models.report import Report
from strata.models.report_run import ReportRun
from strata.services import cache_service, template_service
from strata.services.query_service import QueryResult, compute_result_hash, execute_report

bp = Blueprint("reports", __name__, url_prefix="/reports")


@bp.route("/")
@login_required
def index() -> str:
    """List all reports."""
    reports = Report.get_all()
    return render_template("reports/index.html", reports=reports)


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new() -> str | Response:
    """Create a new report."""
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        sql_template = request.form.get("sql_template", "")

        if not name:
            flash("Report name is required.", "error")
            return render_template(
                "reports/edit.html",
                report=None,
                params=[],
                name=name,
                description=description,
                sql_template=sql_template,
            )

        report = Report.create(
            name=name,
            sql_template=sql_template,
            created_by=g.user.username,
            description=description,
        )

        extracted = template_service.extract_parameters(sql_template)
        if extracted:
            Parameter.sync_parameters(report.id, extracted)

        flash("Report created.", "success")
        return redirect(url_for("reports.edit", uuid=report.uuid))

    return render_template(
        "reports/edit.html",
        report=None,
        params=[],
        name="",
        description="",
        sql_template="",
    )


@bp.route("/<uuid>/edit", methods=["GET", "POST"])
@login_required
def edit(uuid: str) -> str | Response:
    """Edit an existing report."""
    report = Report.get_by_uuid(uuid)
    if not report:
        abort(404)

    params = Parameter.get_for_report(report.id)

    if request.method == "POST":
        action = request.form.get("action", "save")

        if action == "save":
            name = request.form.get("name", "").strip()
            description = request.form.get("description", "").strip()
            sql_template = request.form.get("sql_template", "")

            if not name:
                flash("Report name is required.", "error")
                return render_template(
                    "reports/edit.html",
                    report=report,
                    params=params,
                    name=name,
                    description=description,
                    sql_template=sql_template,
                )

            report.update(
                modified_by=g.user.username,
                name=name,
                description=description,
                sql_template=sql_template,
            )

            extracted = template_service.extract_parameters(sql_template)
            added, removed = Parameter.sync_parameters(report.id, extracted)
            params = Parameter.get_for_report(report.id)

            messages = []
            if added:
                messages.append(f"Added parameters: {', '.join(added)}")
            if removed:
                messages.append(f"Removed from template (not deleted): {', '.join(removed)}")
            if messages:
                flash(" | ".join(messages), "info")

            flash("Report saved.", "success")
            return redirect(url_for("reports.edit", uuid=report.uuid))

        elif action == "update_param":
            param_id = request.form.get("param_id", type=int)
            if param_id:
                param = Parameter.get_by_id(param_id)
                if param and param.report_id == report.id:
                    param.update(
                        data_type=request.form.get("data_type", "string"),
                        default_value=request.form.get("default_value") or None,
                        description=request.form.get("param_description", ""),
                        required=request.form.get("required") == "on",
                    )
                    flash(f"Parameter '{param.name}' updated.", "success")
            return redirect(url_for("reports.edit", uuid=report.uuid))

        elif action == "delete_param":
            param_id = request.form.get("param_id", type=int)
            if param_id:
                param = Parameter.get_by_id(param_id)
                if param and param.report_id == report.id:
                    param.delete()
                    flash(f"Parameter '{param.name}' deleted.", "success")
            return redirect(url_for("reports.edit", uuid=report.uuid))

    return render_template(
        "reports/edit.html",
        report=report,
        params=params,
        name=report.name,
        description=report.description,
        sql_template=report.sql_template,
    )


@bp.route("/<uuid>/delete", methods=["POST"])
@login_required
def delete(uuid: str) -> Response:
    """Delete a report."""
    report = Report.get_by_uuid(uuid)
    if not report:
        abort(404)

    report.delete()
    flash(f"Report '{report.name}' deleted.", "success")
    return redirect(url_for("reports.index"))


@bp.route("/<uuid>/run", methods=["GET", "POST"])
@login_required
def run(uuid: str) -> str | Response:
    """Run a report with parameters."""
    report = Report.get_by_uuid(uuid)
    if not report:
        abort(404)

    params = Parameter.get_for_report(report.id)
    result: QueryResult | None = None
    run_record: ReportRun | None = None

    if request.method == "POST":
        structural_params: dict[str, str] = {}
        value_params: dict[str, str] = {}
        param_types: dict[str, str] = {}

        for param in params:
            form_value = request.form.get(f"param_{param.name}", "")
            if not form_value and param.default_value:
                form_value = param.default_value
            if not form_value and param.required:
                flash(f"Parameter '{param.name}' is required.", "error")
                return render_template(
                    "reports/run.html",
                    report=report,
                    params=params,
                    result=None,
                    run_record=None,
                    param_values=dict(request.form),
                )

            if param.param_type == "structural":
                structural_params[param.name] = form_value
            else:
                value_params[param.name] = form_value
                param_types[param.name] = param.data_type

        all_params = {**structural_params, **value_params}
        run_record = ReportRun.create_running(
            report_id=report.id,
            run_by=g.user.username,
            parameters=all_params,
        )

        result = execute_report(
            sql_template=report.sql_template,
            structural_params=structural_params,
            value_params=value_params,
            param_types=param_types,
        )

        if result.error:
            run_record.mark_failed(result.error, result.duration_ms)
            flash(result.error, "error")
        else:
            # Cache result as Parquet
            result_hash = compute_result_hash(report.id, result.rendered_sql, value_params)
            cache_service.write_result(result_hash, result.columns, result.types, result.rows)
            column_info = [
                {"name": col, "type": typ}
                for col, typ in zip(result.columns, result.types, strict=False)
            ]
            run_record.mark_completed(
                row_count=result.row_count,
                column_info=column_info,
                result_hash=result_hash,
                duration_ms=result.duration_ms,
            )

    return render_template(
        "reports/run.html",
        report=report,
        params=params,
        result=result,
        run_record=run_record,
        param_values=dict(request.form) if request.method == "POST" else {},
    )


@bp.route("/<uuid>/runs")
@login_required
def runs(uuid: str) -> str:
    """List run history for a report."""
    report = Report.get_by_uuid(uuid)
    if not report:
        abort(404)

    run_list = ReportRun.get_for_report(report.id)
    return render_template("runs/index.html", report=report, runs=run_list)


@bp.route("/runs/<run_uuid>")
@login_required
def view_run(run_uuid: str) -> str:
    """View a past run's results from cache."""
    run_record = ReportRun.get_by_uuid(run_uuid)
    if not run_record:
        abort(404)

    report = Report.get_by_id(run_record.report_id)
    if not report:
        abort(404)

    columns: list[str] = []
    rows: list[tuple] = []

    if run_record.result_hash and cache_service.result_exists(run_record.result_hash):
        sort_col = request.args.get("sort_col", "")
        sort_dir = request.args.get("sort_dir", "asc")
        columns, rows, _ = cache_service.read_result(
            run_record.result_hash, sort_col=sort_col or None, sort_dir=sort_dir
        )
    else:
        sort_col = ""
        sort_dir = "asc"

    return render_template(
        "runs/view.html",
        report=report,
        run=run_record,
        columns=columns,
        rows=rows,
        sort_col=sort_col,
        sort_dir=sort_dir,
    )


@bp.route("/runs/<run_uuid>/download")
@login_required
def download_run(run_uuid: str) -> FlaskResponse:
    """Download run results as XLSX."""
    from strata.services.export_service import generate_xlsx_from_cache

    run_record = ReportRun.get_by_uuid(run_uuid)
    if not run_record or not run_record.result_hash:
        abort(404)

    report = Report.get_by_id(run_record.report_id)
    sheet_name = report.name[:31] if report else "Results"

    xlsx_bytes = generate_xlsx_from_cache(run_record.result_hash, sheet_name)
    if not xlsx_bytes:
        abort(404)

    filename = f"{sheet_name.replace(' ', '_')}_{run_record.uuid[:8]}.xlsx"

    return FlaskResponse(
        xlsx_bytes,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.route("/<uuid>/run/sort", methods=["POST"])
@login_required
def run_sort(uuid: str) -> str:
    """HTMX endpoint: sort/filter cached results."""
    report = Report.get_by_uuid(uuid)
    if not report:
        abort(404)

    result_hash = request.form.get("result_hash", "")
    sort_col = request.form.get("sort_col", "")
    sort_dir = request.form.get("sort_dir", "asc")
    filter_text = request.form.get("filter_text", "").strip()

    if result_hash and cache_service.result_exists(result_hash):
        columns, rows, total = cache_service.read_result(
            result_hash,
            sort_col=sort_col or None,
            sort_dir=sort_dir,
            filter_text=filter_text or None,
        )
        result = QueryResult(
            columns=columns,
            rows=rows,
            row_count=total,
        )
    else:
        result = QueryResult()

    return render_template(
        "reports/_results.html",
        result=result,
        sort_col=sort_col,
        sort_dir=sort_dir,
        report=report,
        result_hash=result_hash,
    )
