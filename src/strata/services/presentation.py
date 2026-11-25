"""A report's presentation: how its rows are shown. Formats, alignment, hidden columns,
links, styles, totals and subtotals: a configuration of its own per report, which never
changes the data (user decision 2026-11-25).

It is one JSON document in ``report.presentation`` (NULL = no settings), read and written
whole with its report; a definition file's ``[[report.column]]``, ``[[report.style]]``,
``subtotals_by`` and ``total_row`` map onto it one to one.

``validate(presentation, columns)`` checks it against the report's (name, DuckDB type)
columns and returns every problem, each tied to the field it is about. A style rule's
condition is SQL: DuckDB binds it against the live data (``report_service.describe_on``).
"""

import difflib
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, fields
from typing import Any

from strata.services import cell_format, excel_format, link_template, styles

log = logging.getLogger(__name__)

ALIGNS = ("auto", *cell_format.ALIGNMENTS)
TOTALS = ("sum", "avg", "min", "max", "count", "count_distinct")
_TOTAL_KINDS = {
    "sum": cell_format.NUMBER_KINDS,
    "avg": cell_format.NUMBER_KINDS,
    "min": cell_format.NUMBER_KINDS | cell_format.DATE_TIME_KINDS | {cell_format.TEXT},
    "max": cell_format.NUMBER_KINDS | cell_format.DATE_TIME_KINDS | {cell_format.TEXT},
}
_TOTAL_NEEDS = {
    "sum": "numbers",
    "avg": "numbers",
    "min": "numbers, dates, times or text",
    "max": "numbers, dates, times or text",
}


# The Format page offers these first, by the column's kind (cell_format.kind).
PRESETS: dict[str, tuple[str, ...]] = {
    cell_format.INTEGER: ("0", "#,##0"),
    cell_format.DECIMAL: (
        "#,##0.00",
        "#,##0.00;[Red]-#,##0.00",
        "0.00",
        "#,##0",
        "0.0000",
        "0.000000",
        "0.0%",
        "0.00%",
    ),
    cell_format.DATE: ("yyyy-mm-dd", "d mmm yyyy", "dd/mm/yyyy", "ddd d mmm yyyy"),
    cell_format.TIMESTAMP: (
        "yyyy-mm-dd hh:mm",
        "yyyy-mm-dd hh:mm:ss",
        "d mmm yyyy h:mm AM/PM",
        "yyyy-mm-dd",
    ),
    cell_format.TIME: ("hh:mm", "hh:mm:ss", "h:mm AM/PM"),
    cell_format.TEXT: ("@",),
}
PRESETS[cell_format.FLOAT] = PRESETS[cell_format.DECIMAL]


def codes_for(duck_type: str, documents: Sequence[str]) -> list[str]:
    """Format codes for a column of ``duck_type``: the presets for its kind, then the
    codes other reports use (``documents``: their stored JSON) that fit it."""
    codes = list(PRESETS.get(cell_format.kind(duck_type), ()))
    for document in documents:
        try:
            used = from_json(document)
        except PresentationError:
            continue
        for c in used.columns:
            if c.format and c.format not in codes:
                try:
                    excel_format.compile(c.format, duck_type)
                except excel_format.FormatError:
                    continue
                codes.append(c.format)
    return codes


class PresentationError(ValueError):
    """A presentation document (JSON, or a definition file's keys) of the wrong shape."""


@dataclass
class ColumnSetting:
    name: str
    format: str = ""  # an Excel format code; "" = plain
    align: str = "auto"
    total: str = ""  # one of TOTALS; "" = none
    hidden: bool = False  # hidden on the page and in mail; kept in downloads
    link: str = ""  # a link template; "" = no link
    link_text: str = ""  # a template; "" = the cell's (formatted) value
    new_tab: bool = False

    def is_default(self) -> bool:
        return self == ColumnSetting(self.name)


@dataclass
class StyleRule:
    when: str  # a DuckDB condition on the row
    style: str  # space-separated words (services/styles)
    columns: list[str] = field(default_factory=list)  # [] = the whole row


@dataclass
class Presentation:
    columns: list[ColumnSetting] = field(default_factory=list)
    styles: list[StyleRule] = field(default_factory=list)
    subtotals_by: list[str] = field(default_factory=list)  # ordered: nested subtotals
    total_row: bool = False

    def normalised(self) -> Presentation:
        """Text trimmed (but not a format code or template), and column settings that
        say nothing left out."""
        cols = [c for c in self.columns if not c.is_default()]
        rules = [StyleRule(r.when.strip(), r.style.strip(), list(r.columns)) for r in self.styles]
        return Presentation(cols, rules, list(self.subtotals_by), self.total_row)

    def is_empty(self) -> bool:
        n = self.normalised()
        return not (n.columns or n.styles or n.subtotals_by or n.total_row)

    def setting(self, name: str) -> ColumnSetting:
        """The settings of column ``name`` (all defaults when it has none)."""
        return next((c for c in self.columns if c.name == name), ColumnSetting(name))

    def to_dict(self) -> dict[str, Any]:
        n = self.normalised()
        return {
            "columns": [column_dict(c) for c in n.columns],
            "styles": [{"when": r.when, "columns": r.columns, "style": r.style} for r in n.styles],
            "subtotals_by": n.subtotals_by,
            "total_row": n.total_row,
        }

    def to_json(self) -> str | None:
        """The stored document; None when there are no settings."""
        return None if self.is_empty() else json.dumps(self.to_dict(), ensure_ascii=False)


def column_dict(c: ColumnSetting) -> dict[str, Any]:
    """A column's settings: its name and every key that is not its default."""
    default = ColumnSetting(c.name)
    out: dict[str, Any] = {"name": c.name}
    for f in fields(ColumnSetting):
        value = getattr(c, f.name)
        if f.name != "name" and value != getattr(default, f.name):
            out[f.name] = value
    return out


_COLUMN_KEYS = {f.name for f in fields(ColumnSetting)}


def _known(data: dict[str, Any], keys: set[str], where: str) -> None:
    unknown = sorted(set(data) - keys)
    if unknown:
        raise PresentationError(f"{where}: unknown key(s) {', '.join(unknown)}.")


def _text(data: dict[str, Any], key: str, where: str, required: bool = False) -> str:
    value = data.get(key)
    if value is None:
        if required:
            raise PresentationError(f"{where}: '{key}' is required.")
        return ""
    if not isinstance(value, str):
        raise PresentationError(f"{where}: '{key}' must be text.")
    return value


def _flag(data: dict[str, Any], key: str, where: str) -> bool:
    value = data.get(key, False)
    if not isinstance(value, bool):
        raise PresentationError(f"{where}: '{key}' must be true or false.")
    return value


def _names(data: dict[str, Any], key: str, where: str) -> list[str]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise PresentationError(f"{where}: '{key}' must be a list of column names.")
    return list(value)


def column_from_dict(data: Any, where: str) -> ColumnSetting:
    if not isinstance(data, dict):
        raise PresentationError(f"{where}: a column's settings must be a table.")
    _known(data, _COLUMN_KEYS, where)
    name = _text(data, "name", where, required=True)
    where = f"{where} ('{name}')"
    return ColumnSetting(
        name=name,
        format=_text(data, "format", where),
        align=_text(data, "align", where) or "auto",
        total=_text(data, "total", where),
        hidden=_flag(data, "hidden", where),
        link=_text(data, "link", where),
        link_text=_text(data, "link_text", where),
        new_tab=_flag(data, "new_tab", where),
    )


def style_from_dict(data: Any, where: str) -> StyleRule:
    if not isinstance(data, dict):
        raise PresentationError(f"{where}: a style rule must be a table.")
    _known(data, {"when", "columns", "style"}, where)
    return StyleRule(
        when=_text(data, "when", where, required=True),
        style=_text(data, "style", where, required=True),
        columns=_names(data, "columns", where),
    )


def from_dict(data: Any, where: str = "presentation") -> Presentation:
    if not isinstance(data, dict):
        raise PresentationError(f"{where}: must be a table.")
    _known(data, {"columns", "styles", "subtotals_by", "total_row"}, where)
    cols = data.get("columns", [])
    rules = data.get("styles", [])
    if not isinstance(cols, list) or not isinstance(rules, list):
        raise PresentationError(f"{where}: 'columns' and 'styles' must be lists.")
    return Presentation(
        columns=[column_from_dict(c, f"{where}, column {i + 1}") for i, c in enumerate(cols)],
        styles=[style_from_dict(r, f"{where}, style {i + 1}") for i, r in enumerate(rules)],
        subtotals_by=_names(data, "subtotals_by", where),
        total_row=_flag(data, "total_row", where),
    )


def from_json(document: str | None) -> Presentation:
    if not document:
        return Presentation()
    try:
        return from_dict(json.loads(document))
    except (ValueError, PresentationError) as exc:
        raise PresentationError(f"The stored presentation cannot be read: {exc}") from exc


def of(report: Any) -> Presentation:
    """A report's settings. A stored document that cannot be read (it never should)
    shows the report plain rather than failing it."""
    try:
        return from_json(report.presentation)
    except PresentationError:
        log.exception("report %s: unreadable presentation", getattr(report, "uuid", "?"))
        return Presentation()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Problem:
    """``field`` says where in the Format page: ``column:<name>:<key>``,
    ``style:<n>:<key>`` (n from 1), ``subtotals_by`` or ``total_row``."""

    field: str
    message: str


def describe(problems: Sequence[Problem]) -> str:
    return "\n".join(p.message for p in problems)


def _unknown_column(name: str, known: Sequence[str]) -> str:
    near = difflib.get_close_matches(name, known, n=1, cutoff=0.6)
    hint = f" (did you mean '{near[0]}'?)" if near else ""
    return f"'{name}' is not a column of this report{hint}"


def validate(
    p: Presentation,
    columns: Sequence[tuple[str, str]] | None,
    schemes: Sequence[str] = link_template.DEFAULT_SCHEMES,
) -> list[Problem]:
    """Every problem of ``p`` against the report's (name, DuckDB type) ``columns``;
    ``schemes`` = the link schemes allowed. ``columns`` None (no live data yet: a
    datamodule not built) checks all that does not need them: names and types are
    checked once there is data (``report_service.check_reports``)."""
    known = columns is not None
    types = dict(columns or [])
    names = [n for n, _ in columns or []]
    problems: list[Problem] = []

    def bad(where: str, message: str) -> None:
        problems.append(Problem(where, message))

    seen: set[str] = set()
    for c in p.columns:
        here = f"column:{c.name}"
        if c.name in seen:
            bad(f"{here}:name", f"'{c.name}' has two sets of settings")
            continue
        seen.add(c.name)
        if known and c.name not in types:
            bad(f"{here}:name", _unknown_column(c.name, names) + ": remove its settings")
            continue
        _check_column(c, types.get(c.name), names if known else None, schemes, bad)

    hidden = {c.name for c in p.columns if c.hidden}
    totals = {c.name for c in p.columns if c.total}
    groups: set[str] = set()
    for name in p.subtotals_by:
        if name in groups:
            bad("subtotals_by", f"Subtotals by '{name}' twice")
        elif known and name not in types:
            bad("subtotals_by", "Subtotals by " + _unknown_column(name, names))
        elif name in hidden:
            bad("subtotals_by", f"'{name}' is hidden, but its subtotal rows are labelled in it")
        elif name in totals:
            bad(
                f"column:{name}:total",
                f"'{name}' groups the subtotals, so it shows their labels, not a total",
            )
        groups.add(name)
    if p.subtotals_by and not totals:
        bad("subtotals_by", "Subtotals need a column with a total (e.g. sum on an amount)")
    if p.total_row and not totals:
        bad("total_row", "A total row needs a column with a total (e.g. sum on an amount)")

    for n, rule in enumerate(p.styles, 1):
        here = f"style:{n}"
        if not rule.when.strip():
            bad(f"{here}:when", f'Style {n}: a rule needs a condition, e.g. "Amount" < 0')
        try:
            styles.parse(rule.style)
        except styles.StyleError as exc:
            bad(f"{here}:style", f"Style {n}: {exc}")
        listed: set[str] = set()
        for name in rule.columns:
            if name in listed:
                bad(f"{here}:columns", f"Style {n}: '{name}' listed twice")
            elif known and name not in types:
                bad(f"{here}:columns", f"Style {n}: " + _unknown_column(name, names))
            elif name in hidden:
                bad(f"{here}:columns", f"Style {n}: '{name}' is hidden, so its style never shows")
            listed.add(name)
    return problems


_Bad = Callable[[str, str], None]


def _check_column(
    c: ColumnSetting,
    duck_type: str | None,
    names: Sequence[str] | None,
    schemes: Sequence[str],
    bad: _Bad,
) -> None:
    """``duck_type``/``names`` None: the columns are not known yet."""
    here = f"column:{c.name}"
    if c.format:
        try:
            if duck_type is None:
                excel_format.check_syntax(c.format)
            else:
                excel_format.compile(c.format, duck_type)
        except excel_format.FormatError as exc:
            bad(f"{here}:format", f"{c.name}: format: {exc}")
    if c.align not in ALIGNS:
        bad(f"{here}:align", f"{c.name}: align is auto, left, center or right, not '{c.align}'")
    if c.total:
        kinds = _TOTAL_KINDS.get(c.total)
        if c.total not in TOTALS:
            bad(f"{here}:total", f"{c.name}: total is one of {', '.join(TOTALS)}, not '{c.total}'")
        elif kinds is not None and duck_type and cell_format.kind(duck_type) not in kinds:
            bad(
                f"{here}:total",
                f"{c.name}: {c.total} needs {_TOTAL_NEEDS[c.total]}; this column is {duck_type}",
            )
        elif c.hidden:
            bad(f"{here}:total", f"{c.name} is hidden, so its total never shows")
    if c.link:
        _check_template(c.name, "link", c.link, names, bad)
        try:
            scheme = link_template.parse(c.link).scheme()
        except link_template.TemplateError:
            scheme = ""
        if _has_blank(c.link):
            bad(f"{here}:link", f"{c.name}: link: an address has no spaces: write %20 for one")
        if scheme and scheme not in schemes:
            bad(
                f"{here}:link",
                f"{c.name}: link: links may go to {', '.join(f'{s}:' for s in schemes)} "
                f"addresses, not '{scheme}:'",
            )
        if c.hidden:
            bad(f"{here}:link", f"{c.name} is hidden, so its link never shows")
    else:
        if c.link_text:
            bad(f"{here}:link_text", f"{c.name}: link text needs a link")
        if c.new_tab:
            bad(f"{here}:new_tab", f"{c.name}: open in a new tab needs a link")
    if c.link_text:
        _check_template(c.name, "link_text", c.link_text, names, bad)


def _has_blank(source: str) -> bool:
    """A space or control character in the template's own text (values are encoded)."""
    try:
        parts = link_template.parse(source).parts
    except link_template.TemplateError:
        return False
    return any(not p.column and any(ch <= " " or ch == "\x7f" for ch in p.text) for p in parts)


def _check_template(
    column: str, key: str, source: str, names: Sequence[str] | None, bad: _Bad
) -> None:
    label = "link" if key == "link" else "link text"
    try:
        template = link_template.parse(source)
    except link_template.TemplateError as exc:
        bad(f"column:{column}:{key}", f"{column}: {label}: {exc}")
        return
    for name in template.columns():
        if names is not None and name not in names:
            bad(f"column:{column}:{key}", f"{column}: {label}: " + _unknown_column(name, names))
