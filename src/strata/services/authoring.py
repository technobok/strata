"""The rules for saving what authors write, shared by the web forms and
``strata-admin import`` (services/definitions.py): access and alert lists, report
arguments, reports and checks, dependencies, connection bindings and schedules.

Each takes plain values (the forms read ``request.form``, import reads a file) and
says why it refuses, so both front ends refuse the same things in the same words.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from strata.models.connection import Connection
from strata.models.datamodule import Datamodule
from strata.models.dependency import Dependency
from strata.models.report import Report
from strata.models.report_arg import DATA_TYPES, WIDGETS, ReportArg
from strata.models.report_check import SEVERITIES, ReportCheck
from strata.models.schedule import Schedule
from strata.services import access, datamodule_service, duck, report_service, schedule_service
from strata.services.query_filter import RESERVED_KEYS
from strata.services.schedule_service import Definition, ScheduleError
from strata.services.template_service import ALIAS_RE


class AuthoringError(ValueError):
    """A save is refused; the message is shown to the author as it is."""


# ---------------------------------------------------------------------------
# Lists of people
# ---------------------------------------------------------------------------


def access_problem(owners: list[str], viewers: list[str]) -> str | None:
    """Why a datamodule's owners and viewers cannot be saved. Both lists name someone:
    neither is ever blank."""
    if not owners:
        return "Access: Owners must name at least one person or @group."
    if not viewers:
        return (
            "Access: Viewers must name at least one person or @group, or * for everyone "
            "signed in (owners view too: name them for an owners-only datamodule)."
        )
    named = owners + viewers
    problem = access.check_principals(named)
    if problem:
        return f"Access: {problem}"
    if access.EVERYONE in owners:
        return "Access: owners are people or groups; * (everyone signed in) can only view."
    try:
        unknown = access.Directory().unknown(named)
    except access.DirectoryUnavailable as exc:
        return f"Access: the names could not be checked: {exc}."
    if unknown:
        return f"Access: Gatekeeper has no user or group {', '.join(unknown)}."
    return None


def alert_list_problem(recipients: list[str]) -> str | None:
    """Why a datamodule's alert list cannot be saved. It names someone: never blank."""
    from strata.services import notify_service

    if not recipients:
        return (
            f"Alert list: name at least one person, @group or email address (the Strata "
            f"admins are @{access.admin_group()})."
        )
    problem = notify_service.check_recipients(recipients) or notify_service.unknown_recipients(
        recipients
    )
    return f"Alert list: {problem}" if problem else None


def inputs_problem(dm: Datamodule, owners: list[str]) -> str | None:
    """Why these owners may not own ``dm``: one of them may not use a bound connection."""
    try:
        problems = access.input_problems(dm, owners, access.Directory())
    except access.DirectoryUnavailable as exc:
        return f"Access: the owners could not be checked: {exc}."
    return f"Access: {'; '.join(problems)}." if problems else None


# ---------------------------------------------------------------------------
# Reports and checks
# ---------------------------------------------------------------------------


def arg_problem(name: str, data_type: str, widget: str) -> str | None:
    """Why a report argument cannot be saved. Its name becomes a macro parameter."""
    if not ALIAS_RE.match(name):
        return f"Argument name '{name}' must be a letter/underscore identifier."
    if name in RESERVED_KEYS:
        return (
            f"Argument name '{name}' is reserved for sorting/filtering "
            "(sort, page, q, format, key, version)."
        )
    if data_type not in DATA_TYPES:
        return f"Unknown argument type '{data_type}'."
    if widget not in WIDGETS:
        return f"Unknown widget '{widget}'."
    return None


@dataclass
class ReportFields:
    """A report as an author gives it. ``args`` are ReportArg.replace_all dicts;
    ``severity`` None = a plain report, else a check."""

    name: str
    description: str
    body_sql: str
    args: list[dict[str, Any]] = field(default_factory=list)
    severity: str | None = None
    gate: bool = False


def _live_version_file(dm: Datamodule) -> Path | None:
    """The file of ``dm``'s live version, if it has one on disk."""
    live = datamodule_service.live_version(dm)
    if live is None:
        return None
    path = datamodule_service.version_path(dm.uuid, live.uuid)
    return path if path.exists() else None


def save_report(
    dm: Datamodule,
    report: Report | None,
    fields: ReportFields,
    actor: str,
    macro_name: str | None = None,
    check_live: bool = True,
) -> Report:
    """Create (``report`` None) or update a report of ``dm``; raise AuthoringError.

    A new report's macro name is ``macro_name`` when given (it must be one
    report_service.macro_name_for would make), else made from its name; either way it
    is kept from then on, renames included: workbooks select its xlsx sheet and table
    by it. With ``check_live`` it must run on the live version (when there is one)."""
    name = fields.name.strip()
    if not name:
        raise AuthoringError("Report name is required.")
    others = [r for r in Report.get_for_datamodule(dm.id) if report is None or r.id != report.id]
    if any(r.name.lower() == name.lower() for r in others):
        raise AuthoringError(f"{dm.path} already has a report named '{name}': choose another name.")
    taken = [r.macro_name for r in others]
    if report is not None:
        if macro_name is not None and macro_name != report.macro_name:
            raise AuthoringError(
                f"Report '{name}' keeps its macro name '{report.macro_name}': a workbook "
                f"selects its sheet and table by it ('{macro_name}' was given)."
            )
        macro = report.macro_name
    elif macro_name is not None:
        if report_service.macro_name_for(macro_name, taken) != macro_name:
            raise AuthoringError(
                f"Macro name '{macro_name}': lowercase ASCII letters, digits and _, at most "
                f"{report_service.MACRO_MAX} characters, not a DuckDB function or keyword, "
                "not another report's."
            )
        macro = macro_name
    else:
        macro = report_service.macro_name_for(name, taken)

    for arg in fields.args:
        problem = arg_problem(arg["name"], arg["data_type"], arg["widget"])
        if problem:
            raise AuthoringError(problem)
    is_check = fields.severity is not None
    gate = is_check and fields.gate
    if is_check and fields.severity not in SEVERITIES:
        raise AuthoringError(f"Unknown severity '{fields.severity}'.")
    if gate:
        for arg in fields.args:
            if arg["required"] and not arg["default_value"]:
                raise AuthoringError(
                    f"A gate check runs inside each build with its arguments' defaults: "
                    f"give '{arg['name']}' a default, make it optional, or untick Gate."
                )
    try:
        duck.validate_select(fields.body_sql, "The report body")
        for arg in fields.args:
            if arg["constraint_query"]:
                duck.validate_select(
                    arg["constraint_query"], f"The constraint query for '{arg['name']}'"
                )
    except duck.SqlShapeError as exc:
        raise AuthoringError(str(exc)) from exc

    # It must run on the data it will read (when there is any yet): DuckDB
    # describes it without reading rows (report_service.problem_on).
    live = _live_version_file(dm) if check_live else None
    if live is not None:
        problem = report_service.problem_on(
            live,
            macro,
            fields.body_sql,
            [
                report_service.ArgShape(
                    name=a["name"],
                    data_type=a["data_type"],
                    widget=a["widget"],
                    default_value=a["default_value"] or None,
                    required=a["required"],
                    constraint_query=a["constraint_query"],
                )
                for a in fields.args
            ],
        )
        if problem is not None:
            raise AuthoringError(
                f"Not saved: the report does not run on the data of {dm.path}.\n{problem}"
            )

    existing = Report.get_by_uuid(report.uuid) if report else None
    try:
        if existing is None:
            saved = Report.create(
                datamodule_id=dm.id,
                name=name,
                macro_name=macro,
                body_sql=fields.body_sql,
                created_by=actor,
                description=fields.description.strip(),
            )
        else:
            existing.update(
                modified_by=actor,
                name=name,
                description=fields.description.strip(),
                body_sql=fields.body_sql,
            )
            saved = existing
    except Exception as exc:  # UNIQUE(datamodule_id, macro_name|name) etc.
        raise AuthoringError(f"Could not save report: {exc}") from exc

    ReportArg.replace_all(saved.id, fields.args)
    if check_live:
        Report.set_problem(saved.id, None)  # it runs on the live data (or there is none yet)
    if is_check:
        assert fields.severity is not None
        ReportCheck.save(saved.id, fields.severity, gate)
    else:
        ReportCheck.clear(saved.id)
    return saved


# ---------------------------------------------------------------------------
# Build inputs
# ---------------------------------------------------------------------------


def dependency_problem(dm: Datamodule, parent: Datamodule, local_name: str) -> str | None:
    """Why ``dm`` may not depend on ``parent`` as ``local_name``. (Whether the person
    adding it may see ``parent`` is the form's question.)"""
    if not local_name or not ALIAS_RE.match(local_name):
        return "Local name must be a letter/underscore identifier."
    if parent.id == dm.id:
        return "A datamodule cannot depend on itself."
    if Dependency.reaches(parent.id, dm.id):
        return f"Adding this dependency would create a cycle via '{parent.path}'."
    try:
        problem = access.parent_problem(
            access.owners_of(dm), parent, local_name, access.Directory()
        )
    except access.DirectoryUnavailable as exc:
        problem = f"access could not be checked: {exc}"
    return f"Not added: {problem}." if problem else None


def connection_problem(dm: Datamodule, crow: Connection, local_name: str) -> str | None:
    """Why ``dm`` may not bind ``crow`` as ``local_name``: every owner must be on the
    connection's "May use" list. (Whether the person binding it may use it is the
    form's question.)"""
    if not local_name or not ALIAS_RE.match(local_name):
        return "Local name must be a letter/underscore identifier."
    try:
        missing = access.Directory().missing(access.owners_of(dm), crow.users or [])
    except access.DirectoryUnavailable as exc:
        why = f"access could not be checked: {exc}"
    else:
        if not missing:
            return None
        names = ", ".join(access.describe(p) for p in missing)
        why = f"{names} (owners of this datamodule) may not use the connection '{crow.name}'"
    return f'Not bound: {why}. An admin can add them to the connection\'s "May use" list.'


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------


@dataclass
class ScheduleFields:
    """A schedule as an author gives it: a builder ``kind`` and its form ``fields``
    (schedule_service.build), and for a report schedule who gets it and the arguments."""

    name: str
    kind: str
    fields: dict[str, Any]
    timezone: str
    enabled: bool = True
    recipients: list[str] = field(default_factory=list)
    params: dict[str, list[str]] = field(default_factory=dict)


def next_run_iso(definition: Definition) -> str | None:
    nxt = schedule_service.next_run(definition, datetime.now(UTC))
    return nxt.isoformat() if nxt else None


def report_schedule_problem(
    recipients: list[str], args: Iterable[Any], params: dict[str, list[str]]
) -> str | None:
    """Why a report schedule's recipients or arguments cannot be saved."""
    from strata.services import notify_service

    problem = notify_service.check_recipients(recipients) or notify_service.unknown_recipients(
        recipients
    )
    if problem:
        return problem
    try:
        report_service.check_args(list(args), params)
    except report_service.ArgError as exc:
        return f"{exc}: set it on this schedule, or give the argument a default."
    return None


def save_schedule(
    dm: Datamodule,
    schedule: Schedule | None,
    report: Report | None,
    fields: ScheduleFields,
    actor: str,
) -> Schedule:
    """Create or update a rebuild schedule, or (``report``) a report schedule; raise
    ScheduleError."""
    if not fields.name:
        raise ScheduleError("Name is required.")
    definition = schedule_service.build(fields.kind, fields.fields, fields.timezone)
    recipients = fields.recipients if report else []
    params = fields.params if report else {}
    if report is not None:
        problem = report_schedule_problem(recipients, ReportArg.get_for_report(report.id), params)
        if problem:
            raise ScheduleError(problem)
    next_run = next_run_iso(definition) if fields.enabled else None
    if schedule is None:
        return Schedule.create(
            dm.id,
            fields.name,
            definition.to_json(),
            next_run,
            actor,
            enabled=fields.enabled,
            report_id=report.id if report else None,
            recipients=recipients,
            parameters=params,
        )
    schedule.update(
        fields.name,
        definition.to_json(),
        next_run,
        fields.enabled,
        recipients=recipients if report else None,
        parameters=params if report else None,
    )
    updated = Schedule.get_by_id(schedule.id)
    assert updated is not None
    return updated
