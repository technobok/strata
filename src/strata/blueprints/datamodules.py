"""Datamodules blueprint — author datamodules, build them, define query reports.

Authoring is admin-gated in Phase 1 (the author/runner split lands in Phase 9).
The public serve path for running a report lives in blueprints/reports.py.
"""

from flask import (
    Blueprint,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from werkzeug.exceptions import NotFound
from werkzeug.wrappers import Response

from strata.blueprints.auth import admin_required
from strata.models.connection import Connection
from strata.models.datamodule import Datamodule
from strata.models.datamodule_connection import DatamoduleConnection
from strata.models.datamodule_version import DatamoduleVersion
from strata.models.dependency import Dependency
from strata.models.report import Report
from strata.models.report_arg import DATA_TYPES, WIDGETS, ReportArg
from strata.models.source_file import SourceFile
from strata.services import datamodule_service
from strata.services.query_filter import RESERVED_KEYS
from strata.services.template_service import ALIAS_RE

bp = Blueprint("datamodules", __name__, url_prefix="/datamodules")


def _parse_ttl(raw: str) -> int | None:
    raw = raw.strip()
    if raw == "":
        return None
    return int(raw)


def _parse_args_form() -> tuple[list[dict], str | None]:
    """Read the repeated arg rows from the report form.

    Returns (args, error). Rows are aligned parallel lists; blank-name rows are
    dropped. Every arg name must be a SQL-safe identifier (it becomes a macro
    parameter).
    """
    names = request.form.getlist("arg_name")
    types = request.form.getlist("arg_type")
    widgets = request.form.getlist("arg_widget")
    defaults = request.form.getlist("arg_default")
    requireds = request.form.getlist("arg_required")
    constraints = request.form.getlist("arg_constraint")
    descriptions = request.form.getlist("arg_description")

    def _at(items: list[str], i: int, default: str = "") -> str:
        return items[i] if i < len(items) else default

    args: list[dict] = []
    for i, name in enumerate(names):
        name = name.strip()
        if not name:
            continue
        if not ALIAS_RE.match(name):
            return [], f"Argument name '{name}' must be a letter/underscore identifier."
        if name in RESERVED_KEYS:
            return [], (
                f"Argument name '{name}' is reserved for sorting/filtering "
                "(sort, page, q, format, key)."
            )
        data_type = _at(types, i, "text")
        if data_type not in DATA_TYPES:
            return [], f"Unknown argument type '{data_type}'."
        widget = _at(widgets, i, "input")
        if widget not in WIDGETS:
            return [], f"Unknown widget '{widget}'."
        args.append(
            {
                "name": name,
                "data_type": data_type,
                "widget": widget,
                "default_value": _at(defaults, i).strip(),
                "required": _at(requireds, i, "yes") == "yes",
                "constraint_query": _at(constraints, i).strip() or None,
                "description": _at(descriptions, i).strip(),
            }
        )
    return args, None


# ---------------------------------------------------------------------------
# Datamodule CRUD + build
# ---------------------------------------------------------------------------


@bp.route("/")
@admin_required
def index() -> str:
    return render_template("datamodules/index.html", datamodules=Datamodule.get_all())


@bp.route("/new", methods=["GET", "POST"])
@admin_required
def new() -> str | Response:
    if request.method == "POST":
        path = request.form.get("path", "").strip()
        description = request.form.get("description", "").strip()
        build_script = request.form.get("build_script", "")
        ttl_raw = request.form.get("ttl_seconds", "")

        if not path:
            flash("Path is required.", "error")
        elif Datamodule.get_by_path(path) is not None:
            flash(f"A datamodule with path '{path}' already exists.", "error")
        else:
            dm = Datamodule.create(
                path=path,
                created_by=g.user.username,
                description=description,
                build_script=build_script,
                ttl_seconds=_parse_ttl(ttl_raw),
            )
            flash("Datamodule created. Build it to produce a live version.", "success")
            return redirect(url_for("datamodules.edit", uuid=dm.uuid))

    return render_template("datamodules/edit.html", datamodule=None, reports=[], versions=[])


@bp.route("/<uuid>/edit", methods=["GET", "POST"])
@admin_required
def edit(uuid: str) -> str | Response:
    dm = Datamodule.get_by_uuid(uuid)
    if dm is None:
        raise NotFound()

    if request.method == "POST":
        path = request.form.get("path", "").strip()
        description = request.form.get("description", "").strip()
        build_script = request.form.get("build_script", "")
        ttl_raw = request.form.get("ttl_seconds", "")

        existing = Datamodule.get_by_path(path)
        if not path:
            flash("Path is required.", "error")
        elif existing is not None and existing.id != dm.id:
            flash(f"A datamodule with path '{path}' already exists.", "error")
        else:
            dm.update(
                modified_by=g.user.username,
                path=path,
                description=description,
                build_script=build_script,
                ttl_seconds=_parse_ttl(ttl_raw),
            )
            flash("Datamodule saved.", "success")
            return redirect(url_for("datamodules.edit", uuid=dm.uuid))

    deps = Dependency.get_for_datamodule(dm.id)
    dep_names = {d.depends_on_id: Datamodule.get_by_id(d.depends_on_id) for d in deps}
    conns = DatamoduleConnection.get_for_datamodule(dm.id)
    conn_names = {c.connection_id: Connection.get_by_id(c.connection_id) for c in conns}
    return render_template(
        "datamodules/edit.html",
        datamodule=dm,
        reports=Report.get_for_datamodule(dm.id),
        versions=DatamoduleVersion.list_for(dm.id),
        sources=SourceFile.get_for_datamodule(dm.id),
        dependencies=deps,
        dep_names=dep_names,
        connections=conns,
        conn_names=conn_names,
        all_datamodules=[d for d in Datamodule.get_all() if d.id != dm.id],
        all_connections=Connection.get_all(),
    )


@bp.route("/<uuid>/build", methods=["POST"])
@admin_required
def build(uuid: str) -> Response:
    dm = Datamodule.get_by_uuid(uuid)
    if dm is None:
        raise NotFound()
    try:
        version = datamodule_service.build_datamodule(dm, triggered_by=g.user.username)
        flash(f"Built version {version.uuid[:8]} ({version.byte_size} bytes).", "success")
    except Exception as exc:
        flash(f"Build failed: {exc}", "error")
    return redirect(url_for("datamodules.edit", uuid=dm.uuid))


@bp.route("/<uuid>/delete", methods=["POST"])
@admin_required
def delete(uuid: str) -> Response:
    dm = Datamodule.get_by_uuid(uuid)
    if dm is None:
        raise NotFound()
    path = dm.path
    dm.delete()
    flash(f"Datamodule '{path}' deleted.", "success")
    return redirect(url_for("datamodules.index"))


# ---------------------------------------------------------------------------
# Query-report authoring (nested under a datamodule)
# ---------------------------------------------------------------------------


@bp.route("/<uuid>/reports/new", methods=["GET", "POST"])
@admin_required
def report_new(uuid: str) -> str | Response:
    dm = Datamodule.get_by_uuid(uuid)
    if dm is None:
        raise NotFound()

    if request.method == "POST":
        error = _save_report(dm, report=None)
        if error is None:
            flash("Report created.", "success")
            return redirect(url_for("datamodules.edit", uuid=dm.uuid))
        flash(error, "error")

    return render_template(
        "datamodules/report_edit.html",
        datamodule=dm,
        report=None,
        args=[],
        data_types=DATA_TYPES,
        widgets=WIDGETS,
    )


@bp.route("/reports/<report_uuid>/edit", methods=["GET", "POST"])
@admin_required
def report_edit(report_uuid: str) -> str | Response:
    report = Report.get_by_uuid(report_uuid)
    if report is None:
        raise NotFound()
    dm = Datamodule.get_by_id(report.datamodule_id)
    if dm is None:
        raise NotFound()

    if request.method == "POST":
        error = _save_report(dm, report=report)
        if error is None:
            flash("Report saved.", "success")
            return redirect(url_for("datamodules.report_edit", report_uuid=report.uuid))
        flash(error, "error")
        report = Report.get_by_uuid(report_uuid)

    return render_template(
        "datamodules/report_edit.html",
        datamodule=dm,
        report=report,
        args=ReportArg.get_for_report(report.id) if report else [],
        data_types=DATA_TYPES,
        widgets=WIDGETS,
    )


@bp.route("/reports/<report_uuid>/delete", methods=["POST"])
@admin_required
def report_delete(report_uuid: str) -> Response:
    report = Report.get_by_uuid(report_uuid)
    if report is None:
        raise NotFound()
    dm = Datamodule.get_by_id(report.datamodule_id)
    report.delete()
    flash(f"Report '{report.name}' deleted.", "success")
    if dm is None:
        return redirect(url_for("datamodules.index"))
    return redirect(url_for("datamodules.edit", uuid=dm.uuid))


def _save_report(dm: Datamodule, report: Report | None) -> str | None:
    """Create or update a query report from the form. Returns an error or None."""
    name = request.form.get("name", "").strip()
    macro_name = request.form.get("macro_name", "").strip()
    body_sql = request.form.get("body_sql", "")
    default_format = request.form.get("default_format", "html")

    if not name:
        return "Report name is required."
    if not macro_name or not ALIAS_RE.match(macro_name):
        return "Macro name must be a letter/underscore identifier (used as the SQL macro)."

    args, arg_error = _parse_args_form()
    if arg_error:
        return arg_error

    existing = Report.get_by_uuid(report.uuid) if report else None
    try:
        if existing is None:
            saved = Report.create(
                datamodule_id=dm.id,
                name=name,
                macro_name=macro_name,
                body_sql=body_sql,
                created_by=g.user.username,
                default_format=default_format,
            )
        else:
            existing.update(
                modified_by=g.user.username,
                name=name,
                macro_name=macro_name,
                body_sql=body_sql,
                default_format=default_format,
            )
            saved = existing
    except Exception as exc:  # UNIQUE(datamodule_id, macro_name|name) etc.
        return f"Could not save report: {exc}"

    ReportArg.replace_all(saved.id, args)
    return None


# ---------------------------------------------------------------------------
# Build inputs: source files, dependencies, connections
# ---------------------------------------------------------------------------


@bp.route("/<uuid>/sources", methods=["POST"])
@admin_required
def source_upload(uuid: str) -> Response:
    dm = Datamodule.get_by_uuid(uuid)
    if dm is None:
        raise NotFound()
    file = request.files.get("file")
    filename = file.filename if file else None
    if file is None or not filename:
        flash("Choose a file to upload.", "error")
        return redirect(url_for("datamodules.edit", uuid=dm.uuid))
    datamodule_service.save_source(dm, file, filename, uploaded_by=g.user.username)
    flash(f"Uploaded '{filename}'.", "success")
    return redirect(url_for("datamodules.edit", uuid=dm.uuid))


@bp.route("/<uuid>/sources/<int:source_id>/download")
@admin_required
def source_download(uuid: str, source_id: int) -> Response:
    dm = Datamodule.get_by_uuid(uuid)
    if dm is None:
        raise NotFound()
    sf = SourceFile.get_by_id(source_id)
    if sf is None or sf.datamodule_id != dm.id:
        raise NotFound()
    path = datamodule_service.source_disk_path(dm.uuid, sf.stored_name)
    if not path.exists():
        raise NotFound()
    return send_file(str(path), as_attachment=True, download_name=sf.filename)


@bp.route("/<uuid>/sources/<int:source_id>/delete", methods=["POST"])
@admin_required
def source_delete(uuid: str, source_id: int) -> Response:
    dm = Datamodule.get_by_uuid(uuid)
    if dm is None:
        raise NotFound()
    sf = SourceFile.get_by_id(source_id)
    if sf is None or sf.datamodule_id != dm.id:
        raise NotFound()
    datamodule_service.delete_source(dm, sf)
    flash(f"Deleted source '{sf.filename}'.", "success")
    return redirect(url_for("datamodules.edit", uuid=dm.uuid))


@bp.route("/<uuid>/dependencies", methods=["POST"])
@admin_required
def dependency_add(uuid: str) -> Response:
    dm = Datamodule.get_by_uuid(uuid)
    if dm is None:
        raise NotFound()
    depends_on_uuid = request.form.get("depends_on_uuid", "").strip()
    local_name = request.form.get("local_name", "").strip()
    dep_dm = Datamodule.get_by_uuid(depends_on_uuid)

    if not local_name or not ALIAS_RE.match(local_name):
        flash("Local name must be a letter/underscore identifier.", "error")
    elif dep_dm is None:
        flash("Choose a datamodule to depend on.", "error")
    elif dep_dm.id == dm.id:
        flash("A datamodule cannot depend on itself.", "error")
    elif Dependency.reaches(dep_dm.id, dm.id):
        flash(f"Adding this dependency would create a cycle via '{dep_dm.path}'.", "error")
    else:
        try:
            Dependency.create(dm.id, dep_dm.id, local_name)
            flash(f"Added dependency '{local_name}' → {dep_dm.path}.", "success")
        except Exception as exc:  # UNIQUE(datamodule_id, local_name)
            flash(f"Could not add dependency: {exc}", "error")
    return redirect(url_for("datamodules.edit", uuid=dm.uuid))


@bp.route("/<uuid>/dependencies/<int:dep_id>/delete", methods=["POST"])
@admin_required
def dependency_delete(uuid: str, dep_id: int) -> Response:
    dm = Datamodule.get_by_uuid(uuid)
    if dm is None:
        raise NotFound()
    dep = Dependency.get_by_id(dep_id)
    if dep is None or dep.datamodule_id != dm.id:
        raise NotFound()
    dep.delete()
    flash("Dependency removed.", "success")
    return redirect(url_for("datamodules.edit", uuid=dm.uuid))


@bp.route("/<uuid>/connections", methods=["POST"])
@admin_required
def connection_add(uuid: str) -> Response:
    dm = Datamodule.get_by_uuid(uuid)
    if dm is None:
        raise NotFound()
    connection_id = request.form.get("connection_id", type=int)
    local_name = request.form.get("local_name", "").strip()
    crow = Connection.get_by_id(connection_id) if connection_id else None

    if not local_name or not ALIAS_RE.match(local_name):
        flash("Local name must be a letter/underscore identifier.", "error")
    elif crow is None:
        flash("Choose a connection.", "error")
    else:
        try:
            DatamoduleConnection.create(dm.id, crow.id, local_name)
            flash(f"Bound connection '{crow.name}' as '{local_name}'.", "success")
        except Exception as exc:  # UNIQUE(datamodule_id, local_name)
            flash(f"Could not bind connection: {exc}", "error")
    return redirect(url_for("datamodules.edit", uuid=dm.uuid))


@bp.route("/<uuid>/connections/<int:link_id>/delete", methods=["POST"])
@admin_required
def connection_delete(uuid: str, link_id: int) -> Response:
    dm = Datamodule.get_by_uuid(uuid)
    if dm is None:
        raise NotFound()
    link = DatamoduleConnection.get_by_id(link_id)
    if link is None or link.datamodule_id != dm.id:
        raise NotFound()
    link.delete()
    flash("Connection binding removed.", "success")
    return redirect(url_for("datamodules.edit", uuid=dm.uuid))
