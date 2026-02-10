"""Schedules blueprint — CRUD for report scheduling."""

import json

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
from werkzeug.wrappers import Response

from strata.blueprints.auth import login_required
from strata.models.report import Report
from strata.models.schedule import Schedule
from strata.services.schedule_service import next_n_runs

bp = Blueprint("schedules", __name__)


@bp.route("/reports/<uuid>/schedules")
@login_required
def index(uuid: str) -> str:
    """List schedules for a report."""
    report = Report.get_by_uuid(uuid)
    if not report:
        abort(404)

    schedules = Schedule.get_for_report(report.id)
    return render_template("schedules/index.html", report=report, schedules=schedules)


@bp.route("/reports/<uuid>/schedules/new", methods=["GET", "POST"])
@login_required
def new(uuid: str) -> str | Response:
    """Create a new schedule."""
    report = Report.get_by_uuid(uuid)
    if not report:
        abort(404)

    if request.method == "POST":
        definition = _parse_schedule_form(request.form)
        if definition is None:
            flash("Invalid schedule definition.", "error")
            return render_template(
                "schedules/edit.html",
                report=report,
                schedule=None,
                form=request.form,
            )

        name = request.form.get("name", "").strip()
        if not name:
            flash("Schedule name is required.", "error")
            return render_template(
                "schedules/edit.html",
                report=report,
                schedule=None,
                form=request.form,
            )

        recipients_raw = request.form.get("recipients", "").strip()
        recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]
        if not recipients:
            flash("At least one recipient email is required.", "error")
            return render_template(
                "schedules/edit.html",
                report=report,
                schedule=None,
                form=request.form,
            )

        max_inline_rows = request.form.get("max_inline_rows", "100", type=int) or 100

        # Parse optional fixed parameters
        params_json_raw = request.form.get("parameters_json", "").strip()
        parameters = None
        if params_json_raw:
            try:
                parameters = json.loads(params_json_raw)
            except json.JSONDecodeError:
                flash("Invalid JSON in parameters field.", "error")
                return render_template(
                    "schedules/edit.html",
                    report=report,
                    schedule=None,
                    form=request.form,
                )

        schedule = Schedule.create(
            report_id=report.id,
            name=name,
            schedule_definition=definition,
            recipients=recipients,
            created_by=g.user.username,
            parameters=parameters,
            max_inline_rows=max_inline_rows,
        )

        flash(f"Schedule '{schedule.name}' created.", "success")
        return redirect(url_for("schedules.index", uuid=report.uuid))

    return render_template(
        "schedules/edit.html",
        report=report,
        schedule=None,
        form={},
    )


@bp.route("/schedules/<schedule_uuid>/edit", methods=["GET", "POST"])
@login_required
def edit(schedule_uuid: str) -> str | Response:
    """Edit a schedule."""
    schedule = Schedule.get_by_uuid(schedule_uuid)
    if not schedule:
        abort(404)

    report = Report.get_by_id(schedule.report_id)
    if not report:
        abort(404)

    if request.method == "POST":
        definition = _parse_schedule_form(request.form)
        if definition is None:
            flash("Invalid schedule definition.", "error")
            return render_template(
                "schedules/edit.html",
                report=report,
                schedule=schedule,
                form=request.form,
            )

        name = request.form.get("name", "").strip()
        if not name:
            flash("Schedule name is required.", "error")
            return render_template(
                "schedules/edit.html",
                report=report,
                schedule=schedule,
                form=request.form,
            )

        recipients_raw = request.form.get("recipients", "").strip()
        recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]
        if not recipients:
            flash("At least one recipient email is required.", "error")
            return render_template(
                "schedules/edit.html",
                report=report,
                schedule=schedule,
                form=request.form,
            )

        max_inline_rows = request.form.get("max_inline_rows", "100", type=int) or 100

        params_json_raw = request.form.get("parameters_json", "").strip()
        parameters: dict | None = ...  # type: ignore[assignment]
        if params_json_raw:
            try:
                parameters = json.loads(params_json_raw)
            except json.JSONDecodeError:
                flash("Invalid JSON in parameters field.", "error")
                return render_template(
                    "schedules/edit.html",
                    report=report,
                    schedule=schedule,
                    form=request.form,
                )
        else:
            parameters = None

        enabled = request.form.get("enabled") == "on"

        schedule.update(
            name=name,
            schedule_definition=definition,
            recipients=recipients,
            parameters=parameters,
            max_inline_rows=max_inline_rows,
            enabled=enabled,
        )

        flash(f"Schedule '{schedule.name}' updated.", "success")
        return redirect(url_for("schedules.index", uuid=report.uuid))

    # Build form dict from existing schedule for template
    form = _schedule_to_form(schedule)
    return render_template(
        "schedules/edit.html",
        report=report,
        schedule=schedule,
        form=form,
    )


@bp.route("/schedules/<schedule_uuid>/delete", methods=["POST"])
@login_required
def delete(schedule_uuid: str) -> Response:
    """Delete a schedule."""
    schedule = Schedule.get_by_uuid(schedule_uuid)
    if not schedule:
        abort(404)

    report = Report.get_by_id(schedule.report_id)
    schedule.delete()
    flash(f"Schedule '{schedule.name}' deleted.", "success")

    if report:
        return redirect(url_for("schedules.index", uuid=report.uuid))
    return redirect(url_for("reports.index"))


@bp.route("/schedules/<schedule_uuid>/preview")
@login_required
def preview(schedule_uuid: str) -> str:
    """HTMX endpoint: preview next N run times."""
    schedule = Schedule.get_by_uuid(schedule_uuid)
    if not schedule:
        abort(404)

    runs = next_n_runs(schedule.schedule_definition, n=5)
    return render_template("schedules/_preview.html", runs=runs)


@bp.route("/schedules/preview", methods=["POST"])
@login_required
def preview_form() -> str:
    """HTMX endpoint: preview next N run times from form data."""
    definition = _parse_schedule_form(request.form)
    if definition is None:
        return "<p>Invalid schedule definition.</p>"

    runs = next_n_runs(definition, n=5)
    return render_template("schedules/_preview.html", runs=runs)


def _parse_schedule_form(form: dict) -> dict | None:  # type: ignore[type-arg]
    """Parse schedule definition from form data."""
    schedule_type = form.get("schedule_type", "")

    try:
        match schedule_type:
            case "interval":
                every = int(form.get("interval_every", "1"))
                unit = form.get("interval_unit", "hours")
                if unit not in ("minutes", "hours", "days"):
                    return None
                definition: dict = {"type": "interval", "every": every, "unit": unit}
                if unit == "days" and form.get("interval_at"):
                    definition["at"] = form["interval_at"]
                return definition

            case "daily":
                at = form.get("daily_at", "08:00")
                if not at:
                    return None
                return {"type": "daily", "at": at}

            case "weekly":
                days = form.getlist("weekly_days") if hasattr(form, "getlist") else []
                if not days:
                    return None
                at = form.get("weekly_at", "08:00")
                return {"type": "weekly", "days": days, "at": at}

            case "monthly_day":
                day = int(form.get("monthly_day_day", "1"))
                at = form.get("monthly_day_at", "08:00")
                return {"type": "monthly_day", "day": day, "at": at}

            case "monthly_pattern":
                pattern = form.get("monthly_pattern_type", "first_working_day")
                at = form.get("monthly_pattern_at", "08:00")
                return {"type": "monthly_pattern", "pattern": pattern, "at": at}

            case "one_time":
                dt = form.get("one_time_datetime", "")
                if not dt:
                    return None
                return {"type": "one_time", "datetime": dt}

            case _:
                return None
    except (ValueError, TypeError):
        return None


def _schedule_to_form(schedule: Schedule) -> dict[str, str]:
    """Convert a schedule's definition back to form field values."""
    defn = schedule.schedule_definition
    form: dict[str, str] = {
        "name": schedule.name,
        "recipients": ", ".join(schedule.get_recipients()),
        "max_inline_rows": str(schedule.max_inline_rows),
        "schedule_type": defn.get("type", ""),
        "enabled": "on" if schedule.enabled else "",
    }

    if schedule.parameters_json:
        form["parameters_json"] = schedule.parameters_json

    match defn.get("type"):
        case "interval":
            form["interval_every"] = str(defn.get("every", 1))
            form["interval_unit"] = defn.get("unit", "hours")
            form["interval_at"] = defn.get("at", "")
        case "daily":
            at = defn.get("at", "08:00")
            form["daily_at"] = at if isinstance(at, str) else at[0] if at else "08:00"
        case "weekly":
            form["weekly_at"] = defn.get("at", "08:00")
            # weekly_days handled separately in template
        case "monthly_day":
            form["monthly_day_day"] = str(defn.get("day", 1))
            form["monthly_day_at"] = defn.get("at", "08:00")
        case "monthly_pattern":
            form["monthly_pattern_type"] = defn.get("pattern", "first_working_day")
            form["monthly_pattern_at"] = defn.get("at", "08:00")
        case "one_time":
            form["one_time_datetime"] = defn.get("datetime", "")

    return form
