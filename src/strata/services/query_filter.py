"""Sort/filter/paginate grammar for the report serve path.

Query keys that are not declared report args are interpreted as result-set
operations over the macro's DESCRIBEd output columns:

    region=north            bare key: a declared arg wins, else an eq filter
    revenue__gte=1000       field__op filter (ops below)
    q=widget                substring search ORed across every column
    sort=-revenue,region    ORDER BY "revenue" DESC NULLS LAST, "region" ASC NULLS LAST
    page=2                  1-based page of PAGE_SIZE rows

Field names are validated against the real output columns (exact match first,
then a ``_``->space retry for hand-typed URLs) and emitted double-quoted;
values are always bound as parameters, cast in SQL to the column's DESCRIBEd
type. Nothing user-supplied is ever interpolated into SQL text.
"""

import logging
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# Query keys that are never args or filters. `format` and `key` are accepted
# and ignored for now (the URL extension is canonical; API keys are Phase 6).
RESERVED_KEYS = frozenset({"sort", "page", "q", "format", "key"})

PAGE_SIZE = 200

_COMPARE_SQL = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
_OPS = frozenset(_COMPARE_SQL) | {"in", "between", "contains", "isnull"}
_OP_LABEL = {
    "eq": "=",
    "ne": "≠",
    "gt": ">",
    "gte": "≥",
    "lt": "<",
    "lte": "≤",
    "in": "in",
    "contains": "contains",
}


class FilterError(ValueError):
    """A sort/filter/page/search parameter was invalid (-> 400 / inline flash)."""


@dataclass(frozen=True)
class RawFilter:
    key: str  # original query-string key, e.g. "revenue__gte" or "region"
    value: str


@dataclass(frozen=True)
class QuerySpec:
    """Parsed reserved/filter params, before column validation."""

    filters: list[RawFilter]
    q: list[str]
    sort_raw: list[str]
    page: int | None


@dataclass(frozen=True)
class AppliedFilter:
    """A filter resolved against real columns, for chip rendering."""

    key: str
    value: str
    field: str
    op: str
    label: str


@dataclass(frozen=True)
class SortTerm:
    field: str
    desc: bool


@dataclass
class Clauses:
    """SQL fragments + binds built from a QuerySpec over the DESCRIBEd columns."""

    where_sql: str = ""  # "" or " WHERE ..." (leading space included)
    order_sql: str = ""  # "" or " ORDER BY ..."
    binds: list[Any] = field(default_factory=list)
    filters: list[AppliedFilter] = field(default_factory=list)
    sort: list[SortTerm] = field(default_factory=list)


def parse_params(
    pairs: Iterable[tuple[str, str]], arg_names: Collection[str]
) -> tuple[dict[str, list[str]], QuerySpec]:
    """Partition query-string pairs into macro-arg values and a QuerySpec.

    ``pairs`` preserves order and repetition (request.args.items(multi=True)).
    Only ``page`` can fail here; field/op validation needs the DESCRIBE columns
    and happens in build_clauses.
    """
    shadowed = RESERVED_KEYS & set(arg_names)
    if shadowed:
        log.warning(
            "report args %s shadow reserved query keys and are unreachable; rename them",
            sorted(shadowed),
        )
    arg_values: dict[str, list[str]] = {}
    filters: list[RawFilter] = []
    q: list[str] = []
    sort_raw: list[str] = []
    page: int | None = None
    for key, value in pairs:
        if key in RESERVED_KEYS:
            if key == "q":
                if value:
                    q.append(value)
            elif key == "sort":
                sort_raw.extend(t for t in value.split(",") if t)
            elif key == "page" and page is None:
                try:
                    page = int(value)
                except ValueError:
                    raise FilterError(f"page must be a positive integer, not '{value}'") from None
                if page < 1:
                    raise FilterError("page must be a positive integer")
        elif key in arg_names:
            arg_values.setdefault(key, []).append(value)
        else:
            filters.append(RawFilter(key, value))
    return arg_values, QuerySpec(filters=filters, q=q, sort_raw=sort_raw, page=page)


def build_clauses(spec: QuerySpec, columns: list[tuple[str, str]]) -> Clauses:
    """Validate the spec against the DESCRIBEd (name, type) columns and emit SQL."""
    colmap: dict[str, str] = {}
    for name, ctype in columns:
        colmap.setdefault(name, ctype)  # keep-first on duplicate output names

    where_parts: list[str] = []
    binds: list[Any] = []
    applied: list[AppliedFilter] = []
    for raw in spec.filters:
        field_name, op = _split_key(raw.key, colmap)
        col = _resolve_field(field_name, colmap)
        sql, values = _op_sql(col, colmap[col], op, raw.value)
        where_parts.append(sql)
        binds.extend(values)
        applied.append(
            AppliedFilter(
                key=raw.key, value=raw.value, field=col, op=op, label=_label(col, op, raw.value)
            )
        )

    for fragment in spec.q:
        if not colmap:
            break
        pattern = "%" + _escape_like(fragment) + "%"
        ors = " OR ".join(
            f"CAST({_quote_ident(name)} AS VARCHAR) ILIKE ? ESCAPE '\\'" for name in colmap
        )
        where_parts.append(f"({ors})")
        binds.extend([pattern] * len(colmap))

    sort_terms: list[SortTerm] = []
    seen: set[str] = set()
    for token in spec.sort_raw:
        desc = token.startswith("-")
        col = _resolve_field(token[1:] if desc else token, colmap)
        if col in seen:
            continue
        seen.add(col)
        sort_terms.append(SortTerm(field=col, desc=desc))

    where_sql = " WHERE " + " AND ".join(where_parts) if where_parts else ""
    order_sql = (
        " ORDER BY "
        + ", ".join(
            f"{_quote_ident(t.field)} {'DESC' if t.desc else 'ASC'} NULLS LAST" for t in sort_terms
        )
        if sort_terms
        else ""
    )
    return Clauses(
        where_sql=where_sql, order_sql=order_sql, binds=binds, filters=applied, sort=sort_terms
    )


def wrap_select(
    final_sql: str, clauses: Clauses, limit: int | None = None, offset: int | None = None
) -> tuple[str, list[Any]]:
    """Wrap the macro call with WHERE/ORDER BY/LIMIT.

    Returned binds EXCLUDE the macro's own binds — callers prepend them
    (positional ``?`` placeholders bind in textual order).
    """
    if not clauses.where_sql and not clauses.order_sql and limit is None:
        return final_sql, []
    sql = f"SELECT * FROM ({final_sql}) AS _q{clauses.where_sql}{clauses.order_sql}"
    extra: list[Any] = list(clauses.binds)
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        extra += [limit, offset or 0]
    return sql, extra


def wrap_count(final_sql: str, clauses: Clauses) -> tuple[str, list[Any]]:
    """COUNT(*) over the filtered (unsorted, unpaginated) result, for the pager."""
    return f"SELECT count(*) FROM ({final_sql}) AS _q{clauses.where_sql}", list(clauses.binds)


def _split_key(key: str, colmap: dict[str, str]) -> tuple[str, str]:
    """(field, op) for a filter key: an exact column match wins over op-splitting,
    so a column literally named ``a__in`` stays reachable (as a bare eq)."""
    if key in colmap:
        return key, "eq"
    if "__" in key:
        field_name, op = key.rsplit("__", 1)
        if not field_name:
            raise FilterError(f"Malformed filter '{key}'")
        if op not in _OPS:
            raise FilterError(f"Unknown filter operator '{op}' in '{key}'")
        return field_name, op
    return key, "eq"


def _resolve_field(name: str, colmap: dict[str, str]) -> str:
    """Exact column match first; a ``_``->space retry only when exact misses."""
    if name in colmap:
        return name
    if "_" in name:
        spaced = name.replace("_", " ")
        if spaced in colmap:
            return spaced
    raise FilterError(f"Unknown field '{name}'")


def _op_sql(col: str, ctype: str, op: str, raw: str) -> tuple[str, list[Any]]:
    """One WHERE clause for a resolved column. ``ctype`` comes from DESCRIBE
    (never user input); values are always bound."""
    qi = _quote_ident(col)
    cast = f"CAST(? AS {ctype})"
    if op in _COMPARE_SQL:
        return f"{qi} {_COMPARE_SQL[op]} {cast}", [raw]
    if op == "in":
        parts = raw.split(",")
        return f"{qi} IN ({', '.join([cast] * len(parts))})", list(parts)
    if op == "between":
        parts = raw.split(",")
        if len(parts) != 2:
            raise FilterError("'between' requires exactly two comma-separated values")
        return f"{qi} BETWEEN {cast} AND {cast}", list(parts)
    if op == "contains":
        return (
            f"CAST({qi} AS VARCHAR) ILIKE ? ESCAPE '\\'",
            ["%" + _escape_like(raw) + "%"],
        )
    # isnull
    return f"{qi} IS {'NULL' if _is_true(raw) else 'NOT NULL'}", []


def _label(col: str, op: str, raw: str) -> str:
    if op == "isnull":
        return f"{col} is null" if _is_true(raw) else f"{col} is not null"
    if op == "between":
        return f"{col} between {raw.replace(',', ' and ', 1)}"
    return f"{col} {_OP_LABEL[op]} {raw}"


def _is_true(raw: str) -> bool:
    """Boolean-ish parse where bare/empty means true (``?col__isnull=``)."""
    return raw.lower() in ("", "true", "1", "yes", "on")


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _escape_like(value: str) -> str:
    """Escape LIKE wildcards in a user fragment (backslash first)."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
