"""Definition files: one TOML file describes one datamodule, with its reports,
checks and schedules (``strata-admin import`` / ``strata-admin export``).

Import matches what exists by name: a datamodule by path, a report by name
(ignoring case) within its datamodule, a schedule by name within its report (or,
for a rebuild schedule, its datamodule). What a file lists is set to the file's
values; what it does not list is left alone, unless ``prune`` (then reports,
schedules, dependencies and connection bindings it does not list are removed).
Every save goes through services/authoring, the rules the web forms use.

A file is all or nothing: it is applied in one transaction, and a refusal rolls it
back and names the reason; the next file is still tried. A dry run applies each
file the same way and rolls it back, so it refuses exactly what a real run would.
One difference: in a dry run a dependency on a datamodule an earlier file of the
same run creates is reported, not bound (that datamodule was rolled back).

Schedule recipients Gatekeeper does not know are dropped with a note (a person who
left must not stop a check); unknown owners, viewers or alert-list names refuse
the file, since dropping one would change who may read or hears about it.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from strata.models.connection import Connection
from strata.models.datamodule import Datamodule
from strata.models.datamodule_connection import DatamoduleConnection
from strata.models.dependency import Dependency
from strata.models.report import Report
from strata.models.report_arg import ReportArg
from strata.models.report_check import ReportCheck
from strata.models.schedule import Schedule
from strata.models.tag import Tag
from strata.services import access, authoring, schedule_service
from strata.services.authoring import AuthoringError
from strata.services.schedule_service import Definition, ScheduleError

NEVER = "never"  # ttl_seconds: never expire
KEEP_ALL = "all"  # retention_age_seconds: keep every version
OFF = "off"  # stale_alert_hours: no stale alert


class DefinitionError(ValueError):
    """A definition file is not the shape import reads."""


# ---------------------------------------------------------------------------
# The shape of a file
# ---------------------------------------------------------------------------


@dataclass
class ArgDef:
    name: str
    data_type: str = "text"
    widget: str = "input"
    required: bool = True
    default: str = ""
    constraint: str = ""
    description: str = ""

    def as_row(self) -> dict[str, Any]:
        """The ReportArg.replace_all shape."""
        return {
            "name": self.name,
            "data_type": self.data_type,
            "widget": self.widget,
            "default_value": self.default,
            "required": self.required,
            "constraint_query": self.constraint or None,
            "description": self.description,
        }


@dataclass
class ScheduleDef:
    name: str
    kind: str
    fields: dict[str, Any] = field(default_factory=dict)
    timezone: str | None = None
    enabled: bool = True
    recipients: list[str] = field(default_factory=list)
    parameters: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class ReportDef:
    name: str
    body: str
    macro: str | None = None
    description: str = ""
    check: str | None = None  # severity; None = a plain report
    gate: bool = False
    args: list[ArgDef] = field(default_factory=list)
    schedules: list[ScheduleDef] = field(default_factory=list)


@dataclass
class DatamoduleDef:
    """A datamodule as a file gives it. None = the file leaves it out: a new
    datamodule gets Strata's default, an existing one keeps what it has."""

    path: str
    source: str = ""
    description: str | None = None
    script: str | None = None
    tags: list[str] | None = None
    ttl_seconds: int | str | None = None  # seconds or NEVER
    retention_kind: str | None = None
    retention_count: int | None = None
    retention_age_seconds: int | str | None = None  # seconds or KEEP_ALL
    build_queue: str | None = None
    owners: list[str] | None = None
    viewers: list[str] | None = None
    alerts: list[str] | None = None
    stale_alert_hours: float | str | None = None  # hours or OFF
    connections: dict[str, str] | None = None  # local name -> connection name
    dependencies: dict[str, str] | None = None  # local name -> datamodule path
    reports: list[ReportDef] = field(default_factory=list)
    schedules: list[ScheduleDef] = field(default_factory=list)  # rebuild schedules


class _Table:
    """One TOML table being read: typed getters, and every key must be known."""

    def __init__(self, data: dict[str, Any], where: str) -> None:
        self.data = data
        self.where = where
        self.seen: set[str] = set()

    def _get(self, key: str, required: bool) -> Any:
        self.seen.add(key)
        if key not in self.data:
            if required:
                raise DefinitionError(f"{self.where}: '{key}' is required.")
            return None
        return self.data[key]

    def text(self, key: str, required: bool = False) -> str | None:
        value = self._get(key, required)
        if value is not None and not isinstance(value, str):
            raise DefinitionError(f"{self.where}: '{key}' must be text.")
        return value

    def flag(self, key: str, default: bool) -> bool:
        value = self._get(key, False)
        if value is None:
            return default
        if not isinstance(value, bool):
            raise DefinitionError(f"{self.where}: '{key}' must be true or false.")
        return value

    def whole(self, key: str, word: str | None = None) -> int | str | None:
        """A whole number >= 0, or ``word`` (e.g. "never")."""
        value = self._get(key, False)
        if value is None or (word is not None and value == word):
            return value
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            also = f' or "{word}"' if word else ""
            raise DefinitionError(f"{self.where}: '{key}' must be a whole number{also}.")
        return value

    def texts(self, key: str) -> list[str] | None:
        value = self._get(key, False)
        if value is None:
            return None
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise DefinitionError(f"{self.where}: '{key}' must be a list of text.")
        return [v.strip() for v in value if v.strip()]

    def names(self, key: str) -> dict[str, str] | None:
        value = self._get(key, False)
        if value is None:
            return None
        if not isinstance(value, dict) or not all(isinstance(v, str) for v in value.values()):
            raise DefinitionError(f"{self.where}: [{key}] must map local names to text.")
        return {str(k): v for k, v in value.items()}

    def tables(self, key: str) -> list[_Table]:
        value = self._get(key, False)
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
            raise DefinitionError(f"{self.where}: '{key}' must be [[{key}]] tables.")
        return [_Table(v, f"{self.where}, {key} {i + 1}") for i, v in enumerate(value)]

    def table(self, key: str) -> dict[str, Any]:
        value = self._get(key, False)
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise DefinitionError(f"{self.where}: '{key}' must be a table.")
        return value

    def done(self) -> None:
        unknown = sorted(set(self.data) - self.seen)
        if unknown:
            raise DefinitionError(f"{self.where}: unknown key(s) {', '.join(unknown)}.")


def _schedule(t: _Table, report: bool) -> ScheduleDef:
    name = t.text("name", required=True) or ""
    t.where = f"{t.where} ('{name}')"
    params: dict[str, list[str]] = {}
    if report:
        for arg, value in t.table("parameters").items():
            values = value if isinstance(value, list) else [value]
            if not all(isinstance(v, str) for v in values):
                raise DefinitionError(f"{t.where}: parameter '{arg}' must be text.")
            params[str(arg)] = list(values)
    sched = ScheduleDef(
        name=name,
        kind=t.text("kind", required=True) or "",
        fields=t.table("fields"),
        timezone=t.text("timezone"),
        enabled=t.flag("enabled", True),
        recipients=(t.texts("recipients") or []) if report else [],
        parameters=params,
    )
    t.done()
    return sched


def parse(text: str, source: str = "") -> DatamoduleDef:
    """Read a definition file; raise DefinitionError naming what is wrong."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise DefinitionError(f"{source}: not a TOML file: {exc}") from exc
    top = _Table(data, source or "the file")
    path = (top.text("path", required=True) or "").strip()
    if not path:
        raise DefinitionError(f"{top.where}: 'path' is required.")
    stale = top._get("stale_alert_hours", False)
    if stale is not None and stale != OFF:
        if isinstance(stale, bool) or not isinstance(stale, int | float) or stale <= 0:
            raise DefinitionError(f"{top.where}: 'stale_alert_hours' must be hours or \"{OFF}\".")
    defn = DatamoduleDef(
        path=path,
        source=source,
        description=top.text("description"),
        script=top.text("script"),
        tags=top.texts("tags"),
        ttl_seconds=top.whole("ttl_seconds", NEVER),
        retention_kind=top.text("retention_kind"),
        retention_count=_count(top),
        retention_age_seconds=top.whole("retention_age_seconds", KEEP_ALL),
        build_queue=top.text("build_queue"),
        owners=top.texts("owners"),
        viewers=top.texts("viewers"),
        alerts=top.texts("alerts"),
        stale_alert_hours=stale,
        connections=top.names("connections"),
        dependencies=top.names("dependencies"),
        schedules=[_schedule(t, report=False) for t in top.tables("schedule")],
    )
    for t in top.tables("report"):
        name = t.text("name", required=True) or ""
        t.where = f"{top.where}, report '{name}'"
        args: list[ArgDef] = []
        for a in t.tables("arg"):
            args.append(
                ArgDef(
                    name=(a.text("name", required=True) or "").strip(),
                    data_type=a.text("type") or "text",
                    widget=a.text("widget") or "input",
                    required=a.flag("required", True),
                    default=a.text("default") or "",
                    constraint=a.text("constraint") or "",
                    description=a.text("description") or "",
                )
            )
            a.done()
        defn.reports.append(
            ReportDef(
                name=name,
                body=t.text("body", required=True) or "",
                macro=t.text("macro"),
                description=t.text("description") or "",
                check=t.text("check"),
                gate=t.flag("gate", False),
                args=args,
                schedules=[_schedule(s, report=True) for s in t.tables("schedule")],
            )
        )
        t.done()
    top.done()
    return defn


def _count(top: _Table) -> int | None:
    value = top.whole("retention_count")
    if isinstance(value, int) and value < 1:
        raise DefinitionError(f"{top.where}: 'retention_count' must be at least 1.")
    return value if isinstance(value, int) else None


def read_files(paths: Iterable[Path]) -> tuple[list[DatamoduleDef], list[tuple[str, str]]]:
    """Every ``*.toml`` under ``paths`` (files or directories), parsed; and the
    (file, reason) of each that could not be read."""
    files: list[Path] = []
    for p in paths:
        files.extend(sorted(p.rglob("*.toml")) if p.is_dir() else [p])
    defs: list[DatamoduleDef] = []
    bad: list[tuple[str, str]] = []
    for f in files:
        try:
            defs.append(parse(f.read_text(encoding="utf-8"), str(f)))
        except (OSError, UnicodeDecodeError, DefinitionError) as exc:
            bad.append((str(f), str(exc)))
    return defs, bad


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


@dataclass
class Outcome:
    """What importing one file did (or, in a dry run, would do)."""

    path: str
    source: str
    action: str = "unchanged"  # create | update | unchanged | refused
    changes: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    refused: str | None = None
    datamodule_uuid: str | None = None


class _RollBack(Exception):
    """Ends a dry run's transaction (its writes are undone)."""


def _same_text(a: str, b: str) -> bool:
    """Equal but for line endings (a browser posts a textarea with CRLF)."""
    return a.replace("\r\n", "\n") == b.replace("\r\n", "\n")


def _ordered(defs: list[DatamoduleDef]) -> list[DatamoduleDef]:
    """Files whose datamodules others in the run depend on come first."""
    by_path = {d.path: d for d in defs}
    done: list[DatamoduleDef] = []
    seen: set[str] = set()

    def visit(d: DatamoduleDef, trail: tuple[str, ...]) -> None:
        if d.path in seen or d.path in trail:
            return  # a cycle is refused when its dependency is bound
        for parent in (d.dependencies or {}).values():
            if parent in by_path:
                visit(by_path[parent], (*trail, d.path))
        seen.add(d.path)
        done.append(d)

    for d in defs:
        visit(d, ())
    return done


def import_definitions(
    defs: list[DatamoduleDef], actor: str, dry_run: bool = False, prune: bool = False
) -> list[Outcome]:
    """Apply each definition in its own transaction (dependencies first)."""
    from strata.db import transaction

    outcomes: list[Outcome] = []
    created_in_run: set[str] = set()
    for defn in _ordered(defs):
        out = Outcome(defn.path, defn.source)
        try:
            with transaction():
                _apply(defn, actor, prune, out, created_in_run if dry_run else set())
                if dry_run:
                    raise _RollBack
        except _RollBack:
            pass
        except (AuthoringError, ScheduleError, DefinitionError) as exc:
            out.action, out.refused = "refused", str(exc)
            out.changes, out.notes = [], []
        except Exception as exc:  # noqa: BLE001 - one file's surprise must not stop the run
            out.action, out.refused = "refused", f"{type(exc).__name__}: {exc}"
            out.changes, out.notes = [], []
        if out.action == "create":
            created_in_run.add(defn.path)
        outcomes.append(out)
    return outcomes


def _apply(
    defn: DatamoduleDef, actor: str, prune: bool, out: Outcome, created_in_run: set[str]
) -> None:
    dm = Datamodule.get_by_path(defn.path)
    script_changed = False
    if dm is None:
        dm = _create(defn, actor, out)
    else:
        script_changed = _update(defn, dm, actor, out)
    out.datamodule_uuid = dm.uuid

    if defn.tags is not None:
        ids = authoring.tag_ids(defn.tags, create=True)
        if sorted(ids) != sorted(t.id for t in Tag.for_datamodule(dm.id)):
            Tag.set_for_datamodule(dm.id, ids)
            out.changes.append("tags")

    _bind_connections(defn, dm, prune, out)
    _bind_dependencies(defn, dm, prune, out, created_in_run)
    if defn.owners is not None and (problem := authoring.inputs_problem(dm, defn.owners)):
        raise AuthoringError(problem)

    _apply_schedules(dm, None, defn.schedules, actor, prune, out)
    _apply_reports(defn, dm, actor, prune, out, check_live=not script_changed)
    if out.action == "unchanged" and out.changes:
        out.action = "update"


def _lists(defn: DatamoduleDef) -> None:
    """Refuse owners, viewers or an alert list the forms would refuse."""
    if defn.owners is not None or defn.viewers is not None:
        owners = defn.owners if defn.owners is not None else access.default_owners()
        viewers = defn.viewers if defn.viewers is not None else access.default_viewers()
        if problem := authoring.access_problem(owners, viewers):
            raise AuthoringError(problem)
    if defn.alerts is not None and (problem := authoring.alert_list_problem(defn.alerts)):
        raise AuthoringError(problem)
    if defn.retention_kind is not None and defn.retention_kind not in ("count", "age"):
        raise AuthoringError("Retention policy must be 'count' or 'age'.")
    if defn.build_queue is not None and not re.fullmatch(r"[a-z0-9_-]{1,32}", defn.build_queue):
        raise AuthoringError("Worker queue: lowercase letters, digits, '-' and '_' only.")


def _ttl(value: int | str | None) -> int | None:
    return None if value == NEVER else int(value) if value is not None else None


def _stale_seconds(value: float | str | None) -> int | None:
    if value is None or value == OFF:
        return None
    return round(float(value) * 3600)


def _create(defn: DatamoduleDef, actor: str, out: Outcome) -> Datamodule:
    from strata.models.api_key import ApiKey

    _lists(defn)
    dm = Datamodule.create(
        path=defn.path,
        created_by=actor,
        description=defn.description or "",
        build_script=defn.script or "",
        ttl_seconds=_ttl(defn.ttl_seconds),
        owners=defn.owners if defn.owners is not None else access.form_owners(None),
        viewers=defn.viewers if defn.viewers is not None else access.form_viewers(None),
    )
    # As the form does: its Excel link from the start, and an alert list.
    ApiKey.ensure_excel_link(dm.id, actor)
    dm.set_alerts(
        defn.alerts if defn.alerts is not None else [f"@{access.admin_group()}"],
        _stale_seconds(defn.stale_alert_hours),
    )
    if (
        defn.retention_kind is not None
        or defn.retention_count is not None
        or defn.retention_age_seconds is not None
        or defn.build_queue is not None
    ):
        dm.update(
            modified_by=actor,
            retention_kind=defn.retention_kind,
            retention_count=defn.retention_count,
            retention_age_seconds=_age(defn.retention_age_seconds, Datamodule._UNSET),
            build_queue=defn.build_queue,
        )
    out.action = "create"
    created = Datamodule.get_by_id(dm.id)
    assert created is not None
    return created


def _age(value: int | str | None, unset: object) -> object:
    if value is None:
        return unset
    return None if value == KEEP_ALL else int(value)


def _update(defn: DatamoduleDef, dm: Datamodule, actor: str, out: Outcome) -> bool:
    """Set the datamodule's own fields; True when its build script changed."""
    _lists(defn)
    changes: dict[str, Any] = {}
    if defn.description is not None and defn.description.strip() != dm.description:
        changes["description"] = defn.description
    script_changed = defn.script is not None and not _same_text(defn.script, dm.build_script)
    if script_changed:
        changes["build_script"] = defn.script
    if defn.ttl_seconds is not None and _ttl(defn.ttl_seconds) != dm.ttl_seconds:
        changes["ttl_seconds"] = _ttl(defn.ttl_seconds)
    if defn.retention_kind is not None and defn.retention_kind != dm.retention_kind:
        changes["retention_kind"] = defn.retention_kind
    if defn.retention_count is not None and defn.retention_count != dm.retention_count:
        changes["retention_count"] = defn.retention_count
    age = _age(defn.retention_age_seconds, Datamodule._UNSET)
    if age is not Datamodule._UNSET and age != dm.retention_age_seconds:
        changes["retention_age_seconds"] = age
    if defn.build_queue is not None and defn.build_queue != dm.build_queue:
        changes["build_queue"] = defn.build_queue
    if changes:
        dm.update(modified_by=actor, **changes)
        out.changes.extend("script" if k == "build_script" else k for k in changes)
    if script_changed and dm.live_version_id is not None:
        dm.mark_stale()  # as the form does: the next read or run rebuilds it
        out.notes.append("its script changed: its data is stale until the next build")

    owners = defn.owners if defn.owners is not None else dm.owners
    viewers = defn.viewers if defn.viewers is not None else dm.viewers
    if (owners, viewers) != (dm.owners, dm.viewers):
        dm.set_access(owners, viewers, modified_by=actor)
        out.changes.append("access")
    alerts = defn.alerts if defn.alerts is not None else dm.alert_recipients
    stale = (
        _stale_seconds(defn.stale_alert_hours)
        if defn.stale_alert_hours is not None
        else dm.stale_alert_seconds
    )
    if (alerts, stale) != (dm.alert_recipients, dm.stale_alert_seconds):
        dm.set_alerts(alerts, stale)
        out.changes.append("alerts")
    return script_changed


def _bind_connections(defn: DatamoduleDef, dm: Datamodule, prune: bool, out: Outcome) -> None:
    if defn.connections is None:
        return
    bound = {link.local_name: link for link in DatamoduleConnection.get_for_datamodule(dm.id)}
    for local_name, name in defn.connections.items():
        crow = Connection.get_by_name(name)
        if crow is None:
            raise AuthoringError(f"Connection '{name}' (as '{local_name}'): there is none.")
        link = bound.get(local_name)
        if link is not None and link.connection_id == crow.id:
            continue
        if problem := authoring.connection_problem(dm, crow, local_name):
            raise AuthoringError(f"Connection '{name}' as '{local_name}': {problem}")
        if link is not None:
            link.delete()
        DatamoduleConnection.create(dm.id, crow.id, local_name)
        out.changes.append(f"connection {local_name}")
    if prune:
        for local_name, link in bound.items():
            if local_name not in defn.connections:
                link.delete()
                out.changes.append(f"connection {local_name} removed")


def _bind_dependencies(
    defn: DatamoduleDef, dm: Datamodule, prune: bool, out: Outcome, created_in_run: set[str]
) -> None:
    if defn.dependencies is None:
        return
    bound = {d.local_name: d for d in Dependency.get_for_datamodule(dm.id)}
    for local_name, path in defn.dependencies.items():
        parent = Datamodule.get_by_path(path)
        if parent is None:
            if path in created_in_run:
                out.notes.append(
                    f"dependency '{local_name}' on {path}: made by an earlier file of this run, "
                    "so not checked in a dry run"
                )
                continue
            raise AuthoringError(f"Dependency '{local_name}': there is no datamodule {path}.")
        dep = bound.get(local_name)
        if dep is not None and dep.depends_on_id == parent.id:
            continue
        if dep is not None:
            dep.delete()
        if problem := authoring.dependency_problem(dm, parent, local_name):
            raise AuthoringError(f"Dependency '{local_name}' on {path}: {problem}")
        Dependency.create(dm.id, parent.id, local_name)
        out.changes.append(f"dependency {local_name}")
    if prune:
        for local_name, dep in bound.items():
            if local_name not in defn.dependencies:
                dep.delete()
                out.changes.append(f"dependency {local_name} removed")


def _arg_rows(args: list[ReportArg]) -> list[dict[str, Any]]:
    return [
        {
            "name": a.name,
            "data_type": a.data_type,
            "widget": a.widget,
            "default_value": a.default_value or "",
            "required": a.required,
            "constraint_query": a.constraint_query or None,
            "description": a.description,
        }
        for a in args
    ]


def _apply_reports(
    defn: DatamoduleDef, dm: Datamodule, actor: str, prune: bool, out: Outcome, check_live: bool
) -> None:
    existing = {r.name.lower(): r for r in Report.get_for_datamodule(dm.id)}
    listed: set[str] = set()
    for rdef in defn.reports:
        key = rdef.name.strip().lower()
        if key in listed:
            raise AuthoringError(f"The file names the report '{rdef.name}' twice.")
        listed.add(key)
        report = existing.get(key)
        fields = authoring.ReportFields(
            name=rdef.name,
            description=rdef.description,
            body_sql=rdef.body,
            args=[a.as_row() for a in rdef.args],
            severity=rdef.check,
            gate=rdef.gate,
        )
        where = f"Report '{rdef.name}'"
        if report is None or _report_differs(report, rdef, fields):
            try:
                report = authoring.save_report(
                    dm, report, fields, actor, macro_name=rdef.macro, check_live=check_live
                )
            except AuthoringError as exc:
                raise AuthoringError(f"{where}: {exc}") from exc
            out.changes.append(f"report '{rdef.name}'")
        elif rdef.macro is not None and rdef.macro != report.macro_name:
            raise AuthoringError(
                f"{where} keeps its macro name '{report.macro_name}': a workbook selects its "
                f"sheet and table by it ('{rdef.macro}' was given)."
            )
        _apply_schedules(dm, report, rdef.schedules, actor, prune, out)
    if prune:
        for key, report in existing.items():
            if key not in listed:
                report.delete()  # its runs' files: strata-admin reconcile sweeps them
                out.changes.append(f"report '{report.name}' removed")


def _report_differs(report: Report, rdef: ReportDef, fields: authoring.ReportFields) -> bool:
    check = ReportCheck.get(report.id)
    return (
        report.name != rdef.name.strip()
        or report.description != rdef.description.strip()
        or not _same_text(report.body_sql, rdef.body)
        or _arg_rows(ReportArg.get_for_report(report.id)) != fields.args
        or (check.severity if check else None) != rdef.check
        or (bool(check and check.gate) != (rdef.check is not None and rdef.gate))
    )


def _norm_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Schedule builder fields as the form posts them: text, a list of days, no blanks."""
    normal: dict[str, Any] = {}
    for key, value in fields.items():
        if isinstance(value, list):
            if value:
                normal[key] = [str(v) for v in value]
        elif value is not None and str(value).strip() != "":
            normal[key] = str(value).strip()
    return normal


def _known_recipients(entries: list[str], where: str, out: Outcome) -> list[str]:
    """``entries`` without the usernames and @groups Gatekeeper does not know (noted)."""
    named = [e for e in entries if e.startswith("@") or "@" not in e]
    try:
        unknown = set(access.Directory().unknown(named))
    except access.DirectoryUnavailable as exc:
        raise AuthoringError(f"{where}: the recipients could not be checked: {exc}.") from exc
    if unknown:
        out.notes.append(
            f"{where}: left out recipients Gatekeeper does not know: {', '.join(sorted(unknown))}"
        )
    return [e for e in entries if e not in unknown]


def _apply_schedules(
    dm: Datamodule,
    report: Report | None,
    defs: list[ScheduleDef],
    actor: str,
    prune: bool,
    out: Outcome,
) -> None:
    owner = f"report '{report.name}'" if report else "the datamodule"
    current = (
        Schedule.for_report(report.id)
        if report
        else [s for s in Schedule.for_datamodule(dm.id) if s.report_id is None]
    )
    by_name = {s.name: s for s in current}
    listed: set[str] = set()
    for sdef in defs:
        where = f"Schedule '{sdef.name}' of {owner}"
        if sdef.name in listed:
            raise AuthoringError(f"{where}: named twice.")
        listed.add(sdef.name)
        recipients = _known_recipients(sdef.recipients, where, out) if report else []
        fields = authoring.ScheduleFields(
            name=sdef.name,
            kind=sdef.kind,
            fields=_norm_fields(sdef.fields),
            timezone=sdef.timezone or schedule_service.configured_timezones()[0],
            enabled=sdef.enabled,
            recipients=recipients,
            params=sdef.parameters,
        )
        schedule = by_name.get(sdef.name)
        if schedule is not None and not _schedule_differs(schedule, fields):
            continue
        try:
            authoring.save_schedule(dm, schedule, report, fields, actor)
        except ScheduleError as exc:
            raise AuthoringError(f"{where}: {exc}") from exc
        out.changes.append(f"schedule '{sdef.name}' of {owner}")
    if prune:
        for name, schedule in by_name.items():
            if name not in listed:
                schedule.delete()
                out.changes.append(f"schedule '{name}' of {owner} removed")


def _schedule_differs(schedule: Schedule, fields: authoring.ScheduleFields) -> bool:
    definition = Definition.from_json(schedule.schedule_definition)
    return (
        definition.kind != fields.kind
        or _norm_fields(definition.fields) != fields.fields
        or definition.timezone != fields.timezone
        or schedule.enabled != fields.enabled
        or schedule.recipients != fields.recipients
        or schedule.parameters != fields.params
    )


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


def _key(name: str) -> str:
    return name if _BARE_KEY.fullmatch(name) else _basic(name)


def _basic(text: str) -> str:
    """A TOML basic string (JSON's escapes are TOML's; DEL must be escaped too)."""
    return json.dumps(text, ensure_ascii=False).replace("\x7f", "\\u007F")


def _plain(text: str, allow: str) -> bool:
    return all(c >= " " and c != "\x7f" or c in allow for c in text)


def toml_string(text: str) -> str:
    """``text`` as TOML: a literal string when it needs no escapes (so SQL and
    ``asiap\\pawe`` read as written), a basic string otherwise."""
    if "\n" in text:
        if "'''" not in text and _plain(text, "\n\t"):
            return "'''\n" + text + "'''"
        return _basic(text)
    if "'" not in text and _plain(text, "\t"):
        return f"'{text}'"
    return _basic(text)


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return toml_string(value)
    if isinstance(value, list):
        return "[" + ", ".join(_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_value(v)}" for k, v in value.items()) + " }"
    raise TypeError(f"cannot write {value!r} as TOML")


def _schedule_lines(header: str, schedule: Schedule, report: bool) -> list[str]:
    definition = Definition.from_json(schedule.schedule_definition)
    lines = [
        "",
        f"[[{header}]]",
        f"name = {_value(schedule.name)}",
        f"kind = {_value(definition.kind)}",
        f"fields = {_value(_norm_fields(definition.fields))}",
        f"timezone = {_value(definition.timezone)}",
        f"enabled = {_value(schedule.enabled)}",
    ]
    if report:
        lines.append(f"recipients = {_value(schedule.recipients)}")
        if schedule.parameters:
            lines.append(f"parameters = {_value(schedule.parameters)}")
    return lines


def export_datamodule(dm: Datamodule) -> str:
    """``dm`` as a definition file that imports back unchanged."""
    out = [f"# {dm.path}: a Strata definition file (strata-admin export / import)"]
    out.append(f"path = {_value(dm.path)}")
    if dm.description:
        out.append(f"description = {_value(dm.description)}")
    tags = [t.name for t in Tag.for_datamodule(dm.id)]
    if tags:
        out.append(f"tags = {_value(tags)}")
    out.append(f"ttl_seconds = {_value(NEVER if dm.ttl_seconds is None else dm.ttl_seconds)}")
    out.append(f"retention_kind = {_value(dm.retention_kind)}")
    out.append(f"retention_count = {_value(dm.retention_count)}")
    age = dm.retention_age_seconds
    out.append(f"retention_age_seconds = {_value(KEEP_ALL if age is None else age)}")
    out.append(f"build_queue = {_value(dm.build_queue)}")
    out.append(f"owners = {_value(access.form_owners(dm))}")
    out.append(f"viewers = {_value(access.form_viewers(dm))}")
    from strata.services import alert_service

    out.append(f"alerts = {_value(alert_service.alert_list(dm))}")
    stale = dm.stale_alert_seconds
    out.append(f"stale_alert_hours = {_value(OFF if not stale else stale / 3600)}")
    out.append(f"script = {_value(dm.build_script.replace(chr(13) + chr(10), chr(10)))}")

    links = DatamoduleConnection.get_for_datamodule(dm.id)
    if links:
        out += ["", "[connections]  # local name = Strata connection"]
        for link in links:
            crow = Connection.get_by_id(link.connection_id)
            if crow is not None:
                out.append(f"{_key(link.local_name)} = {_value(crow.name)}")
    deps = Dependency.get_for_datamodule(dm.id)
    if deps:
        out += ["", "[dependencies]  # local name = datamodule path"]
        for dep in deps:
            parent = Datamodule.get_by_id(dep.depends_on_id)
            if parent is not None:
                out.append(f"{_key(dep.local_name)} = {_value(parent.path)}")
    for schedule in Schedule.for_datamodule(dm.id):
        if schedule.report_id is None:
            out += _schedule_lines("schedule", schedule, report=False)

    for report in Report.get_for_datamodule(dm.id):
        out += ["", "[[report]]", f"name = {_value(report.name)}"]
        out.append(f"macro = {_value(report.macro_name)}")
        if report.description:
            out.append(f"description = {_value(report.description)}")
        out.append(f"body = {_value(report.body_sql.replace(chr(13) + chr(10), chr(10)))}")
        check = ReportCheck.get(report.id)
        if check is not None:
            out.append(f"check = {_value(check.severity)}")
            out.append(f"gate = {_value(check.gate)}")
        for arg in ReportArg.get_for_report(report.id):
            out += ["", "[[report.arg]]", f"name = {_value(arg.name)}"]
            out.append(f"type = {_value(arg.data_type)}")
            out.append(f"widget = {_value(arg.widget)}")
            out.append(f"required = {_value(arg.required)}")
            if arg.default_value:
                out.append(f"default = {_value(arg.default_value)}")
            if arg.constraint_query:
                out.append(f"constraint = {_value(arg.constraint_query)}")
            if arg.description:
                out.append(f"description = {_value(arg.description)}")
        for schedule in Schedule.for_report(report.id):
            out += _schedule_lines("report.schedule", schedule, report=True)
    return "\n".join(out) + "\n"


def export_path(out_dir: Path, dm: Datamodule) -> Path:
    """Where ``dm``'s file goes: its path's folders under ``out_dir``."""
    parts = [re.sub(r"[^A-Za-z0-9_.-]+", "_", p) or "_" for p in dm.path.split("/") if p]
    return out_dir.joinpath(*parts[:-1], f"{parts[-1]}.toml")
