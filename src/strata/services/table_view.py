"""The cells a results table shows: each value's text, alignment, link and colour,
worked out once in Python for the rows actually shown (a page, or a mail's rows). The
page and run page (``templates/reports/_table.html``) and the mail table
(``notify_service``) all draw from a ``TableView``; a new table surface does the same
rather than printing raw values.

A report's presentation (``services/presentation``) decides formats, alignment, hidden
columns and links. Rendering never fails because of it: a setting that no longer fits
(a column gone, a format for another type) is skipped and that cell shows plain.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from strata.services import cell_format, excel_format, link_template
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
    colour: str | None = None  # a format's colour: a Pico family (or black/white)


@dataclass
class TableView:
    columns: list[Column] = field(default_factory=list)  # the shown ones
    rows: list[list[Cell]] = field(default_factory=list)


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


def render(
    columns: Sequence[tuple[str, str]],
    rows: Sequence[Sequence[Any]],
    look: Presentation | None = None,
    links: Links | None = None,
) -> TableView:
    """``columns`` = (name, DuckDB type) as DESCRIBE gives them; ``rows`` = the values;
    ``look`` = the report's settings; ``links`` None = no links (mail)."""
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
    body: list[list[Cell]] = []
    for row in rows:
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
            href = _href(p, raw, links) if links is not None else None
            if href is not None and p.link_text is not None:
                text = link_template.fill(p.link_text, texts, encode=False) or text
            if not text:
                href = None  # nothing to click
            cells.append(Cell(text, p.column.align, href, p.new_tab and href is not None, colour))
        body.append(cells)
    return TableView(columns=[p.column for p in plans], rows=body)


def _href(plan: _Plan, raw: dict[str, str | None], links: Links) -> str | None:
    """The cell's address, checked as it will be followed; None = plain text (no link
    set, a NULL in a placeholder, or an address that may not be followed)."""
    if plan.link is None:
        return None
    filled = link_template.fill(plan.link, raw, encode=True)
    if filled is None:
        return None
    return link_template.safe_address(filled, links.root, links.schemes)
