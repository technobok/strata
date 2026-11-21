"""Schedules blueprint — when a datamodule is rebuilt (admin only).

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

from strata.blueprints.auth import admin_required
from strata.models.datamodule import Datamodule
from strata.models.schedule import Schedule
from strata.services import schedule_service
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


def _next_run_iso(definition: Definition) -> str | None:
    nxt = schedule_service.next_run(definition, datetime.now(UTC))
    return nxt.isoformat() if nxt else None


def _render(dm: Datamodule, schedule: Schedule | None, values: dict[str, Any]) -> str:
    return render_template(
        "schedules/edit.html",
        datamodule=dm,
        schedule=schedule,
        values=values,
        kinds=schedule_service.KINDS,
        days=schedule_service.DAYS,
        day_names=schedule_service.DAY_NAMES,
        timezones=schedule_service.configured_timezones(),
    )


def _values_from(schedule: Schedule | None) -> dict[str, Any]:
    if schedule is None:
        return {
            "name": "",
            "kind": "every",
            "timezone": schedule_service.configured_timezones()[0],
            "enabled": True,
            "fields": {"every_n": "15", "every_unit": "minutes", "days": [], "every_weeks": "1"},
        }
    definition = Definition.from_json(schedule.schedule_definition)
    return {
        "name": schedule.name,
        "kind": definition.kind,
        "timezone": definition.timezone,
        "enabled": schedule.enabled,
        "fields": definition.fields,
    }


def _datamodule_or_404(uuid: str) -> Datamodule:
    dm = Datamodule.get_by_uuid(uuid)
    if dm is None:
        raise NotFound()
    return dm


def _schedule_or_404(schedule_uuid: str) -> tuple[Schedule, Datamodule]:
    schedule = Schedule.get_by_uuid(schedule_uuid)
    if schedule is None:
        raise NotFound()
    dm = Datamodule.get_by_id(schedule.datamodule_id)
    if dm is None:
        raise NotFound()
    return schedule, dm


def _back(dm: Datamodule) -> Response:
    return redirect(url_for("datamodules.edit", uuid=dm.uuid) + "#schedules")


@bp.route("/<uuid>/schedules/new", methods=["GET", "POST"])
@admin_required
def new(uuid: str) -> str | Response:
    dm = _datamodule_or_404(uuid)
    if request.method == "POST":
        name, kind, timezone, fields, enabled = _form()
        try:
            if not name:
                raise ScheduleError("Name is required.")
            definition = schedule_service.build(kind, fields, timezone)
            Schedule.create(
                dm.id,
                name,
                definition.to_json(),
                _next_run_iso(definition) if enabled else None,
                g.user.username,
                enabled=enabled,
            )
        except ScheduleError as exc:
            flash(str(exc), "error")
            values = {"name": name, "kind": kind, "timezone": timezone, "enabled": enabled}
            return _render(dm, None, {**values, "fields": fields})
        flash(f"Schedule '{name}' created.", "success")
        return _back(dm)
    return _render(dm, None, _values_from(None))


@bp.route("/schedules/<schedule_uuid>/edit", methods=["GET", "POST"])
@admin_required
def edit(schedule_uuid: str) -> str | Response:
    schedule, dm = _schedule_or_404(schedule_uuid)
    if request.method == "POST":
        name, kind, timezone, fields, enabled = _form()
        try:
            if not name:
                raise ScheduleError("Name is required.")
            definition = schedule_service.build(kind, fields, timezone)
            schedule.update(
                name, definition.to_json(), _next_run_iso(definition) if enabled else None, enabled
            )
        except ScheduleError as exc:
            flash(str(exc), "error")
            values = {"name": name, "kind": kind, "timezone": timezone, "enabled": enabled}
            return _render(dm, schedule, {**values, "fields": fields})
        flash(f"Schedule '{name}' saved.", "success")
        return _back(dm)
    return _render(dm, schedule, _values_from(schedule))


@bp.route("/schedules/<schedule_uuid>/toggle", methods=["POST"])
@admin_required
def toggle(schedule_uuid: str) -> Response:
    schedule, dm = _schedule_or_404(schedule_uuid)
    enabling = not schedule.enabled
    next_run = None
    if enabling:
        next_run = _next_run_iso(Definition.from_json(schedule.schedule_definition))
    schedule.set_enabled(enabling, next_run)
    flash(f"Schedule '{schedule.name}' {'enabled' if enabling else 'disabled'}.", "success")
    return _back(dm)


@bp.route("/schedules/<schedule_uuid>/delete", methods=["POST"])
@admin_required
def delete(schedule_uuid: str) -> Response:
    schedule, dm = _schedule_or_404(schedule_uuid)
    schedule.delete()
    flash(f"Schedule '{schedule.name}' deleted.", "success")
    return _back(dm)


@bp.route("/schedules/preview", methods=["POST"])
@admin_required
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
