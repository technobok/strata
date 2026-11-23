"""Schedules blueprint — when a datamodule is rebuilt, or a report is run and
mailed (admin only).

The form is a builder for common shapes (services/schedule_service.KINDS)
that produces an RRULE; the 'rrule' kind takes RRULE text directly. The
worker's scheduler tick (schedule_service.run_due) queues the builds.
"""

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from werkzeug.exceptions import NotFound
from werkzeug.wrappers import Response

from strata.blueprints.auth import login_required, owner_required
from strata.models.datamodule import Datamodule
from strata.models.report import Report
from strata.models.report_arg import ReportArg
from strata.models.schedule import Schedule
from strata.services import access, notify_service, report_service, schedule_service
from strata.services.schedule_service import Definition, ScheduleError

bp = Blueprint("schedules", __name__, url_prefix="/datamodules")

# Which form fields each kind keeps (the rest are other kinds' inputs).
KIND_FIELDS: dict[str, tuple[str, ...]] = {
    "every": ("every_n", "every_unit", "days", "window_start", "window_end"),
    "daily": ("times", "days"),
    "weekly": ("every_weeks", "days", "time", "start_date"),
    "monthly": ("every_months", "day", "time"),
    "working_day": ("which", "time"),
    "once": ("at",),
    "rrule": ("rrule", "start_date"),
}


def local_time(iso: str | None, timezone: str) -> str:
    """A UTC ISO timestamp shown in the schedule's own timezone."""
    if not iso:
        return ""
    when = datetime.fromisoformat(iso).astimezone(ZoneInfo(timezone))
    return when.strftime("%a %d %b %Y %H:%M %Z")


def _form() -> tuple[str, str, str, dict[str, Any], bool]:
    kind = request.form.get("kind", "every")
    fields: dict[str, Any] = {}
    for name in KIND_FIELDS.get(kind, ()):
        if name == "days":
            fields["days"] = request.form.getlist("days")
        else:
            value = request.form.get(name, "").strip()
            if value:
                fields[name] = value
    timezone = request.form.get("timezone", "") or schedule_service.configured_timezones()[0]
    # An unticked checkbox is simply absent; enabled_present marks the real form.
    enabled = "enabled" in request.form if "enabled_present" in request.form else True
    return request.form.get("name", "").strip(), kind, timezone, fields, enabled


def _recipients_form() -> list[str]:
    return notify_service.recipient_lines(request.form.get("recipients", ""))


def _params_form(args: list[ReportArg]) -> dict[str, list[str]]:
    params: dict[str, list[str]] = {}
    for arg in args:
        raw = request.form.get(f"param_{arg.name}", "").strip()
        if raw:
            parts = raw.split(",") if arg.widget == "multiselect" else [raw]
            params[arg.name] = [p.strip() for p in parts if p.strip()]
    return params


def _check_report_fields(
    recipients: list[str], args: list[ReportArg], params: dict[str, list[str]]
) -> None:
    problem = notify_service.check_recipients(recipients)
    if problem:
        raise ScheduleError(problem)
    try:
        report_service.check_args(args, params)
    except report_service.ArgError as exc:
        raise ScheduleError(
            f"{exc}: set it on this schedule, or give the argument a default."
        ) from exc


def _next_run_iso(definition: Definition) -> str | None:
    nxt = schedule_service.next_run(definition, datetime.now(UTC))
    return nxt.isoformat() if nxt else None


def _render(
    dm: Datamodule, schedule: Schedule | None, values: dict[str, Any], report: Report | None
) -> str:
    return render_template(
        "schedules/edit.html",
        datamodule=dm,
        schedule=schedule,
        report=report,
        report_args=ReportArg.get_for_report(report.id) if report else [],
        values=values,
        kinds=schedule_service.KINDS,
        days=schedule_service.DAYS,
        day_names=schedule_service.DAY_NAMES,
        timezones=schedule_service.configured_timezones(),
        outside=access.recipients_outside(dm, values.get("recipients") or [])
        if report is not None
        else None,
        restricted=access.is_restricted(dm),
    )


def _values_from(schedule: Schedule | None) -> dict[str, Any]:
    if schedule is None:
        return {
            "name": "",
            "kind": "every",
            "timezone": schedule_service.configured_timezones()[0],
            "enabled": True,
            "fields": {"every_n": "15", "every_unit": "minutes", "days": [], "every_weeks": "1"},
            "recipients": [],
            "params": {},
        }
    definition = Definition.from_json(schedule.schedule_definition)
    return {
        "name": schedule.name,
        "kind": definition.kind,
        "timezone": definition.timezone,
        "enabled": schedule.enabled,
        "fields": definition.fields,
        "recipients": schedule.recipients,
        "params": schedule.parameters,
    }


def schedule_rows(
    schedules: list[Schedule], dm: Datamodule | None = None
) -> list[dict[str, object]]:
    """Schedules for a list on a page, described and in their own timezone. With ``dm``
    (a report's schedules), recipients outside its viewers are noted."""
    rows: list[dict[str, object]] = []
    for schedule in schedules:
        definition = Definition.from_json(schedule.schedule_definition)
        rows.append(
            {
                "schedule": schedule,
                "description": schedule_service.describe(definition),
                "timezone": definition.timezone,
                "next_run": local_time(schedule.next_run_at, definition.timezone),
                "last_run": local_time(schedule.last_run_at, definition.timezone),
                "outside": access.recipients_outside(dm, schedule.recipients)
                if dm is not None and schedule.report_id is not None
                else None,
            }
        )
    return rows


def _datamodule_or_404(uuid: str) -> Datamodule:
    dm = Datamodule.get_by_uuid(uuid)
    if dm is None:
        raise NotFound()
    return dm


def _schedule_or_404(schedule_uuid: str) -> tuple[Schedule, Datamodule, Report | None]:
    schedule = Schedule.get_by_uuid(schedule_uuid)
    if schedule is None:
        raise NotFound()
    dm = Datamodule.get_by_id(schedule.datamodule_id)
    if dm is None:
        raise NotFound()
    report = Report.get_by_id(schedule.report_id) if schedule.report_id is not None else None
    return schedule, dm, report


def _back(dm: Datamodule, report: Report | None) -> Response:
    if report is not None:
        return redirect(url_for("datamodules.report_edit", report_uuid=report.uuid) + "#schedules")
    return redirect(url_for("datamodules.edit", uuid=dm.uuid) + "#schedules")


def _submit(dm: Datamodule, schedule: Schedule | None, report: Report | None) -> str | Response:
    """Create or update a rebuild schedule, or (``report``) a report schedule."""
    name, kind, timezone, fields, enabled = _form()
    recipients = _recipients_form() if report else []
    args = ReportArg.get_for_report(report.id) if report else []
    params = _params_form(args) if report else {}
    try:
        if not name:
            raise ScheduleError("Name is required.")
        definition = schedule_service.build(kind, fields, timezone)
        if report is not None:
            _check_report_fields(recipients, args, params)
        next_run = _next_run_iso(definition) if enabled else None
        if schedule is None:
            Schedule.create(
                dm.id,
                name,
                definition.to_json(),
                next_run,
                g.user.username,
                enabled=enabled,
                report_id=report.id if report else None,
                recipients=recipients,
                parameters=params,
            )
        else:
            schedule.update(
                name,
                definition.to_json(),
                next_run,
                enabled,
                recipients=recipients if report else None,
                parameters=params if report else None,
            )
    except ScheduleError as exc:
        flash(str(exc), "error")
        values = {"name": name, "kind": kind, "timezone": timezone, "enabled": enabled}
        values.update(fields=fields, recipients=recipients, params=params)
        return _render(dm, schedule, values, report)
    flash(f"Schedule '{name}' {'saved' if schedule else 'created'}.", "success")
    if report is not None and (outside := access.recipients_outside(dm, recipients)):
        flash(f"Mail goes to them anyway, but some cannot open the run: {outside}.", "error")
    return _back(dm, report)


@bp.route("/<uuid>/schedules/new", methods=["GET", "POST"])
@owner_required
def new(uuid: str) -> str | Response:
    dm = _datamodule_or_404(uuid)
    if request.method == "POST":
        return _submit(dm, None, None)
    return _render(dm, None, _values_from(None), None)


@bp.route("/reports/<report_uuid>/schedules/new", methods=["GET", "POST"])
@owner_required
def report_new(report_uuid: str) -> str | Response:
    report = Report.get_by_uuid(report_uuid)
    dm = Datamodule.get_by_id(report.datamodule_id) if report else None
    if report is None or dm is None:
        raise NotFound()
    if request.method == "POST":
        return _submit(dm, None, report)
    return _render(dm, None, _values_from(None), report)


@bp.route("/schedules/<schedule_uuid>/edit", methods=["GET", "POST"])
@owner_required
def edit(schedule_uuid: str) -> str | Response:
    schedule, dm, report = _schedule_or_404(schedule_uuid)
    if request.method == "POST":
        return _submit(dm, schedule, report)
    return _render(dm, schedule, _values_from(schedule), report)


@bp.route("/schedules/<schedule_uuid>/toggle", methods=["POST"])
@owner_required
def toggle(schedule_uuid: str) -> Response:
    schedule, dm, report = _schedule_or_404(schedule_uuid)
    enabling = not schedule.enabled
    next_run = None
    if enabling:
        next_run = _next_run_iso(Definition.from_json(schedule.schedule_definition))
    schedule.set_enabled(enabling, next_run)
    flash(f"Schedule '{schedule.name}' {'enabled' if enabling else 'disabled'}.", "success")
    return _back(dm, report)


@bp.route("/schedules/<schedule_uuid>/delete", methods=["POST"])
@owner_required
def delete(schedule_uuid: str) -> Response:
    schedule, dm, report = _schedule_or_404(schedule_uuid)
    schedule.delete()
    flash(f"Schedule '{schedule.name}' deleted.", "success")
    return _back(dm, report)


@bp.route("/schedules/preview", methods=["POST"])
@login_required
def preview() -> str:
    """HTMX: the next runs the form as filled in would produce, or why it is invalid."""
    _, kind, timezone, fields, _ = _form()
    try:
        definition = schedule_service.build(kind, fields, timezone)
        runs = schedule_service.upcoming(definition, datetime.now(UTC), 5)
        error = None
    except ScheduleError as exc:
        definition, runs, error = None, [], str(exc)
    return render_template(
        "schedules/_preview.html",
        definition=definition,
        runs=[local_time(r.isoformat(), timezone) for r in runs],
        error=error,
    )
