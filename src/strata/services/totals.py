"""Totals and subtotals: a report's ``total`` per column, ``subtotals_by`` and
``total_row`` (services/presentation), computed by DuckDB over every row that matches
the view's filters and search, never just the page (user decisions 2026-11-25).

- Subtotal rows are footers: no group heading rows; each row still shows its group's
  value. Rows sort by the group columns first (ascending unless the view sorts a group
  column the other way), then by the view's own sort.
- A page fetches one row more than it shows, to see where each group ends: a group's
  subtotal comes after its last row, on the page where it ends. The grand total comes
  after the last row of the last page.
- One ROLLUP query gives the subtotals of the groups on the page (restricted to their
  outermost values); the grand total is a query of its own. ``grouping()`` tells a
  group whose value is NULL from a rolled-up row.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import duckdb

from strata.services import query_filter
from strata.services.presentation import Presentation

_AGG_SQL = {
    "sum": "sum({})",
    "avg": "avg({})",
    "min": "min({})",
    "max": "max({})",
    "count": "count({})",
    "count_distinct": "count(DISTINCT {})",
}
_ROWS = "__strata_rows"  # each total row's number of rows (never a report's column name)


@dataclass(frozen=True)
class Shape:
    """What a report totals, among the columns its result has now (settings for a
    column it no longer has are skipped)."""

    groups: tuple[str, ...]  # subtotals_by
    totals: dict[str, str]  # column → function
    total_row: bool

    @property
    def active(self) -> bool:
        return bool(self.totals) and (bool(self.groups) or self.total_row)


def shape(look: Presentation | None, names: Sequence[str]) -> Shape:
    if look is None:
        return Shape((), {}, False)
    have = set(names)
    totals = {c.name: c.total for c in look.columns if c.total in _AGG_SQL and c.name in have}
    groups = tuple(g for g in look.subtotals_by if g in have and g not in totals)
    return Shape(groups, totals, look.total_row)


@dataclass
class TotalRow:
    """A subtotal (``level`` 1 = the outermost group …) or the grand total (level 0),
    shown after the data row ``after`` (its index on the page)."""

    after: int
    level: int
    key: tuple[Any, ...]  # the group's values, outermost first
    values: dict[str, Any] = field(default_factory=dict)  # column → its total
    rows: int = 0  # how many rows it totals
    label: str | None = None  # instead of "Total" (mail: "Total of all N rows")


def plan(
    rows: Sequence[Sequence[Any]],
    after_last: Sequence[Any] | None,
    group_index: Sequence[int],
    total_row: bool,
) -> list[TotalRow]:
    """Where the page's total rows go. ``after_last`` = the row after the page's last
    one (the look-ahead), None at the end of the data. ``group_index`` = each group
    column's position in a row."""
    out: list[TotalRow] = []
    depth = len(group_index)
    for i, row in enumerate(rows):
        nxt = rows[i + 1] if i + 1 < len(rows) else after_last
        if nxt is None:
            ended = 0  # the data ends: every group ends here
        else:
            ended = next((k for k, g in enumerate(group_index) if row[g] != nxt[g]), depth)
            if ended == depth:
                continue
        key = tuple(row[g] for g in group_index)
        for level in range(depth, ended, -1):  # the innermost first
            out.append(TotalRow(after=i, level=level, key=key[:level]))
        if nxt is None and total_row:
            out.append(TotalRow(after=i, level=0, key=()))
    return out


def _quote(name: str) -> str:
    return query_filter.quote_ident(name)


def _aggregates(s: Shape) -> str:
    parts = [f"{_AGG_SQL[fn].format(_quote(col))} AS {_quote(col)}" for col, fn in s.totals.items()]
    return ", ".join([*parts, f"count(*) AS {_quote(_ROWS)}"])


def fill(
    conn: duckdb.DuckDBPyConnection,
    final_sql: str,
    binds: list[Any],
    clauses: query_filter.Clauses,
    s: Shape,
    wanted: list[TotalRow],
) -> None:
    """Compute the totals of ``wanted`` (in place) over every row the view's filters
    match. DuckDB errors propagate."""
    names = [*s.totals, _ROWS]
    subtotals = [t for t in wanted if t.level > 0]
    if subtotals:
        groups = ", ".join(_quote(g) for g in s.groups)
        bits = ", ".join(f"grouping({_quote(g)})" for g in s.groups)
        outer = _quote(s.groups[0])
        values = sorted({t.key[0] for t in subtotals if t.key[0] is not None}, key=repr)
        within = [f"{outer} IN ({', '.join('?' for _ in values)})"] if values else []
        if any(t.key[0] is None for t in subtotals):
            within.append(f"{outer} IS NULL")
        restrict = "(" + " OR ".join(within) + ")"
        where = f"{clauses.where_sql} AND {restrict}" if clauses.where_sql else f" WHERE {restrict}"
        sql = (
            f"SELECT {groups}, {_aggregates(s)}, {bits} FROM ({final_sql}) AS _q{where} "
            f"GROUP BY ROLLUP ({groups})"
        )
        found: dict[tuple[int, tuple[Any, ...]], tuple[Any, ...]] = {}
        depth = len(s.groups)
        for row in conn.execute(sql, binds + clauses.binds + values).fetchall():
            level = sum(1 for b in row[-depth:] if b == 0)
            found[(level, tuple(row[:level]))] = row[depth : depth + len(names)]
        for t in subtotals:
            got = found.get((t.level, t.key))
            if got is not None:
                t.values = dict(zip(names[:-1], got[:-1], strict=True))
                t.rows = int(got[-1])
    grand = [t for t in wanted if t.level == 0]
    if grand:
        sql = f"SELECT {_aggregates(s)} FROM ({final_sql}) AS _q{clauses.where_sql}"
        got = conn.execute(sql, binds + clauses.binds).fetchone()
        if got is not None:
            for t in grand:
                t.values = dict(zip(names[:-1], got[:-1], strict=True))
                t.rows = int(got[-1])
