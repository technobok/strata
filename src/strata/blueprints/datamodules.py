"""Datamodules blueprint — author datamodules, build them, define query reports.

Authoring is admin-gated: authors are IT only, so there is no author/runner split.
The public serve path for running a report lives in blueprints/reports.py.
"""

import json
from datetime import date

from flask import (
    Blueprint,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.exceptions import NotFound
from werkzeug.wrappers import Response

from strata.blueprints.auth import admin_required
from strata.models.api_key import ApiKey
from strata.models.connection import Connection
from strata.models.datamodule import Datamodule
from strata.models.datamodule_connection import DatamoduleConnection
from strata.models.datamodule_version import DatamoduleVersion
from strata.models.dependency import Dependency
from strata.models.report import Report
from strata.models.report_arg import DATA_TYPES, WIDGETS, ReportArg
from strata.models.source_file import SourceFile
from strata.services import datamodule_service, duck
from strata.services.query_filter import RESERVED_KEYS
from strata.services.template_service import ALIAS_RE

bp = Blueprint("datamodules", __name__, url_prefix="/datamodules")


def _parse_ttl(raw: str) -> int | None:
    raw = raw.strip()
    if raw == "":
        return None
    return int(raw)


def _parse_retention() -> tuple[str | None, int | None, object, str | None]:
    """Retention fields from the edit form -> (kind, count, age, error).

    kind None = fields absent (the create form has none) -> leave unchanged.
    age uses the Datamodule._UNSET sentinel for "unchanged" because None is a
    real value (keep everything).
    """
    kind = request.form.get("retention_kind", "").strip()
    if not kind:
        return None, None, Datamodule._UNSET, None
    if kind not in ("count", "age"):
        return None, None, Datamodule._UNSET, "Retention policy must be 'count' or 'age'."
    count: int | None = None
    count_raw = request.form.get("retention_count", "").strip()
    if count_raw:
        try:
            count = int(count_raw)
        except ValueError:
            return None, None, Datamodule._UNSET, "Retention count must be an integer."
        if count < 1:
            return None, None, Datamodule._UNSET, "Retention count must be at least 1."
    age_raw = request.form.get("retention_age_seconds", "").strip()
    age: object
    try:
        age = int(age_raw) if age_raw else None  # blank = keep everything
    except ValueError:
        return None, None, Datamodule._UNSET, "Retention age must be a whole number of seconds."
    if isinstance(age, int) and age < 0:
        return None, None, Datamodule._UNSET, "Retention age cannot be negative."
    return kind, count, age, None


def _parse_fixed_args(text: str) -> tuple[dict[str, str | list[str]], str | None]:
    """API-key fixed args: one ``name=value`` per line -> dict.

    A repeated name accumulates a JSON list (how a multiselect arg is fixed).
    """
    fixed: dict[str, str | list[str]] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "=" not in line:
            return {}, f"Fixed args: '{line}' is not name=value."
        name, value = line.split("=", 1)
        name = name.strip()
        if not ALIAS_RE.match(name):
            return {}, f"Fixed args: '{name}' is not a valid argument name."
        value = value.strip()
        existing = fixed.get(name)
        if existing is None:
            fixed[name] = value
        elif isinstance(existing, list):
            existing.append(value)
        else:
            fixed[name] = [existing, value]
    return fixed, None


def _parse_whitelist(raw: str) -> tuple[list[str] | None, str | None]:
    """Comma-separated caller-suppliable arg names; blank = no restriction."""
    raw = raw.strip()
    if not raw:
        return None, None
    names = [n.strip() for n in raw.split(",") if n.strip()]
    for name in names:
        if not ALIAS_RE.match(name):
            return None, f"Caller-suppliable args: '{name}' is not a valid argument name."
    return names, None


def _parse_expires(raw: str) -> tuple[str | None, str | None]:
    """Optional date -> valid through the end of that day UTC."""
    raw = raw.strip()
    if not raw:
        return None, None
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        return None, "Expiry must be a date (YYYY-MM-DD)."
    return f"{day.isoformat()}T23:59:59+00:00", None


def _apikey_datamodule(key: ApiKey) -> Datamodule | None:
    """Owning datamodule (direct for dm scope, via the report otherwise)."""
    if key.datamodule_id is not None:
        return Datamodule.get_by_id(key.datamodule_id)
    if key.report_id is not None:
        rpt = Report.get_by_id(key.report_id)
        if rpt is not None:
            return Datamodule.get_by_id(rpt.datamodule_id)
    return None


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
                "(sort, page, q, format, key, version)."
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
        retention_kind, retention_count, retention_age, retention_error = _parse_retention()

        existing = Datamodule.get_by_path(path)
        if not path:
            flash("Path is required.", "error")
        elif existing is not None and existing.id != dm.id:
            flash(f"A datamodule with path '{path}' already exists.", "error")
        elif retention_error:
            flash(retention_error, "error")
        else:
            dm.update(
                modified_by=g.user.username,
                path=path,
                description=description,
                build_script=build_script,
                ttl_seconds=_parse_ttl(ttl_raw),
                retention_kind=retention_kind,
                retention_count=retention_count,
                retention_age_seconds=retention_age,
            )
            flash("Datamodule saved.", "success")
            return redirect(url_for("datamodules.edit", uuid=dm.uuid))

    deps = Dependency.get_for_datamodule(dm.id)
    dep_names = {d.depends_on_id: Datamodule.get_by_id(d.depends_on_id) for d in deps}
    conns = DatamoduleConnection.get_for_datamodule(dm.id)
    conn_names = {c.connection_id: Connection.get_by_id(c.connection_id) for c in conns}
    reports = Report.get_for_datamodule(dm.id)

    # One-time API-token reveal: create/rotate stashed the plaintext in the
    # session; it renders exactly once, with ready-made PowerQuery URLs.
    api_keys = ApiKey.get_for_datamodule(dm.id)
    reveal = session.pop("new_api_token", None)
    reveal_urls: list[tuple[str, str]] = []
    if reveal:
        reveal_key = ApiKey.get_by_uuid(reveal.get("key_uuid", ""))
        if reveal_key is None:
            reveal = None
        else:
            targets = (
                [r for r in reports if r.id == reveal_key.report_id]
                if reveal_key.scope_type == "report"
                else reports
            )
            reveal_urls = [
                (
                    r.name,
                    url_for(
                        "reports.serve",
                        report_uuid=r.uuid,
                        ext="parquet",
                        key=reveal["token"],
                        _external=True,
                    ),
                )
                for r in targets
            ]

    return render_template(
        "datamodules/edit.html",
        datamodule=dm,
        reports=reports,
        report_by_id={r.id: r for r in reports},
        api_keys=api_keys,
        reveal=reveal,
        reveal_urls=reveal_urls,
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
    try:
        duck.validate_select(body_sql, "The report body")
        for arg in args:
            if arg["constraint_query"]:
                duck.validate_select(
                    arg["constraint_query"], f"The constraint query for '{arg['name']}'"
                )
    except duck.SqlShapeError as exc:
        return str(exc)

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


# ---------------------------------------------------------------------------
# API keys (unauthenticated data access for Excel/PowerQuery)
# ---------------------------------------------------------------------------


def _apikey_redirect(dm: Datamodule | None) -> Response:
    if dm is None:
        return redirect(url_for("datamodules.index"))
    return redirect(url_for("datamodules.edit", uuid=dm.uuid) + "#api-keys")


@bp.route("/<uuid>/apikeys", methods=["POST"])
@admin_required
def apikey_create(uuid: str) -> Response:
    dm = Datamodule.get_by_uuid(uuid)
    if dm is None:
        raise NotFound()

    name = request.form.get("name", "").strip()
    scope = request.form.get("scope", "datamodule")
    fixed, fixed_error = _parse_fixed_args(request.form.get("fixed_args", ""))
    whitelist, wl_error = _parse_whitelist(request.form.get("whitelist", ""))
    expires_at, exp_error = _parse_expires(request.form.get("expires", ""))

    error = fixed_error or wl_error or exp_error
    if not name:
        error = "API key name is required."
    scope_type = "datamodule"
    datamodule_id: int | None = dm.id
    report_id: int | None = None
    if error is None and scope != "datamodule":
        rpt = (
            Report.get_by_uuid(scope.removeprefix("report:"))
            if scope.startswith("report:")
            else None
        )
        if rpt is None or rpt.datamodule_id != dm.id:
            error = "Scope report not found on this datamodule."
        else:
            scope_type = "report"
            datamodule_id = None
            report_id = rpt.id
    if error is not None:
        flash(error, "error")
        return _apikey_redirect(dm)

    key, token = ApiKey.create(
        name=name,
        scope_type=scope_type,
        datamodule_id=datamodule_id,
        report_id=report_id,
        fixed_args_json=json.dumps(fixed) if fixed else None,
        parameterised_args_json=json.dumps(whitelist) if whitelist is not None else None,
        expires_at=expires_at,
        created_by=g.user.username,
    )
    session["new_api_token"] = {"token": token, "key_uuid": key.uuid}
    flash(
        f"API key '{key.name}' created — copy the token below; it cannot be shown again.",
        "success",
    )
    return _apikey_redirect(dm)


@bp.route("/apikeys/<key_uuid>/rotate", methods=["POST"])
@admin_required
def apikey_rotate(key_uuid: str) -> Response:
    key = ApiKey.get_by_uuid(key_uuid)
    if key is None:
        raise NotFound()
    dm = _apikey_datamodule(key)
    token = key.rotate()
    session["new_api_token"] = {"token": token, "key_uuid": key.uuid}
    flash(f"API key '{key.name}' rotated — the old token no longer works.", "success")
    return _apikey_redirect(dm)


@bp.route("/apikeys/<key_uuid>/toggle", methods=["POST"])
@admin_required
def apikey_toggle(key_uuid: str) -> Response:
    key = ApiKey.get_by_uuid(key_uuid)
    if key is None:
        raise NotFound()
    dm = _apikey_datamodule(key)
    key.set_enabled(not key.enabled)
    flash(f"API key '{key.name}' {'enabled' if key.enabled else 'disabled'}.", "success")
    return _apikey_redirect(dm)


@bp.route("/apikeys/<key_uuid>/delete", methods=["POST"])
@admin_required
def apikey_delete(key_uuid: str) -> Response:
    key = ApiKey.get_by_uuid(key_uuid)
    if key is None:
        raise NotFound()
    dm = _apikey_datamodule(key)  # resolve before delete (needs the report row)
    key.delete()
    flash(f"API key '{key.name}' deleted.", "success")
    return _apikey_redirect(dm)


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
