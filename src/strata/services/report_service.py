"""Serve a report macro over a datamodule's live version.

The report body (``report.body_sql``) is the source of truth in SQLite. At serve
time the blueprint picks the version to serve (build_queue.servable_version:
the serve path never builds; it waits a bounded time for the worker), we open
that version file read-only, register the report as a ``TEMP`` macro (so editing a body reflects
immediately with no rebuild), bind the request args, ``DESCRIBE`` to learn the
real output columns, validate any sort/filter/search params against those
columns (services/query_filter), and either render one HTML page of rows or
stream the full filtered result.

Authors reference args in the body with the familiar ``$arg`` syntax; we rewrite
``$arg`` -> the macro parameter ``arg`` (skipping occurrences inside string /
identifier literals and comments via template_service._strip_literals).
"""

import functools
import logging
import os
import re
import tempfile
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import duckdb
from flask import Response

from strata.models.datamodule import Datamodule
from strata.models.datamodule_version import DatamoduleVersion
from strata.models.report import Report
from strata.models.report_arg import ReportArg
from strata.services import (
    datamodule_service,
    duck,
    presentation,
    query_filter,
    stream_service,
    table_view,
    template_service,
)
from strata.services.presentation import Presentation

log = logging.getLogger(__name__)


MACRO_MAX = 40  # longest generated macro name (the xlsx sheet title keeps 31)


@functools.cache
def _duckdb_words() -> frozenset[str]:
    """DuckDB's function names and keywords: a report macro named like one would
    shadow it in its own body (a macro ``range`` breaks ``range(5)``) or not parse."""
    conn = duckdb.connect(":memory:")
    try:
        rows = conn.execute(
            "SELECT function_name FROM duckdb_functions() "
            "UNION SELECT keyword_name FROM duckdb_keywords()"
        ).fetchall()
    finally:
        conn.close()
    return frozenset(str(row[0]).lower() for row in rows)


def macro_name_for(name: str, taken: Iterable[str]) -> str:
    """A report's macro name, made from its name: lower case, ASCII letters, digits
    and ``_`` (``Bad item — département`` → ``bad_item_departement``); never a DuckDB
    function or keyword, never one in ``taken`` (the datamodule's other reports).
    It is made once: renaming the report keeps it, as workbooks select its xlsx
    sheet and table by it."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-z0-9]+", "_", ascii_name.lower()).strip("_")[:MACRO_MAX].strip("_")
    if not base or base[0].isdigit():
        base = f"report_{base}".rstrip("_")
    if base in _duckdb_words():
        base = f"{base}_report"
    used = {t.lower() for t in taken}
    candidate, n = base, 2
    while candidate in used:
        candidate, n = f"{base}_{n}", n + 1
    return candidate


class ArgLike(Protocol):
    """What binding a report reads of an argument: a saved ``ReportArg``, or a
    row of the report form before it is saved (``ArgShape``)."""

    @property
    def name(self) -> str: ...
    @property
    def data_type(self) -> str: ...
    @property
    def widget(self) -> str: ...
    @property
    def default_value(self) -> str | None: ...
    @property
    def required(self) -> bool: ...
    @property
    def constraint_query(self) -> str | None: ...


@dataclass(frozen=True)
class ArgShape:
    """An argument as the report form gives it, before it is saved."""

    name: str
    data_type: str
    widget: str
    default_value: str | None
    required: bool
    constraint_query: str | None


# report_arg.data_type -> template_service.cast_value type name.
_CAST_TYPE = {
    "text": "string",
    "integer": "integer",
    "number": "float",
    "decimal": "decimal",
    "date": "date",
    "time": "time",
    "boolean": "boolean",
}

# report_arg.data_type -> DuckDB scalar type, used to give a multiselect LIST bind
# an explicit element type (so CAST(? AS T[]) works even for an empty selection).
_DUCK_LIST_TYPE = {
    "text": "VARCHAR",
    "integer": "BIGINT",
    "number": "DOUBLE",
    "decimal": "DOUBLE",
    "date": "DATE",
    "time": "TIME",
    "boolean": "BOOLEAN",
}


class ArgError(Exception):
    """A request argument was missing or could not be cast (-> HTTP 400)."""


@dataclass
class TableResult:
    columns: list[str] = field(default_factory=list)
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    row_count: int = 0  # rows on THIS page
    total_rows: int = 0  # filtered count across all pages
    page: int = 1
    page_count: int = 1
    error: str | None = None
    applied_filters: list[query_filter.AppliedFilter] = field(default_factory=list)
    applied_sort: list[query_filter.SortTerm] = field(default_factory=list)
    view: table_view.TableView | None = None  # the shown cells (None on an error)


def resolve(report_uuid: str) -> tuple[Report, Datamodule] | None:
    report = Report.get_by_uuid(report_uuid)
    if report is None:
        return None
    dm = Datamodule.get_by_id(report.datamodule_id)
    if dm is None:
        return None
    return report, dm


# Macro parameters are prefixed so they can never collide with a column of the
# same name: an author filtering `WHERE region = $region` must bind the param,
# not compare the column to itself. `$region` -> `arg_region`; the registered
# macro declares `arg_region` as its parameter.
_PARAM_PREFIX = "arg_"


def _rewrite_body(body: str) -> str:
    """Rewrite ``$arg`` bind syntax to the prefixed macro-parameter reference."""
    scrubbed = template_service._strip_literals(body)
    out: list[str] = []
    last = 0
    for match in template_service._BIND_PARAM_RE.finditer(scrubbed):
        start, end = match.span()
        out.append(body[last:start])
        out.append(_PARAM_PREFIX + match.group(1))  # $name -> arg_name
        last = end
    out.append(body[last:])
    return "".join(out)


def _bind_args(args: Sequence[ArgLike], arg_values: dict[str, list[str]]) -> list[Any]:
    """Bind request values to macro args (aligned with ``args``).

    Query-string keys may repeat, so values arrive as lists. A ``multiselect``
    arg binds a Python list (a DuckDB LIST); every other widget binds a single
    cast value (or NULL when optional and absent).
    """
    binds: list[Any] = []
    for arg in args:
        raw_list = arg_values.get(arg.name) or []
        cast_type = _CAST_TYPE.get(arg.data_type, "string")

        if arg.widget == "multiselect":
            selected = [v for v in raw_list if v != ""]
            if not selected and arg.default_value:
                selected = [v.strip() for v in arg.default_value.split(",") if v.strip()]
            if not selected and arg.required:
                raise ArgError(f"Argument '{arg.name}' requires at least one selection")
            try:
                binds.append([template_service.cast_value(v, cast_type) for v in selected])
            except (ValueError, TypeError) as exc:
                raise ArgError(f"Argument '{arg.name}': {exc}") from exc
            continue

        raw = raw_list[0] if raw_list else None
        if (raw is None or raw == "") and arg.default_value:
            raw = arg.default_value
        if raw is None or raw == "":
            if arg.required:
                raise ArgError(f"Argument '{arg.name}' is required")
            binds.append(None)
            continue
        try:
            binds.append(template_service.cast_value(raw, cast_type))
        except (ValueError, TypeError) as exc:
            raise ArgError(f"Argument '{arg.name}': {exc}") from exc
    return binds


def check_args(args: list[ReportArg], arg_values: dict[str, list[str]]) -> None:
    """Raise ArgError if ``arg_values`` (with the defaults) cannot run the report."""
    _bind_args(args, arg_values)


def _prepare(
    report: Report,
    dm: Datamodule,
    arg_values: dict[str, list[str]],
    version: DatamoduleVersion,
) -> tuple[duckdb.DuckDBPyConnection, str, list[Any], list[tuple[str, str]]]:
    """Attach ``version`` read-only, register the macro, describe.

    ``version`` is served as-is (the live one the blueprint resolved, or a
    pinned historical one); nothing here builds. Returns (conn, final_sql, binds, columns)
    where columns is the DESCRIBEd (name, type) per output column. Caller owns
    closing ``conn`` (stream_service closes it after writing; run_table closes
    it after fetch).
    """
    if not template_service.ALIAS_RE.match(report.macro_name):
        raise ArgError(f"Invalid macro name '{report.macro_name}'")
    args = ReportArg.get_for_report(report.id)
    for arg in args:
        if not template_service.ALIAS_RE.match(arg.name):
            raise ArgError(f"Invalid argument name '{arg.name}'")

    vpath = datamodule_service.version_path(dm.uuid, version.uuid)

    binds = _bind_args(args, arg_values)
    try:
        duck.validate_select(report.body_sql, "The report body")
    except duck.SqlShapeError as exc:
        raise ArgError(str(exc)) from exc

    conn = duck.serve_conn(vpath)
    try:
        call = _register_macro(conn, report.macro_name, report.body_sql, args)
        described = conn.execute(f"DESCRIBE SELECT * FROM {call}", binds).fetchall()
        columns = [(str(row[0]), str(row[1])) for row in described]
        final_sql = f"SELECT * FROM {call}"
    except Exception:
        conn.close()
        raise
    return conn, final_sql, binds, columns


def _register_macro(
    conn: duckdb.DuckDBPyConnection,
    macro_name: str,
    body_sql: str,
    args: Sequence[ArgLike],
    typed: frozenset[str] = frozenset(),
) -> str:
    """Register a report body as a TEMP macro on ``conn``; return its call with one
    ``?`` per argument (bind them with ``_bind_args``). An argument named in
    ``typed`` binds as its own type (``CAST(? AS DATE)``), so a NULL there types
    like a real value would."""
    params = ", ".join(f"{_PARAM_PREFIX}{arg.name}" for arg in args)
    body = _rewrite_body(body_sql)
    conn.execute(f"CREATE OR REPLACE TEMP MACRO {macro_name}({params}) AS TABLE ({body})")

    # multiselect binds a LIST: give it an explicit element type so an empty
    # selection still types correctly.
    def placeholder(arg: ArgLike) -> str:
        duck_type = _DUCK_LIST_TYPE.get(arg.data_type, "VARCHAR")
        if arg.widget == "multiselect":
            return f"CAST(? AS {duck_type}[])"
        return f"CAST(? AS {duck_type})" if arg.name in typed else "?"

    return f"{macro_name}({', '.join(placeholder(arg) for arg in args)})"


def link_args(args: Sequence[ArgLike], given: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Every argument of a report as query pairs, in the report's order: the value(s)
    ``given`` (a view's), else its default (a multiselect's comma-separated default
    is one pair per value), else empty. An Excel link names every argument, so a
    workbook's owner can change one in the link (in Power Query) without knowing its
    name."""
    pairs: list[tuple[str, str]] = []
    for arg in args:
        values = [v for k, v in given if k == arg.name]
        if not values:
            default = arg.default_value or ""
            values = (
                [v.strip() for v in default.split(",") if v.strip()]
                if arg.widget == "multiselect" and default
                else [default]
            )
        pairs.extend((arg.name, v) for v in values)
    return pairs


@dataclass
class Described:
    """A report described on a version file: its (name, DuckDB type) columns, or
    ``problem`` when it does not run; ``rule_problems`` = style rule number (from 1) →
    why its condition does not bind."""

    columns: list[tuple[str, str]] = field(default_factory=list)
    problem: str | None = None
    rule_problems: dict[int, str] = field(default_factory=dict)


def problem_on(vpath: Path, macro_name: str, body_sql: str, args: Sequence[ArgLike]) -> str | None:
    """Why a report would not run on the version file ``vpath``; None when it would."""
    return describe_on(vpath, macro_name, body_sql, args).problem


def describe_on(
    vpath: Path,
    macro_name: str,
    body_sql: str,
    args: Sequence[ArgLike],
    rules: Sequence[str] = (),
) -> Described:
    """Describe a report on the version file ``vpath``, and bind its style ``rules``
    (DuckDB conditions on a row) against its columns.

    DuckDB describes the report without reading any rows, so this is cheap and
    finds what a read would hit first: a missing table or column, a type error, an
    argument default that is not of its type, a broken option (constraint) query.
    Arguments bind as a read with none given would (their defaults; NULL for an
    optional one); a required one without a default binds a NULL of its type, as
    nobody can read the report without giving it.
    """
    try:
        binds: list[Any] = []
        typed: set[str] = set()
        for arg in args:
            if arg.required and not arg.default_value:
                binds.append([] if arg.widget == "multiselect" else None)
                typed.add(arg.name)
            else:
                binds.extend(_bind_args([arg], {}))
    except ArgError as exc:
        return Described(problem=str(exc))
    try:
        conn = duck.serve_conn(vpath)
    except duckdb.Error as exc:
        return Described(problem=str(exc))
    try:
        call = _register_macro(conn, macro_name, body_sql, args, frozenset(typed))
        described = conn.execute(f"DESCRIBE SELECT * FROM {call}", binds).fetchall()
        out = Described(columns=[(str(r[0]), str(r[1])) for r in described])
        for arg in args:
            if arg.constraint_query:
                try:
                    conn.execute(f"DESCRIBE {arg.constraint_query}").fetchall()
                except duckdb.Error as exc:
                    return Described(problem=f"The constraint query of '{arg.name}': {exc}")
        for n, when in enumerate(rules, 1):
            if not when.strip():
                continue  # presentation.validate says a rule needs a condition
            problem = _rule_problem(conn, call, binds, when)
            if problem is not None:
                out.rule_problems[n] = problem
        return out
    except duckdb.Error as exc:
        return Described(problem=str(exc))
    finally:
        conn.close()


def rule_sql(when: str) -> str:
    """A style rule's condition as the flag column the page query selects: true or
    false for every row (NULL counts as false)."""
    return f"coalesce(({when}), false)"


def _rule_problem(
    conn: duckdb.DuckDBPyConnection, call: str, binds: list[Any], when: str
) -> str | None:
    """Why a style rule's condition does not bind on the report's rows, or is not a
    true/false condition; None when it is one. Same trust as the report body: an
    owner's SQL on the read-only, locked-down serve connection."""
    sql = f"SELECT ({when}) AS __rule FROM (SELECT * FROM {call}) AS _q"
    try:
        duck.validate_select(sql, "The condition")
    except duck.SqlShapeError as exc:
        if "does not parse" in str(exc):
            return str(exc)
        return 'a rule is one condition on the row, e.g. "Amount" < 0'
    try:
        rows = conn.execute(f"DESCRIBE {sql}", binds).fetchall()
    except duckdb.Error as exc:
        return str(exc)
    if len(rows) != 1:
        return 'a rule is one condition on the row, e.g. "Amount" < 0'
    if str(rows[0][1]) != "BOOLEAN":
        return f'a rule must be true or false, e.g. "Amount" < 0 (this one gives {rows[0][1]})'
    return None


def check_reports(dm: Datamodule, version: DatamoduleVersion) -> list[tuple[Report, str]]:
    """Describe every report of ``dm`` against ``version`` (its new live data) and
    record each one's problem. Returns the reports that ran before and do not now
    (newly broken), with their problems."""
    vpath = datamodule_service.version_path(dm.uuid, version.uuid)
    newly: list[tuple[Report, str]] = []
    for report in Report.get_for_datamodule(dm.id):
        if report.kind != "query":
            continue
        args = ReportArg.get_for_report(report.id)
        problem = problem_on(vpath, report.macro_name, report.body_sql, args)
        if problem != report.problem:
            Report.set_problem(report.id, problem)
            if problem is not None and report.problem is None:
                newly.append((report, problem))
    return newly


def run_table(
    report: Report,
    dm: Datamodule,
    arg_values: dict[str, list[str]],
    spec: query_filter.QuerySpec,
    version: DatamoduleVersion,
) -> TableResult:
    """Run the report and return one page of rows for the HTML table.

    ArgError and FilterError propagate (rendered inline by the blueprint).
    DuckDB errors — a bad macro body or an uncastable filter value — are
    captured into the result, which still carries the applied filters so a
    failing filter chip stays visible and removable.
    """
    try:
        conn, final_sql, binds, columns = _prepare(report, dm, arg_values, version)
    except duckdb.Error as exc:
        return TableResult(error=f"Query error: {exc}")
    return page_of(
        conn, final_sql, binds, columns, spec, presentation.of(report), table_view.Links.here()
    )


def page_of(
    conn: duckdb.DuckDBPyConnection,
    final_sql: str,
    binds: list[Any],
    columns: list[tuple[str, str]],
    spec: query_filter.QuerySpec,
    look: Presentation | None = None,
    links: table_view.Links | None = None,
) -> TableResult:
    """One page of ``final_sql`` with the spec's filters/search/sort applied, its
    cells rendered with ``look`` (the report's settings) and ``links``.

    Closes ``conn``. FilterError propagates; DuckDB errors are captured into the
    result (it keeps the applied filters so a failing chip stays removable).
    """
    try:
        clauses = query_filter.build_clauses(spec, columns, hidden_columns(look))
    except query_filter.FilterError:
        conn.close()
        raise
    col_names = [name for name, _ in columns]
    page = spec.page or 1
    try:
        # One extra count execution per HTML view; the data is a local DuckDB or
        # Parquet file, so this is cheap relative to rendering.
        count_sql, count_binds = query_filter.wrap_count(final_sql, clauses)
        count_row = conn.execute(count_sql, binds + count_binds).fetchone()
        total = int(count_row[0]) if count_row else 0
        select_sql, extra = query_filter.wrap_select(
            final_sql,
            clauses,
            limit=query_filter.PAGE_SIZE,
            offset=(page - 1) * query_filter.PAGE_SIZE,
        )
        rows = conn.execute(select_sql, binds + extra).fetchall()
    except duckdb.Error as exc:
        return TableResult(
            error=f"Query error: {exc}",
            columns=col_names,
            page=page,
            applied_filters=clauses.filters,
            applied_sort=clauses.sort,
        )
    finally:
        conn.close()
    page_count = max(1, (total + query_filter.PAGE_SIZE - 1) // query_filter.PAGE_SIZE)
    return TableResult(
        columns=col_names,
        rows=rows,
        view=table_view.render(columns, rows, look, links),
        row_count=len(rows),
        total_rows=total,
        page=page,
        page_count=page_count,
        applied_filters=clauses.filters,
        applied_sort=clauses.sort,
    )


def hidden_columns(look: Presentation | None) -> frozenset[str]:
    return frozenset(c.name for c in look.columns if c.hidden) if look else frozenset()


def xlsx_styles(
    columns: list[tuple[str, str]], look: Presentation | None
) -> list[stream_service.XlsxStyle]:
    """Each column's Excel number format (the report's format code, when it fits the
    column) and horizontal alignment (None = Excel's own)."""
    out: list[stream_service.XlsxStyle] = []
    for name, duck_type in columns:
        setting = look.setting(name) if look else None
        fits = setting is not None and table_view.formatter_for(setting, duck_type) is not None
        code = setting.format if setting is not None and fits else None
        if code is not None and code.strip().lower() == "general":
            code = None
        align = setting.align if setting is not None and setting.align != "auto" else None
        out.append(stream_service.XlsxStyle(number_format=code, horizontal=align))
    return out


def stream(
    report: Report,
    dm: Datamodule,
    arg_values: dict[str, list[str]],
    spec: query_filter.QuerySpec,
    fmt: str,
    version: DatamoduleVersion,
) -> Response:
    """Stream the filtered/sorted result as parquet/csv/json/xlsx.

    Exports cover the full result; an explicit ``page=N`` from the caller
    yields just that slice (UI export links never carry ``page``).
    """
    conn, final_sql, binds, columns = _prepare(report, dm, arg_values, version)
    try:
        return stream_rows(
            conn,
            final_sql,
            binds,
            columns,
            spec,
            fmt,
            report.macro_name,
            look=presentation.of(report),
        )
    except Exception:
        conn.close()  # idempotent — the stream writers close on their own paths
        raise


def stream_rows(
    conn: duckdb.DuckDBPyConnection,
    final_sql: str,
    binds: list[Any],
    columns: list[tuple[str, str]],
    spec: query_filter.QuerySpec,
    fmt: str,
    name: str,
    sheet: str | None = None,
    look: Presentation | None = None,
) -> Response:
    """Stream ``final_sql`` (spec applied) as ``fmt`` as the file ``<name>.<fmt>`` (an
    xlsx's sheet and table are named after ``sheet``, default ``name``); the writer
    closes ``conn``. A download keeps every column, hidden ones too, and has no links;
    an xlsx carries the formats and alignment of ``look`` (the report's settings).

    Exports cover the full result; an explicit ``page=N`` yields just that slice.
    """
    clauses = query_filter.build_clauses(spec, columns, hidden_columns(look))
    limit = offset = None
    if spec.page is not None:
        limit = query_filter.PAGE_SIZE
        offset = (spec.page - 1) * query_filter.PAGE_SIZE
    select_sql, extra = query_filter.wrap_select(final_sql, clauses, limit=limit, offset=offset)
    download_name = f"{name}.{fmt}"
    if fmt == "xlsx":
        return stream_service.stream_xlsx(
            conn,
            select_sql,
            binds + extra,
            download_name,
            [n for n, _ in columns],
            sheet,
            xlsx_styles(columns, look),
        )
    if fmt in stream_service.COPY_FORMATS:
        return stream_service.stream_copy(conn, select_sql, binds + extra, fmt, download_name)
    raise ArgError(f"Unsupported format '{fmt}'")


def snapshot(
    report: Report,
    dm: Datamodule,
    arg_values: dict[str, list[str]],
    version: DatamoduleVersion,
    dest: Path,
) -> int:
    """Run the report on ``version`` and save every row to ``dest`` (Parquet).

    The serve sandbox only writes under the exports directory, so the COPY goes
    there and the finished file is renamed into place (same filesystem).
    Returns the row count. ArgError and DuckDB errors propagate.
    """
    conn, final_sql, binds, _columns = _prepare(report, dm, arg_values, version)
    options, _content_type = stream_service.COPY_FORMATS["parquet"]
    fd, tmp_path = tempfile.mkstemp(suffix=".parquet", dir=duck.exports_dir())
    os.close(fd)
    try:
        row = conn.execute(f"COPY ({final_sql}) TO '{tmp_path}' {options}", binds).fetchone()
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp_path, dest)
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise
    finally:
        conn.close()
    return int(row[0]) if row else 0


def gate_rows(
    conn: duckdb.DuckDBPyConnection, report: Report, sample: int = 3
) -> tuple[int, list[tuple[Any, ...]]]:
    """Evaluate a gate check on a build connection (the new data, before the swap),
    with its arguments' defaults. Returns (row count, the first ``sample`` rows).

    ArgError (a required argument without a default) and DuckDB errors propagate.
    """
    duck.validate_select(report.body_sql, f"The body of check '{report.name}'")
    args = ReportArg.get_for_report(report.id)
    binds = _bind_args(args, {})
    call = _register_macro(conn, report.macro_name, report.body_sql, args)
    row = conn.execute(f"SELECT count(*) FROM {call}", binds).fetchone()
    count = int(row[0]) if row else 0
    rows = (
        conn.execute(f"SELECT * FROM {call} LIMIT {int(sample)}", binds).fetchall() if count else []
    )
    return count, rows


# Constraint-query options, memoised per (arg_id, live_version_id). Editing a
# report replaces its arg rows (new ids) and a rebuild yields a new version id,
# so stale entries simply fall out of use.
_options_cache: dict[tuple[int, int], list[tuple[str, str]]] = {}


def arg_options_map(
    report: Report, dm: Datamodule, args: list[ReportArg]
) -> dict[int, list[tuple[str, str]]]:
    """Options for select/multiselect args, from the datamodule's *current* live version.

    Deliberately live-based even when a historical version is pinned — options
    are form affordances, memoised per live version id.
    Uses the live version as-is (never triggers a build), so a form GET is cheap.
    Each option is (value, label): a 1-column constraint_query yields (v, v); ≥2
    columns yield (value, label). No live version → empty map.
    """
    version = datamodule_service.live_version(dm)
    if version is None:
        return {}
    vpath = datamodule_service.version_path(dm.uuid, version.uuid)
    result: dict[int, list[tuple[str, str]]] = {}
    conn: duckdb.DuckDBPyConnection | None = None
    try:
        for arg in args:
            if arg.widget not in ("select", "multiselect") or not arg.constraint_query:
                continue
            key = (arg.id, version.id)
            cached = _options_cache.get(key)
            if cached is not None:
                result[arg.id] = cached
                continue
            if conn is None:
                conn = duck.serve_conn(vpath)
            try:
                duck.validate_select(arg.constraint_query, "The constraint query")
                rows = conn.execute(arg.constraint_query).fetchall()
                opts = [
                    (str(r[0]), str(r[1]) if len(r) > 1 and r[1] is not None else str(r[0]))
                    for r in rows
                    if r[0] is not None
                ]
            except (duckdb.Error, duck.SqlShapeError) as exc:
                log.warning("constraint_query for arg '%s' failed: %s", arg.name, exc)
                opts = []
            _options_cache[key] = opts
            result[arg.id] = opts
    finally:
        if conn is not None:
            conn.close()
    return result
