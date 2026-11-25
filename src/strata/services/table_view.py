"""The cells a results table shows: each value's text and alignment, worked out once in
Python for the rows actually shown (a page, or a mail's rows). The page and run page
(``templates/reports/_table.html``) and the mail table (``notify_service``) all draw
from a ``TableView``; a new table surface does the same rather than printing raw values.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from strata.services import cell_format


@dataclass(frozen=True)
class Column:
    name: str
    duck_type: str
    align: str


@dataclass(frozen=True)
class Cell:
    text: str
    align: str


@dataclass
class TableView:
    columns: list[Column] = field(default_factory=list)
    rows: list[list[Cell]] = field(default_factory=list)


def render(columns: Sequence[tuple[str, str]], rows: Sequence[Sequence[Any]]) -> TableView:
    """``columns`` = (name, DuckDB type) as DESCRIBE gives them; ``rows`` = the values."""
    cols = [
        Column(name, duck_type, cell_format.default_align(duck_type)) for name, duck_type in columns
    ]
    body = [
        [
            Cell(cell_format.plain(value, col.duck_type), col.align)
            for col, value in zip(cols, row, strict=True)
        ]
        for row in rows
    ]
    return TableView(columns=cols, rows=body)
