"""The Format page: a report's presentation settings (services/presentation), checked as
they are typed and previewed on the report's first page.

Owners only. It shows the report's data (a sample value per column and a preview page),
so each view and each preview is a read in the audit (kind ``preview``). It needs the
datamodule's live data: without it the report's columns are not known.
"""

import json
import re
from dataclasses import dataclass

from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, url_for
from werkzeug.exceptions import BadRequest, NotFound
from werkzeug.wrappers import Response

from strata.blueprints.auth import owner_required
from strata.models.datamodule import Datamodule
from strata.models.report import Report
from strata.models.report_arg import ReportArg
from strata.services import (
    authoring,
    cell_format,
    datamodule_service,
    presentation,
    read_audit,
    report_service,
    styles,
    table_view,
)
from strata.services.presentation import ColumnSetting, Presentation, Problem, StyleRule
from strata.services.query_filter import parse_params
from strata.services.report_service import ArgError, TableResult

bp = Blueprint("formatting", __name__, url_prefix="/datamodules/reports")


def _load(report_uuid: str) -> tuple[Report, Datamodule]:
    report = Report.get_by_uuid(report_uuid)
    if report is None:
        raise NotFound()
    dm = Datamodule.get_by_id(report.datamodule_id)
    if dm is None:
        raise NotFound()
    return report, dm


@bp.route("/<report_uuid>/format", methods=["GET", "POST"])
@owner_required
def page(report_uuid: str) -> str | Response:
    report, dm = _load(report_uuid)
    read_audit.note("preview", dm, report)
    stored = presentation.of(report)
    if request.method == "POST" and not dm.access_lock:
        draft = _draft(stored)
        try:
            authoring.save_presentation(dm, report, draft, g.user.username)
        except authoring.AuthoringError as exc:  # PresentationRefused: the page marks each
            return _render(report, dm, draft, str(exc))
        flash("Formatting saved.", "success")
        return redirect(url_for("formatting.page", report_uuid=report.uuid))
    return _render(report, dm, stored)


@bp.route("/<report_uuid>/format/preview", methods=["POST"])
@owner_required
def preview(report_uuid: str) -> Response:
    """The draft's problems (by field), each column's sample and the preview page, as
    JSON for the page's script."""
    report, dm = _load(report_uuid)
    read_audit.note("preview", dm, report)
    if dm.access_lock:
        return jsonify({"problems": {}, "summary": [], "samples": {}, "preview": ""})
    draft = _draft(presentation.of(report))
    view = _view(report, dm, draft)
    problems: dict[str, str] = {}
    for p in view.problems:
        problems.setdefault(p.field, p.message)
    return jsonify(
        {
            "problems": problems,
            "summary": [p.message for p in view.problems],
            "samples": {
                row.key: {"format": row.shown if row.setting.format else "", "link": row.link}
                for row in view.rows
            },
            "preview": render_template("formatting/_preview.html", view=view),
        }
    )


def _draft(stored: Presentation) -> Presentation:
    """The settings the form gives. A part the form does not carry (its ``*_present``
    marker missing) is kept as stored."""
    form = request.form
    columns: list[ColumnSetting] = []
    n = 0
    while f"c-{n}-name" in form:
        key = f"c-{n}"
        fmt = form.get(f"{key}-format", "")
        columns.append(
            ColumnSetting(
                name=form[f"{key}-name"],
                format=fmt if fmt.strip() else "",
                align=form.get(f"{key}-align", "auto"),
                total=form.get(f"{key}-total", ""),
                hidden=form.get(f"{key}-hidden") == "on",
                link=form.get(f"{key}-link", "").strip(),
                link_text=form.get(f"{key}-link_text", "").strip(),
                new_tab=form.get(f"{key}-new_tab") == "on",
            )
        )
        n += 1
    k = 0
    while f"s-{k}-json" in form:  # settings of a column no longer in the result
        if form.get(f"s-{k}-remove") != "on":
            try:
                columns.append(presentation.column_from_dict(json.loads(form[f"s-{k}-json"]), ""))
            except (ValueError, presentation.PresentationError) as exc:
                raise BadRequest(f"a column's settings could not be read: {exc}") from exc
        k += 1
    rules = _rules() if form.get("styles_present") else stored.styles
    if form.get("totals_present"):
        groups = [g for g in form.getlist("subtotals_by") if g]
        total_row = form.get("total_row") == "on"
    else:
        groups, total_row = stored.subtotals_by, stored.total_row
    return Presentation(columns, rules, groups, total_row)


_RULE_RE = re.compile(r"r-(\w+)-when")


def _rules() -> list[StyleRule]:
    """The style rule rows in the order the page has them (r-<id>-when/style/columns;
    ids only tell rows apart). A row with neither a condition nor a style is left out."""
    form = request.form
    rules: list[StyleRule] = []
    for key in form:
        match = _RULE_RE.fullmatch(key)
        if not match:
            continue
        rid = match.group(1)
        when = form.get(key, "").strip()
        style = form.get(f"r-{rid}-style", "").strip()
        if when or style:
            rules.append(StyleRule(when, style, form.getlist(f"r-{rid}-columns")))
    return rules


@dataclass
class _Row:
    """One result column on the page."""

    key: str  # the form fields' prefix: c-<n>
    name: str
    duck_type: str
    kind: str
    setting: ColumnSetting
    sample: str  # its first value in the preview, plain
    shown: str  # that value with the draft's format
    link: str  # the first row's link, or why there is none
    codes: list[str]
    totals: list[str]  # the total functions it takes


@dataclass
class _View:
    rows: list[_Row]
    stale: list[ColumnSetting]  # settings for columns no longer in the result
    problems: list[Problem]
    report_problem: str | None
    result: TableResult | None
    missing_args: list[ReportArg]  # required, no default: the preview asks for them
    arg_values: dict[str, str]
    no_data: bool


def _view(report: Report, dm: Datamodule, look: Presentation) -> _View:
    described, problems = authoring.check_presentation(dm, report, look)
    args = ReportArg.get_for_report(report.id)
    missing = [a for a in args if a.required and not a.default_value]
    given = {a.name: request.form.get(f"arg-{a.name}", "").strip() for a in missing}
    view = _View([], [], problems, None, None, missing, given, described is None)
    if described is None:
        return view
    if described.problem is not None:
        view.report_problem = described.problem
        return view
    version = datamodule_service.live_version(dm)
    if version is not None and all(given.values()):
        arg_values, _ = parse_params([(k, v) for k, v in given.items()], set(given))
        try:
            view.result = report_service.preview_table(report, dm, arg_values, version, look)
        except ArgError as exc:
            view.result = TableResult(error=str(exc))
        if view.result.rows:
            read_audit.update(version=version, row_count=view.result.row_count)
    documents = Report.presentations()
    names = {name for name, _ in described.columns}
    view.stale = [c for c in look.columns if c.name not in names]
    first_hrefs = _first_hrefs(view.result)
    for n, (name, duck_type) in enumerate(described.columns):
        setting = look.setting(name)
        sample, shown = _sample(view.result, n, duck_type, setting)
        view.rows.append(
            _Row(
                key=f"c-{n}",
                name=name,
                duck_type=duck_type,
                kind=cell_format.kind(duck_type),
                setting=setting,
                sample=sample,
                shown=shown,
                link=_link_note(setting, name, first_hrefs),
                codes=presentation.codes_for(duck_type, documents),
                totals=presentation.totals_for(duck_type),
            )
        )
    return view


def _sample(
    result: TableResult | None, n: int, duck_type: str, setting: ColumnSetting
) -> tuple[str, str]:
    """The column's first non-NULL value in the preview: plain, and formatted."""
    if result is None or result.error:
        return "", ""
    value = next((row[n] for row in result.rows if row[n] is not None), None)
    if value is None:
        return "", ""
    plain = cell_format.plain(value, duck_type)
    fmt = table_view.formatter_for(setting, duck_type)
    return plain, fmt.render(value)[0] if fmt else plain


def _first_hrefs(result: TableResult | None) -> dict[str, str | None]:
    if result is None or result.view is None or not result.view.rows:
        return {}
    first = result.view.rows[0].cells
    return {col.name: cell.href for col, cell in zip(result.view.columns, first, strict=True)}


def _link_note(setting: ColumnSetting, name: str, hrefs: dict[str, str | None]) -> str:
    if not setting.link or setting.hidden or not hrefs:
        return ""
    href = hrefs.get(name)
    return f"First row: {href}" if href else "First row: no link (a value is empty, or not allowed)"


def _render(report: Report, dm: Datamodule, look: Presentation, error: str | None = None) -> str:
    view = None if dm.access_lock else _view(report, dm, look)
    marks: dict[str, str] = {}
    for p in view.problems if view else []:
        marks.setdefault(p.field, p.message)
    return render_template(
        "formatting/page.html",
        report=report,
        datamodule=dm,
        view=view,
        marks=marks,
        error=error,
        aligns=presentation.ALIGNS,
        formattable=cell_format.NUMBER_KINDS | cell_format.DATE_TIME_KINDS | {cell_format.TEXT},
        stale_json=lambda c: json.dumps(presentation.column_dict(c)),
        look=look,
        style_words=json.dumps(
            [*styles.WORDS, *styles.FAMILIES, *(f"bg-{f}" for f in styles.FAMILIES)]
        ),
    )
