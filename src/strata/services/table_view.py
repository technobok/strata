"""The cells a results table shows: each value's text, alignment, link and style,
worked out once in Python for the rows actually shown (a page, or a mail's rows). The
page and run page (``templates/reports/_table.html``) and the mail table
(``notify_service``) all draw from a ``TableView``; a new table surface does the same
rather than printing raw values.

A report's presentation (``services/presentation``) decides formats, alignment, hidden
columns, links and styles. Rendering never fails because of it: a setting that no longer
fits (a column gone, a format for another type) is skipped and that cell shows plain.
Style rules are evaluated by DuckDB in the query that fetches the rows (one true/false
flag per rule per row: ``report_service.page_of``); this module applies them.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from strata.services import cell_format, excel_format, link_template, styles, totals
from strata.services.presentation import ColumnSetting, Presentation


@dataclass(frozen=True)
class Column:
    name: str
    duck_type: str
    align: str


@dataclass(frozen=True)
class Cell:
    text: str
    align: str
    href: str | None = None
    new_tab: bool = False
    # Style words (services/styles): the rules that apply, and a format's colour.
    words: tuple[str, ...] = ()

    @property
    def css_class(self) -> str:
        """The page's classes: alignment, then the style words."""
        own = styles.page_attrs(self.words)[0]
        return " ".join(
            c for c in (f"align-{self.align}" if self.align != "left" else "", own) if c
        )

    @property
    def css_style(self) -> str:
        """An inline colour for an exact shade (red-550), else empty."""
        return styles.page_attrs(self.words)[1]


@dataclass
class Row:
    cells: list[Cell]
    kind: str = "data"  # data | subtotal | total
    level: int = 0  # a subtotal's group level (1 = the outermost)
    # A totals view's group row: the filters that select its rows, how many it has,
    # and (set by reports.build_ui) the URL of its rows.
    group_filters: list[tuple[str, str]] = field(default_factory=list)
    group_rows: int = 0
    rows_url: str | None = None


@dataclass
class TableView:
    columns: list[Column] = field(default_factory=list)  # the shown ones
    rows: list[Row] = field(default_factory=list)

    @property
    def data_count(self) -> int:
        """Rows of data (not subtotal or total rows)."""
        return sum(1 for r in self.rows if r.kind == "data")


@dataclass(frozen=True)
class Links:
    """Where links may go: ``root`` = Strata's own root path (a link that is not an
    address or a site path goes within Strata), ``schemes`` = the allowed schemes.
    Mail and downloads have no links: they render with ``links=None``."""

    root: str
    schemes: tuple[str, ...]

    @staticmethod
    def here() -> Links:
        """For the page being served (needs a request)."""
        from flask import request

        return Links(request.script_root + "/", link_template.allowed_schemes())


@dataclass
class _Plan:
    """How one shown column renders."""

    index: int
    column: Column
    link: link_template.Template | None
    link_text: link_template.Template | None
    new_tab: bool


def formatter_for(setting: ColumnSetting, duck_type: str) -> excel_format.Formatter | None:
    """The column's format, or None for plain (none set, or it no longer fits)."""
    if not setting.format:
        return None
    try:
        return excel_format.compile(setting.format, duck_type)
    except excel_format.FormatError:
        return None


def _template(source: str, names: set[str]) -> link_template.Template | None:
    """A link template whose columns are all in the result, else None (no link)."""
    try:
        template = link_template.parse(source)
    except link_template.TemplateError:
        return None
    return template if set(template.columns()) <= names else None


def _rules(look: Presentation, names: set[str]) -> list[tuple[list[str], set[str] | None]]:
    """Each style rule's words and the columns it styles (None = the whole row); a
    rule whose words no longer parse styles nothing."""
    out: list[tuple[list[str], set[str] | None]] = []
    for rule in look.styles:
        try:
            words = styles.parse(rule.style)
        except styles.StyleError:
            words = []
        out.append((words, set(rule.columns) & names if rule.columns else None))
    return out


def render(
    columns: Sequence[tuple[str, str]],
    rows: Sequence[Sequence[Any]],
    look: Presentation | None = None,
    links: Links | None = None,
    flags: Sequence[Sequence[Any]] | None = None,
    total_rows: Sequence[totals.TotalRow] = (),
) -> TableView:
    """``columns`` = (name, DuckDB type) as DESCRIBE gives them; ``rows`` = the values;
    ``look`` = the report's settings; ``links`` None = no links (mail); ``flags`` = per
    row, whether each of ``look.styles`` holds for it (None: rules not evaluated);
    ``total_rows`` = subtotal and total rows, each after the data row it follows."""
    look = look or Presentation()
    names = {name for name, _ in columns}
    formatters: list[excel_format.Formatter | None] = []
    plans: list[_Plan] = []
    for i, (name, duck_type) in enumerate(columns):
        setting = look.setting(name)
        formatters.append(formatter_for(setting, duck_type))
        if setting.hidden:
            continue
        align = setting.align if setting.align != "auto" else cell_format.default_align(duck_type)
        link = _template(setting.link, names) if links and setting.link else None
        text = _template(setting.link_text, names) if link and setting.link_text else None
        plans.append(_Plan(i, Column(name, duck_type, align), link, text, setting.new_tab))
    any_links = any(p.link for p in plans)
    rules = _rules(look, names) if flags is not None else []
    after: dict[int, list[totals.TotalRow]] = {}
    for tr in total_rows:
        after.setdefault(tr.after, []).append(tr)
    body: list[Row] = []
    for r, row in enumerate(rows):
        holds = [rule for rule, flag in zip(rules, flags[r], strict=True) if flag] if flags else []
        shown = [
            fmt.render(value) if fmt else (cell_format.plain(value, duck_type), None)
            for (_name, duck_type), fmt, value in zip(columns, formatters, row, strict=True)
        ]
        raw: dict[str, str | None] = {}
        texts: dict[str, str | None] = {}
        if any_links:
            for (name, duck_type), value, (text, _) in zip(columns, row, shown, strict=True):
                raw.setdefault(name, None if value is None else cell_format.plain(value, duck_type))
                texts.setdefault(name, text)
        cells: list[Cell] = []
        for p in plans:
            text, colour = shown[p.index]
            name = p.column.name
            words = styles.merge([w for w, cols in holds if cols is None or name in cols], colour)
            href = _href(p, raw, links) if links is not None else None
            if href is not None and p.link_text is not None:
                text = link_template.fill(p.link_text, texts, encode=False) or text
            if not text:
                href = None  # nothing to click
            cells.append(Cell(text, p.column.align, href, p.new_tab and href is not None, words))
        body.append(Row(cells))
        for tr in after.get(r, []):
            body.append(_total_row(tr, plans, look, formatters, columns))
    return TableView(columns=[p.column for p in plans], rows=body)


def render_totals(
    columns: Sequence[tuple[str, str]],
    look: Presentation,
    ordered: Sequence[totals.TotalRow],
    level: int,
) -> TableView:
    """The totals view: only total rows, in order. A row of the view's own level can
    load its group's rows (its ``group_filters``)."""
    plans: list[_Plan] = []
    formatters: list[excel_format.Formatter | None] = []
    for i, (name, duck_type) in enumerate(columns):
        setting = look.setting(name)
        formatters.append(formatter_for(setting, duck_type))
        if setting.hidden:
            continue
        align = setting.align if setting.align != "auto" else cell_format.default_align(duck_type)
        plans.append(_Plan(i, Column(name, duck_type, align), None, None, False))
    groups = [g for g in look.subtotals_by if g in {n for n, _ in columns}]
    rows: list[Row] = []
    for tr in ordered:
        row = _total_row(tr, plans, look, formatters, columns)
        if tr.level == level:
            row.group_filters = [
                (f"{g}__isnull", "true") if v is None else (f"{g}__eq", cell_format.plain(v))
                for g, v in zip(groups, tr.key, strict=False)
            ]
            row.group_rows = tr.rows
        rows.append(row)
    return TableView(columns=[p.column for p in plans], rows=rows)


def _total_row(
    tr: totals.TotalRow,
    plans: list[_Plan],
    look: Presentation,
    formatters: list[excel_format.Formatter | None],
    columns: Sequence[tuple[str, str]],
) -> Row:
    """A subtotal row: its group's value and "total" in the group's column, the outer
    groups' values in theirs, each column's total in its format (counts plain). The
    grand total: "Total" in the first shown column without a total."""
    index = {name: i for i, (name, _) in enumerate(columns)}
    groups = [g for g in look.subtotals_by if g in index]
    functions = {c.name: c.total for c in look.columns if c.total}

    def shown(name: str, value: Any, plain_type: str) -> str:
        fmt = formatters[index[name]]
        if fmt is not None:
            return fmt.render(value)[0]
        return cell_format.plain(value, plain_type)

    label_at = None
    if tr.level == 0:
        label_at = next((p.column.name for p in plans if p.column.name not in tr.values), None)
    cells: list[Cell] = []
    for p in plans:
        name = p.column.name
        text = ""
        if name in tr.values:
            fn = functions.get(name, "")
            value = tr.values[name]
            if value is not None:
                if fn in ("count", "count_distinct"):
                    text = cell_format.plain(value)
                else:
                    same_type = p.column.duck_type if fn in ("min", "max") else ""
                    text = shown(name, value, same_type)
        elif tr.level > 0 and name in groups[: tr.level]:
            k = groups.index(name)
            value = tr.key[k]
            value_text = "(blank)" if value is None else shown(name, value, p.column.duck_type)
            text = f"{value_text} total" if k == tr.level - 1 else value_text
        elif name == label_at:
            text = tr.label or "Total"
        cells.append(Cell(text, p.column.align))
    return Row(cells, "subtotal" if tr.level else "total", tr.level)


def _href(plan: _Plan, raw: dict[str, str | None], links: Links) -> str | None:
    """The cell's address, checked as it will be followed; None = plain text (no link
    set, a NULL in a placeholder, or an address that may not be followed)."""
    if plan.link is None:
        return None
    filled = link_template.fill(plan.link, raw, encode=True)
    if filled is None:
        return None
    return link_template.safe_address(filled, links.root, links.schemes)
