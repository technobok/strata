"""API blueprint — PowerQuery links and programmatic report access."""

import json
import logging
from typing import Any

from flask import Blueprint, abort, g, jsonify, request
from flask import Response as FlaskResponse

from strata.blueprints.auth import login_required
from strata.models.api_link import ApiLink
from strata.models.parameter import Parameter
from strata.models.report import Report
from strata.models.report_run import ReportRun
from strata.services import cache_service
from strata.services.export_service import generate_xlsx_from_cache
from strata.services.query_service import compute_result_hash, execute_report

log = logging.getLogger(__name__)

bp = Blueprint("api", __name__, url_prefix="/api/v1")


# --- PowerQuery link endpoint ---


@bp.route("/link/<link_uuid>")
def link_download(link_uuid: str) -> FlaskResponse:
    """Public endpoint: download report results as XLSX via API link.

    No login required — authentication is via the link UUID itself.
    Supports fixed parameters from the link and query-string overrides
    for parameterised params.
    """
    link = ApiLink.get_by_uuid(link_uuid)
    if not link:
        abort(404)

    if not link.is_valid():
        abort(403)

    report = Report.get_by_id(link.report_id)
    if not report:
        abort(404)

    params = Parameter.get_for_report(report.id)

    # Build parameter values: fixed from link, overrides from query string
    fixed = link.get_fixed_parameters()
    parameterised = link.get_parameterised_params()

    structural_params: dict[str, str] = {}
    value_params: dict[str, str] = {}
    param_types: dict[str, str] = {}

    for param in params:
        # Fixed parameters take priority
        if param.name in fixed:
            value = str(fixed[param.name])
        elif param.name in parameterised:
            value = request.args.get(param.name, "")
            if not value and param.default_value:
                value = param.default_value
        elif param.default_value:
            value = param.default_value
        else:
            value = ""

        if param.param_type == "structural":
            structural_params[param.name] = value
        else:
            value_params[param.name] = value
            param_types[param.name] = param.data_type

    result = execute_report(
        sql_template=report.sql_template,
        structural_params=structural_params,
        value_params=value_params,
        param_types=param_types,
    )

    if result.error:
        log.warning("API link %s execution error: %s", link_uuid, result.error)
        return FlaskResponse(
            json.dumps({"error": result.error}),
            status=400,
            mimetype="application/json",
        )

    # Cache result
    result_hash = compute_result_hash(report.id, result.rendered_sql, value_params)
    cache_service.write_result(result_hash, result.columns, result.types, result.rows)

    # Record the use
    link.record_use()

    # Generate and return XLSX
    from strata.services.export_service import generate_xlsx

    xlsx_bytes = generate_xlsx(result.columns, result.rows, report.name[:31])
    filename = f"{report.name.replace(' ', '_')}.xlsx"

    return FlaskResponse(
        xlsx_bytes,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.route("/link/<link_uuid>/json")
def link_json(link_uuid: str) -> FlaskResponse:
    """Public endpoint: return report results as JSON via API link."""
    link = ApiLink.get_by_uuid(link_uuid)
    if not link:
        abort(404)

    if not link.is_valid():
        abort(403)

    report = Report.get_by_id(link.report_id)
    if not report:
        abort(404)

    params = Parameter.get_for_report(report.id)

    fixed = link.get_fixed_parameters()
    parameterised = link.get_parameterised_params()

    structural_params: dict[str, str] = {}
    value_params: dict[str, str] = {}
    param_types: dict[str, str] = {}

    for param in params:
        if param.name in fixed:
            value = str(fixed[param.name])
        elif param.name in parameterised:
            value = request.args.get(param.name, "")
            if not value and param.default_value:
                value = param.default_value
        elif param.default_value:
            value = param.default_value
        else:
            value = ""

        if param.param_type == "structural":
            structural_params[param.name] = value
        else:
            value_params[param.name] = value
            param_types[param.name] = param.data_type

    result = execute_report(
        sql_template=report.sql_template,
        structural_params=structural_params,
        value_params=value_params,
        param_types=param_types,
    )

    if result.error:
        return FlaskResponse(
            json.dumps({"error": result.error}),
            status=400,
            mimetype="application/json",
        )

    link.record_use()

    rows_data = [dict(zip(result.columns, row, strict=False)) for row in result.rows]

    return jsonify(
        {
            "report": report.name,
            "row_count": result.row_count,
            "columns": result.columns,
            "rows": rows_data,
        }
    )


# --- API link management ---


@bp.route("/reports/<uuid>/links")
@login_required
def list_links(uuid: str) -> FlaskResponse:
    """List API links for a report."""
    report = Report.get_by_uuid(uuid)
    if not report:
        abort(404)

    links = ApiLink.get_for_report(report.id)
    return jsonify(
        [
            {
                "uuid": link.uuid,
                "name": link.name,
                "enabled": link.enabled,
                "expires_at": link.expires_at,
                "use_count": link.use_count,
                "last_used_at": link.last_used_at,
                "created_at": link.created_at,
            }
            for link in links
        ]
    )


@bp.route("/reports/<uuid>/links", methods=["POST"])
@login_required
def create_link(uuid: str) -> FlaskResponse:
    """Create a new API link for a report."""
    report = Report.get_by_uuid(uuid)
    if not report:
        abort(404)

    data = request.get_json() or {}
    name = data.get("name", "").strip()
    if not name:
        return FlaskResponse(
            json.dumps({"error": "Name is required"}),
            status=400,
            mimetype="application/json",
        )

    link = ApiLink.create(
        report_id=report.id,
        name=name,
        created_by=g.user.username,
        fixed_parameters=data.get("fixed_parameters"),
        parameterised_params=data.get("parameterised_params"),
        expires_at=data.get("expires_at"),
    )

    return jsonify(
        {
            "uuid": link.uuid,
            "name": link.name,
            "created_at": link.created_at,
        }
    )


@bp.route("/links/<link_uuid>/rotate", methods=["POST"])
@login_required
def rotate_link(link_uuid: str) -> FlaskResponse:
    """Rotate a link's UUID."""
    link = ApiLink.get_by_uuid(link_uuid)
    if not link:
        abort(404)

    new_uuid = link.rotate_uuid()
    return jsonify({"uuid": new_uuid})


@bp.route("/links/<link_uuid>", methods=["DELETE"])
@login_required
def delete_link(link_uuid: str) -> FlaskResponse:
    """Delete an API link."""
    link = ApiLink.get_by_uuid(link_uuid)
    if not link:
        abort(404)

    link.delete()
    return jsonify({"deleted": True})


# --- Programmatic report execution ---


@bp.route("/reports/<uuid>/run", methods=["POST"])
@login_required
def run_report(uuid: str) -> FlaskResponse:
    """Programmatic report execution — returns JSON results."""
    report = Report.get_by_uuid(uuid)
    if not report:
        abort(404)

    data: dict[str, Any] = request.get_json() or {}
    supplied_params: dict[str, str] = data.get("parameters", {})

    params = Parameter.get_for_report(report.id)

    structural_params: dict[str, str] = {}
    value_params: dict[str, str] = {}
    param_types: dict[str, str] = {}

    for param in params:
        value = supplied_params.get(param.name, "")
        if not value and param.default_value:
            value = param.default_value

        if param.param_type == "structural":
            structural_params[param.name] = value
        else:
            value_params[param.name] = value
            param_types[param.name] = param.data_type

    run_record = ReportRun.create_running(
        report_id=report.id,
        run_by=g.user.username,
        parameters=supplied_params,
    )

    result = execute_report(
        sql_template=report.sql_template,
        structural_params=structural_params,
        value_params=value_params,
        param_types=param_types,
    )

    if result.error:
        run_record.mark_failed(result.error, result.duration_ms)
        return FlaskResponse(
            json.dumps({"error": result.error, "run_uuid": run_record.uuid}),
            status=400,
            mimetype="application/json",
        )

    result_hash = compute_result_hash(report.id, result.rendered_sql, value_params)
    cache_service.write_result(result_hash, result.columns, result.types, result.rows)

    column_info = [
        {"name": col, "type": typ} for col, typ in zip(result.columns, result.types, strict=False)
    ]
    run_record.mark_completed(
        row_count=result.row_count,
        column_info=column_info,
        result_hash=result_hash,
        duration_ms=result.duration_ms,
    )

    rows_data = [dict(zip(result.columns, row, strict=False)) for row in result.rows]

    return jsonify(
        {
            "run_uuid": run_record.uuid,
            "report": report.name,
            "row_count": result.row_count,
            "duration_ms": result.duration_ms,
            "columns": result.columns,
            "rows": rows_data,
        }
    )


@bp.route("/runs/<run_uuid>")
@login_required
def get_run(run_uuid: str) -> FlaskResponse:
    """Get a run's results from cache."""
    run_record = ReportRun.get_by_uuid(run_uuid)
    if not run_record:
        abort(404)

    if run_record.status != "completed" or not run_record.result_hash:
        return jsonify(
            {
                "run_uuid": run_record.uuid,
                "status": run_record.status,
                "error": run_record.error_message,
            }
        )

    columns, rows, total = cache_service.read_result(run_record.result_hash)
    rows_data = [dict(zip(columns, row, strict=False)) for row in rows]

    return jsonify(
        {
            "run_uuid": run_record.uuid,
            "status": run_record.status,
            "row_count": total,
            "columns": columns,
            "rows": rows_data,
        }
    )


@bp.route("/runs/<run_uuid>/download")
@login_required
def download_run(run_uuid: str) -> FlaskResponse:
    """Download run results as XLSX."""
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
