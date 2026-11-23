"""Datamodules blueprint — author datamodules, build them, define query reports.

Each datamodule's owners (and the admins) edit it: every route here that names a
datamodule, report, schedule or key is ``owner_required``. Writers create
datamodules. The serve path for running a report lives in blueprints/reports.py.
"""

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from flask import (
    Blueprint,
    flash,
    g,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from werkzeug.exceptions import NotFound
from werkzeug.wrappers import Response

from strata.blueprints import reads as reads_page
from strata.blueprints.auth import login_required, owner_required, writer_required
from strata.models.api_key import ApiKey
from strata.models.build_job import BuildJob
from strata.models.connection import Connection
from strata.models.datamodule import Datamodule
from strata.models.datamodule_connection import DatamoduleConnection
from strata.models.datamodule_version import DatamoduleVersion
from strata.models.dependency import Dependency
from strata.models.report import Report
from strata.models.report_arg import DATA_TYPES, WIDGETS, ReportArg
from strata.models.report_check import SEVERITIES, ReportCheck
from strata.models.report_run import ReportRun
from strata.models.source_file import SourceFile
from strata.models.worker_process import WorkerProcess
from strata.services import (
    access,
    build_queue,
    connection_service,
    datamodule_service,
    duck,
    read_audit,
    run_service,
    script_template,
)
from strata.services.query_filter import RESERVED_KEYS
from strata.services.template_service import ALIAS_RE

bp = Blueprint("datamodules", __name__, url_prefix="/datamodules")

_QUEUE_RE = re.compile(r"[a-z0-9_-]{1,32}")


def _parse_ttl(raw: str) -> tuple[int | None, str | None]:
    """TTL form field -> (seconds, error). Blank = never expire; 0 = always rebuild."""
    raw = raw.strip()
    if raw == "":
        return None, None
    try:
        seconds = int(raw)
    except ValueError:
        return None, f"TTL must be a whole number of seconds, not '{raw}'."
    if seconds < 0:
        return None, "TTL cannot be negative (blank = never expire, 0 = always rebuild)."
    return seconds, None


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
@login_required
def index() -> str:
    """The datamodules the user may edit (an admin: all of them)."""
    owned = [dm for dm in Datamodule.get_all() if access.can_own(dm)]
    return render_template("datamodules/index.html", datamodules=owned)


@bp.route("/new", methods=["GET", "POST"])
@writer_required
def new() -> str | Response:
    if request.method == "POST":
        path = request.form.get("path", "").strip()
        description = request.form.get("description", "").strip()
        build_script = request.form.get("build_script", "")
        ttl, ttl_error = _parse_ttl(request.form.get("ttl_seconds", ""))
        lists, access_error = _parse_access()
        owners, viewers = lists if lists is not None else (None, None)

        if not path:
            flash("Path is required.", "error")
        elif Datamodule.get_by_path(path) is not None:
            flash(f"A datamodule with path '{path}' already exists.", "error")
        elif ttl_error:
            flash(ttl_error, "error")
        elif access_error or (access_error := _stays_owner(owners)):
            flash(access_error, "error")
        else:
            dm = Datamodule.create(
                path=path,
                created_by=g.user.username,
                description=description,
                build_script=build_script,
                ttl_seconds=ttl,
                owners=owners,
                viewers=viewers,
            )
            flash("Datamodule created. Build it to produce a live version.", "success")
            return redirect(url_for("datamodules.edit", uuid=dm.uuid))

    return render_template(
        "datamodules/edit.html",
        datamodule=None,
        reports=[],
        versions=[],
        **_access_context(None),
    )


@bp.route("/<uuid>/edit", methods=["GET", "POST"])
@owner_required
def edit(uuid: str) -> str | Response:
    dm = Datamodule.get_by_uuid(uuid)
    if dm is None:
        raise NotFound()

    if request.method == "POST":
        path = request.form.get("path", "").strip()
        description = request.form.get("description", "").strip()
        build_script = request.form.get("build_script", "")
        ttl, ttl_error = _parse_ttl(request.form.get("ttl_seconds", ""))
        retention_kind, retention_count, retention_age, retention_error = _parse_retention()
        build_queue_name = request.form.get("build_queue", "").strip() or None
        alerts, alerts_error = _parse_alerts()
        lists, access_error = _parse_access()
        if lists is not None and access_error is None:
            access_error = _stays_owner(lists[0]) or _inputs_allow(dm, lists[0])

        existing = Datamodule.get_by_path(path)
        if not path:
            flash("Path is required.", "error")
        elif existing is not None and existing.id != dm.id:
            flash(f"A datamodule with path '{path}' already exists.", "error")
        elif retention_error:
            flash(retention_error, "error")
        elif ttl_error:
            flash(ttl_error, "error")
        elif build_queue_name is not None and not _QUEUE_RE.fullmatch(build_queue_name):
            flash("Worker queue: lowercase letters, digits, '-' and '_' only.", "error")
        elif alerts_error:
            flash(alerts_error, "error")
        elif access_error:
            flash(access_error, "error")
        else:
            dm.update(
                modified_by=g.user.username,
                path=path,
                description=description,
                build_script=build_script,
                ttl_seconds=ttl,
                retention_kind=retention_kind,
                retention_count=retention_count,
                retention_age_seconds=retention_age,
                build_queue=build_queue_name,
            )
            if alerts is not None and alerts != (
                dm.alert_recipients or None,
                dm.stale_alert_seconds,
            ):
                dm.set_alerts(*alerts)
            if lists is not None and lists != (dm.owners, dm.viewers):
                dm.set_access(*lists, modified_by=g.user.username)
            flash("Datamodule saved.", "success")
            if lists is not None:
                _warn_dependants(dm)
            return redirect(url_for("datamodules.edit", uuid=dm.uuid))

    deps = Dependency.get_for_datamodule(dm.id)
    dep_names = {d.depends_on_id: Datamodule.get_by_id(d.depends_on_id) for d in deps}
    conns = DatamoduleConnection.get_for_datamodule(dm.id)
    conn_names = {c.connection_id: Connection.get_by_id(c.connection_id) for c in conns}
    # How the build script reads each binding: by its local name, per driver.
    conn_usage = {
        c.id: connection_service.usage(crow.driver, c.local_name)
        for c in conns
        if (crow := conn_names[c.connection_id]) is not None
    }
    reports = Report.get_for_datamodule(dm.id)

    # Excel: each report's link carries the datamodule's Excel key (made now if it
    # has none, so it is listed with the keys below); every key's links stay shown.
    excel_key = ApiKey.ensure_excel_link(dm.id, g.user.username) if reports else None
    excel_links = (
        {r.id: _excel_url(r, excel_key.link_token()) for r in reports} if excel_key else {}
    )
    api_keys = ApiKey.get_for_datamodule(dm.id)
    key_links = {
        k.id: [
            (r.name, _excel_url(r, k.link_token()))
            for r in reports
            if k.scope_type == "datamodule" or r.id == k.report_id
        ]
        for k in api_keys
        if k.token_encrypted is not None
    }

    jobs = BuildJob.recent_for_datamodule(dm.id)
    return render_template(
        "datamodules/edit.html",
        datamodule=dm,
        reports=reports,
        checks=ReportCheck.for_datamodule(dm.id),
        report_by_id={r.id: r for r in reports},
        api_keys=api_keys,
        excel_links=excel_links,
        key_links=key_links,
        versions=DatamoduleVersion.list_for(dm.id),
        sources=SourceFile.get_for_datamodule(dm.id),
        dependencies=deps,
        dep_names=dep_names,
        connections=conns,
        conn_names=conn_names,
        conn_usage=conn_usage,
        # Only what the user may read: a picker must not show (say) payroll's path.
        all_datamodules=[d for d in Datamodule.get_all() if d.id != dm.id and access.can_view(d)],
        all_connections=[c for c in Connection.get_all() if access.may_use(c)],
        jobs=jobs,
        run_uuids=ReportRun.uuids_for([j.run_id for j in jobs if j.run_id is not None]),
        schedules=_schedule_rows(dm),
        **_status_context(dm),
        **_access_context(dm),
    )


def _excel_url(report: Report, token: str) -> str:
    """A report's data link for Excel's From Web: xlsx (it cannot open Parquet)."""
    return url_for("reports.serve", report_uuid=report.uuid, ext="xlsx", key=token, _external=True)


def _schedule_rows(dm: Datamodule) -> list[dict[str, object]]:
    """The datamodule's rebuild schedules, described and in their own timezone."""
    from strata.blueprints.schedules import schedule_rows
    from strata.models.schedule import Schedule

    return schedule_rows(Schedule.for_datamodule(dm.id))


def _report_schedule_rows(report: Report, dm: Datamodule) -> list[dict[str, object]]:
    from strata.blueprints.schedules import schedule_rows
    from strata.models.schedule import Schedule

    return schedule_rows(Schedule.for_report(report.id), dm)


def _parse_alerts() -> tuple[tuple[list[str] | None, int | None] | None, str | None]:
    """((alert list or None = admins, stale seconds or None), error); (None, None) when
    the form has no Alerts section, so a partial post leaves them alone."""
    from strata.services import notify_service

    if request.form.get("alerts_present") != "1":
        return None, None
    recipients = notify_service.recipient_lines(request.form.get("alert_recipients", ""))
    problem = notify_service.check_recipients(recipients)
    if problem:
        return None, f"Alert list: {problem}"
    raw = request.form.get("stale_alert_hours", "").strip()
    try:
        hours = float(raw) if raw else None
    except ValueError:
        return None, "Stale alert: a number of hours, or blank for none."
    if hours is not None and hours <= 0:
        return None, "Stale alert: a number of hours above zero, or blank for none."
    return (recipients or None, round(hours * 3600) if hours else None), None


def _parse_access() -> tuple[tuple[list[str] | None, list[str] | None] | None, str | None]:
    """((owners, viewers), error) from the Access fieldset; a list is None when its
    "use the default" box is ticked. (None, None) when the form has no Access
    fieldset, so a partial post (e.g. strata.set_script) leaves access alone."""
    if request.form.get("access_present") != "1":
        return None, None
    owners, viewers = (
        None
        if request.form.get(f"{name}_default") == "1"
        else access.principal_lines(request.form.get(name, ""))
        for name in ("owners", "viewers")
    )
    named = (owners or []) + (viewers or [])
    problem = access.check_principals(named)
    if problem:
        return None, f"Access: {problem}"
    if owners and access.EVERYONE in owners:
        return None, "Access: owners are people or groups; * (everyone signed in) can only view."
    try:
        unknown = access.Directory().unknown(named)
    except access.DirectoryUnavailable as exc:
        return None, f"Access: the names could not be checked: {exc}."
    if unknown:
        return None, f"Access: Gatekeeper has no user or group {', '.join(unknown)}."
    return (owners, viewers), None


def _stays_owner(owners: list[str] | None) -> str | None:
    """Why the signed-in user may not save these owners: they would lock themselves
    out. Admins may (they own everything anyway)."""
    if access.is_admin() or access.matches(
        g.user, owners if owners is not None else access.default_owners()
    ):
        return None
    return (
        "Access: you would no longer be an owner. Keep yourself, or a group you are in, in Owners."
    )


def _inputs_allow(dm: Datamodule, owners: list[str] | None) -> str | None:
    """Why these owners may not own ``dm``: one of them may not use a bound connection."""
    try:
        problems = access.input_problems(
            dm, owners if owners is not None else access.default_owners(), access.Directory()
        )
    except access.DirectoryUnavailable as exc:
        return f"Access: the owners could not be checked: {exc}."
    return f"Access: {'; '.join(problems)}." if problems else None


def _warn_dependants(dm: Datamodule) -> None:
    """Say which datamodules that depend on ``dm`` have owners who may no longer read
    it: their next build fails and locks them. A warning, not a refusal: narrowing who
    reads this datamodule is its owners' call."""
    affected: list[str] = []
    for dep in Dependency.dependants_of(dm.id):
        child = Datamodule.get_by_id(dep.datamodule_id)
        if child is None:
            continue
        try:
            problem = access.parent_problem(
                access.owners_of(child), dm, dep.local_name, access.Directory()
            )
        except access.DirectoryUnavailable:
            problem = "(could not be checked)"
        if problem:
            affected.append(f"{child.path}: {problem}")
    if affected:
        flash(
            "These datamodules depend on this one and their next build fails and locks "
            f"them: {'; '.join(affected)}.",
            "error",
        )


def _access_context(dm: Datamodule | None) -> dict[str, object]:
    """The Access fieldset: each list, or the default it follows."""
    return {
        "default_owners": access.default_owners(),
        "default_viewers": access.default_viewers(),
        "owners_default": dm is None or dm.owners is None,
        "viewers_default": dm is None or dm.viewers is None,
        "owners_text": "\n".join(dm.owners or []) if dm is not None else "",
        "viewers_text": "\n".join(dm.viewers or []) if dm is not None else "",
    }


def _status_context(dm: Datamodule) -> dict[str, object]:
    """The build-status line: active jobs, and whether any worker is alive to run them."""
    return {
        "active_jobs": BuildJob.active_for_datamodule(dm.id),
        "worker_missing": not build_queue.inline_mode() and not WorkerProcess.alive(),
    }


@bp.route("/<uuid>/preview-script", methods=["POST"])
@owner_required
def preview_script(uuid: str) -> str:
    """HTMX: the posted build script rendered as a template, against the live
    versions of the datamodule's dependencies (nothing is built)."""
    dm = Datamodule.get_by_uuid(uuid)
    if dm is None:
        raise NotFound()
    read_audit.note("preview", dm)  # rows() reads the dependencies' data
    script = request.form.get("build_script", "")
    rendered, error = None, None
    if not script_template.is_template(script):
        error = "This script has no {{ … }} or {% … %}: it runs as written."
    elif (refused := _preview_refusal(dm)) is not None:
        error = refused  # the preview reads the parents' rows: the build's rule applies
    else:
        dependencies: dict[str, Path | None] = {}
        for dep in Dependency.get_for_datamodule(dm.id):
            dep_dm = Datamodule.get_by_id(dep.depends_on_id)
            live = datamodule_service.live_version(dep_dm) if dep_dm else None
            dependencies[dep.local_name] = (
                datamodule_service.version_path(dep_dm.uuid, live.uuid)
                if dep_dm is not None and live is not None
                else None
            )
        try:
            rendered = script_template.render(script, dependencies)
        except script_template.ScriptTemplateError as exc:
            error = str(exc)
    return render_template("datamodules/_script_preview.html", rendered=rendered, error=error)


def _preview_refusal(dm: Datamodule) -> str | None:
    """Why the preview may not read ``dm``'s parents: the build would refuse too."""
    try:
        access.check_build(dm)
    except access.AccessError as exc:
        return f"Not previewed: {exc}"
    for dep in Dependency.get_for_datamodule(dm.id):
        parent = Datamodule.get_by_id(dep.depends_on_id)
        if parent is not None and parent.access_lock:
            return f"Not previewed: dependency '{dep.local_name}' ({parent.path}) is locked."
    return None


@bp.route("/<uuid>/status")
@owner_required
def status(uuid: str) -> str | Response:
    """HTMX-polled status line; reloads the page once the active build finishes."""
    dm = Datamodule.get_by_uuid(uuid)
    if dm is None:
        raise NotFound()
    context = _status_context(dm)
    html = render_template("datamodules/_status.html", datamodule=dm, **context)
    if not context["active_jobs"]:
        resp = make_response(html)
        resp.headers["HX-Refresh"] = "true"
        return resp
    return html


@bp.route("/<uuid>/versions/<version_uuid>/script")
@owner_required
def version_script(uuid: str, version_uuid: str) -> str:
    """The build script a version was built from (its snapshot, as it ran)."""
    dm = Datamodule.get_by_uuid(uuid)
    if dm is None:
        raise NotFound()
    version = DatamoduleVersion.get_for_datamodule(dm.id, version_uuid)
    if version is None:
        raise NotFound()
    read_audit.note("script", dm, version=version)  # a rendered script can hold rows
    # A Jinja version's snapshot is its rendered text, so it cannot be compared
    # with the current template: say nothing rather than "changed".
    same_as_current = (
        None
        if script_template.is_template(dm.build_script) or version.build_script_snapshot is None
        else version.build_script_snapshot == dm.build_script
    )
    return render_template(
        "datamodules/version_script.html",
        datamodule=dm,
        version=version,
        same_as_current=same_as_current,
    )


@bp.route("/<uuid>/build", methods=["POST"])
@owner_required
def build(uuid: str) -> Response:
    dm = Datamodule.get_by_uuid(uuid)
    if dm is None:
        raise NotFound()
    # "Build with all dependencies" also force-rebuilds every dependency;
    # a plain build refreshes only the stale ones.
    cascade = request.form.get("cascade") == "1"
    job = build_queue.request_build(dm, f"manual:{g.user.username}", force=True, cascade=cascade)
    if job.status == "done":
        flash("Build finished.", "success")
    elif job.status == "failed":
        flash(f"Build failed: {job.error}", "error")
    else:
        flash("Build queued; the worker will run it.", "success")
    return redirect(url_for("datamodules.edit", uuid=dm.uuid))


@bp.route("/<uuid>/delete", methods=["POST"])
@owner_required
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
@owner_required
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
        check=None,
        severities=SEVERITIES,
        runs=[],
        schedules=[],
    )


@bp.route("/reports/<report_uuid>/edit", methods=["GET", "POST"])
@owner_required
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
        check=ReportCheck.get(report.id) if report else None,
        severities=SEVERITIES,
        runs=ReportRun.recent_for_report(report.id) if report else [],
        schedules=_report_schedule_rows(report, dm) if report else [],
    )


@bp.route("/reports/<report_uuid>/delete", methods=["POST"])
@owner_required
def report_delete(report_uuid: str) -> Response:
    report = Report.get_by_uuid(report_uuid)
    if report is None:
        raise NotFound()
    dm = Datamodule.get_by_id(report.datamodule_id)
    if dm is not None:
        run_service.delete_files_for_report(report, dm.uuid)  # rows cascade with the report
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

    if not name:
        return "Report name is required."
    if not macro_name or not ALIAS_RE.match(macro_name):
        return "Macro name must be a letter/underscore identifier (used as the SQL macro)."

    args, arg_error = _parse_args_form()
    if arg_error:
        return arg_error
    is_check = request.form.get("is_check") == "on"
    severity = request.form.get("severity", "warning")
    gate = is_check and request.form.get("gate") == "on"
    if is_check and severity not in SEVERITIES:
        return f"Unknown severity '{severity}'."
    if gate:
        for arg in args:
            if arg["required"] and not arg["default_value"]:
                return (
                    f"A gate check runs inside each build with its arguments' defaults: "
                    f"give '{arg['name']}' a default, make it optional, or untick Gate."
                )
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
            )
        else:
            existing.update(
                modified_by=g.user.username,
                name=name,
                macro_name=macro_name,
                body_sql=body_sql,
            )
            saved = existing
    except Exception as exc:  # UNIQUE(datamodule_id, macro_name|name) etc.
        return f"Could not save report: {exc}"

    ReportArg.replace_all(saved.id, args)
    if is_check:
        ReportCheck.save(saved.id, severity, gate)
    else:
        ReportCheck.clear(saved.id)
    return None


# ---------------------------------------------------------------------------
# Build inputs: source files, dependencies, connections
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# API keys (unauthenticated data access for Excel/PowerQuery)
# ---------------------------------------------------------------------------


def _datamodule_key(key_uuid: str) -> ApiKey:
    key = ApiKey.get_by_uuid(key_uuid)
    if key is None:
        raise NotFound()
    return key


def _apikey_redirect(dm: Datamodule | None) -> Response:
    if dm is None:
        return redirect(url_for("datamodules.index"))
    return redirect(url_for("datamodules.edit", uuid=dm.uuid) + "#api-keys")


@bp.route("/<uuid>/apikeys", methods=["POST"])
@owner_required
def apikey_create(uuid: str) -> Response:
    dm = Datamodule.get_by_uuid(uuid)
    if dm is None:
        raise NotFound()

    # A key reads every report of the datamodule (user decision 2026-11-23: no scope).
    name = request.form.get("name", "").strip()
    fixed, fixed_error = _parse_fixed_args(request.form.get("fixed_args", ""))
    whitelist, wl_error = _parse_whitelist(request.form.get("whitelist", ""))
    expires_at, exp_error = _parse_expires(request.form.get("expires", ""))

    error = fixed_error or wl_error or exp_error
    if not name:
        error = "API key name is required."
    if error is not None:
        flash(error, "error")
        return _apikey_redirect(dm)

    key, _token = ApiKey.create(
        name=name,
        datamodule_id=dm.id,
        fixed_args_json=json.dumps(fixed) if fixed else None,
        parameterised_args_json=json.dumps(whitelist) if whitelist is not None else None,
        expires_at=expires_at,
        created_by=g.user.username,
    )
    flash(f"API key '{key.name}' created: its links are in the API keys table.", "success")
    return _apikey_redirect(dm)


@bp.route("/apikeys/<key_uuid>/rotate", methods=["POST"])
@owner_required
def apikey_rotate(key_uuid: str) -> Response:
    key = _datamodule_key(key_uuid)
    dm = _apikey_datamodule(key)
    key.rotate()
    flash(
        f"API key '{key.name}' rotated: the old token no longer works; its new links are "
        "in the API keys table.",
        "success",
    )
    return _apikey_redirect(dm)


@bp.route("/apikeys/<key_uuid>/toggle", methods=["POST"])
@owner_required
def apikey_toggle(key_uuid: str) -> Response:
    key = _datamodule_key(key_uuid)
    dm = _apikey_datamodule(key)
    key.set_enabled(not key.enabled)
    flash(f"API key '{key.name}' {'enabled' if key.enabled else 'disabled'}.", "success")
    return _apikey_redirect(dm)


@bp.route("/apikeys/<key_uuid>/delete", methods=["POST"])
@owner_required
def apikey_delete(key_uuid: str) -> Response:
    key = _datamodule_key(key_uuid)
    dm = _apikey_datamodule(key)  # resolve before delete (needs the report row)
    key.delete()
    flash(f"API key '{key.name}' deleted.", "success")
    return _apikey_redirect(dm)


@bp.route("/<uuid>/sources", methods=["POST"])
@owner_required
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
@owner_required
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
    read_audit.note("source", dm, query=f"file={sf.filename}"[:200])
    return send_file(str(path), as_attachment=True, download_name=sf.filename)


@bp.route("/<uuid>/reads")
@owner_required
def reads(uuid: str) -> str:
    """The read audit of this datamodule: who read its reports, runs, keys and files."""
    dm = Datamodule.get_by_uuid(uuid)
    if dm is None:
        raise NotFound()

    def url(csv: bool = False, **query: Any) -> str:
        endpoint = "datamodules.reads_csv" if csv else "datamodules.reads"
        return url_for(endpoint, uuid=dm.uuid, **query)

    return reads_page.render(dm, url)


@bp.route("/<uuid>/reads.csv")
@owner_required
def reads_csv(uuid: str) -> Response:
    dm = Datamodule.get_by_uuid(uuid)
    if dm is None:
        raise NotFound()
    return reads_page.csv_response(dm, f"reads-{dm.path.replace('/', '-')}.csv")


@bp.route("/<uuid>/sources/<int:source_id>/delete", methods=["POST"])
@owner_required
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
@owner_required
def dependency_add(uuid: str) -> Response:
    dm = Datamodule.get_by_uuid(uuid)
    if dm is None:
        raise NotFound()
    depends_on_uuid = request.form.get("depends_on_uuid", "").strip()
    local_name = request.form.get("local_name", "").strip()
    dep_dm = Datamodule.get_by_uuid(depends_on_uuid)

    if not local_name or not ALIAS_RE.match(local_name):
        flash("Local name must be a letter/underscore identifier.", "error")
    elif dep_dm is None or not access.can_view(dep_dm):
        # One message either way: a refusal must not confirm what it is.
        flash("Choose a datamodule to depend on.", "error")
    elif dep_dm.id == dm.id:
        flash("A datamodule cannot depend on itself.", "error")
    elif Dependency.reaches(dep_dm.id, dm.id):
        flash(f"Adding this dependency would create a cycle via '{dep_dm.path}'.", "error")
    elif (problem := _owners_may_read(dm, dep_dm, local_name)) is not None:
        flash(f"Not added: {problem}.", "error")
    else:
        try:
            Dependency.create(dm.id, dep_dm.id, local_name)
            flash(f"Added dependency '{local_name}' → {dep_dm.path}.", "success")
        except Exception as exc:  # UNIQUE(datamodule_id, local_name)
            flash(f"Could not add dependency: {exc}", "error")
    return redirect(url_for("datamodules.edit", uuid=dm.uuid))


@bp.route("/<uuid>/dependencies/<int:dep_id>/delete", methods=["POST"])
@owner_required
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
@owner_required
def connection_add(uuid: str) -> Response:
    dm = Datamodule.get_by_uuid(uuid)
    if dm is None:
        raise NotFound()
    connection_id = request.form.get("connection_id", type=int)
    local_name = request.form.get("local_name", "").strip()
    crow = Connection.get_by_id(connection_id) if connection_id else None

    problems = _owners_may_use(dm, crow) if crow is not None and access.may_use(crow) else []
    if not local_name or not ALIAS_RE.match(local_name):
        flash("Local name must be a letter/underscore identifier.", "error")
    elif crow is None or not access.may_use(crow):
        flash("Choose a connection.", "error")  # the same words: nothing is confirmed
    elif problems:
        flash(
            f'Not bound: {problems[0]}. An admin can add them to the connection\'s "May use" list.',
            "error",
        )
    else:
        try:
            DatamoduleConnection.create(dm.id, crow.id, local_name)
            flash(f"Bound connection '{crow.name}' as '{local_name}'.", "success")
        except Exception as exc:  # UNIQUE(datamodule_id, local_name)
            flash(f"Could not bind connection: {exc}", "error")
    return redirect(url_for("datamodules.edit", uuid=dm.uuid))


def _owners_may_read(dm: Datamodule, parent: Datamodule, local_name: str) -> str | None:
    """Why ``dm``'s owners may not depend on ``parent``: one of them may not read it."""
    try:
        return access.parent_problem(access.owners_of(dm), parent, local_name, access.Directory())
    except access.DirectoryUnavailable as exc:
        return f"access could not be checked: {exc}"


def _owners_may_use(dm: Datamodule, crow: Connection) -> list[str]:
    """Why ``dm``'s owners may not use ``crow`` (one line), or []."""
    try:
        missing = access.Directory().missing(access.owners_of(dm), crow.users or [])
    except access.DirectoryUnavailable as exc:
        return [f"access could not be checked: {exc}"]
    if not missing:
        return []
    names = ", ".join(access.describe(p) for p in missing)
    return [f"{names} (owners of this datamodule) may not use the connection '{crow.name}'"]


@bp.route("/<uuid>/connections/<int:link_id>/delete", methods=["POST"])
@owner_required
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
