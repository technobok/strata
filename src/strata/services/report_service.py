"""Serve a report macro over a datamodule's live version.

The report body (``report.body_sql``) is the source of truth in SQLite. At serve
time we ensure the datamodule is fresh (TTL pull), open its live version file
read-only, register the report as a ``TEMP`` macro (so editing a body reflects
immediately with no rebuild), bind the request args, ``DESCRIBE`` to learn the
real output columns, validate any sort/filter/search params against those
columns (services/query_filter), and either render one HTML page of rows or
stream the full filtered result.

Authors reference args in the body with the familiar ``$arg`` syntax; we rewrite
``$arg`` -> the macro parameter ``arg`` (skipping occurrences inside string /
identifier literals and comments via template_service._strip_literals).
"""

import logging
from dataclasses import dataclass, field
from typing import Any

import duckdb
from flask import Response

from strata.models.datamodule import Datamodule
from strata.models.report import Report
from strata.models.report_arg import ReportArg
from strata.services import datamodule_service, query_filter, stream_service, template_service

log = logging.getLogger(__name__)

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


def _bind_args(args: list[ReportArg], arg_values: dict[str, list[str]]) -> list[Any]:
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


def _prepare(
    report: Report, dm: Datamodule, arg_values: dict[str, list[str]]
) -> tuple[duckdb.DuckDBPyConnection, str, list[Any], list[tuple[str, str]]]:
    """Ensure fresh, attach live version read-only, register the macro, describe.

    Returns (conn, final_sql, binds, columns) where columns is the DESCRIBEd
    (name, type) per output column. Caller owns closing ``conn`` (stream_service
    closes it after writing; run_table closes it after fetch).
    """
    if not template_service.ALIAS_RE.match(report.macro_name):
        raise ArgError(f"Invalid macro name '{report.macro_name}'")
    args = ReportArg.get_for_report(report.id)
    for arg in args:
        if not template_service.ALIAS_RE.match(arg.name):
            raise ArgError(f"Invalid argument name '{arg.name}'")

    effective_ttl = report.ttl_seconds if report.ttl_seconds is not None else dm.ttl_seconds
    version = datamodule_service.ensure_fresh(dm, "ttl-pull", effective_ttl)
    vpath = datamodule_service.version_path(dm.uuid, version.uuid)

    binds = _bind_args(args, arg_values)

    conn = duckdb.connect(":memory:")
    try:
        conn.execute(f"ATTACH '{vpath}' AS d (READ_ONLY)")
        conn.execute("USE d")
        params = ", ".join(f"{_PARAM_PREFIX}{arg.name}" for arg in args)
        body = _rewrite_body(report.body_sql)
        conn.execute(
            f"CREATE OR REPLACE TEMP MACRO {report.macro_name}({params}) AS TABLE ({body})"
        )
        # multiselect binds a LIST: give it an explicit element type so an empty
        # selection still types correctly.
        placeholders = ", ".join(
            f"CAST(? AS {_DUCK_LIST_TYPE.get(arg.data_type, 'VARCHAR')}[])"
            if arg.widget == "multiselect"
            else "?"
            for arg in args
        )
        call = f"{report.macro_name}({placeholders})"
        described = conn.execute(f"DESCRIBE SELECT * FROM {call}", binds).fetchall()
        columns = [(str(row[0]), str(row[1])) for row in described]
        final_sql = f"SELECT * FROM {call}"
    except Exception:
        conn.close()
        raise
    return conn, final_sql, binds, columns


def run_table(
    report: Report,
    dm: Datamodule,
    arg_values: dict[str, list[str]],
    spec: query_filter.QuerySpec,
) -> TableResult:
    """Run the report and return one page of rows for the HTML table.

    ArgError and FilterError propagate (rendered inline by the blueprint).
    DuckDB errors — a bad macro body or an uncastable filter value — are
    captured into the result, which still carries the applied filters so a
    failing filter chip stays visible and removable.
    """
    try:
        conn, final_sql, binds, columns = _prepare(report, dm, arg_values)
    except duckdb.Error as exc:
        return TableResult(error=f"Query error: {exc}")
    try:
        clauses = query_filter.build_clauses(spec, columns)
    except query_filter.FilterError:
        conn.close()
        raise
    col_names = [name for name, _ in columns]
    page = spec.page or 1
    try:
        # One extra count execution per HTML view; the datamodule is a local
        # DuckDB file, so this is cheap relative to rendering.
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
        row_count=len(rows),
        total_rows=total,
        page=page,
        page_count=page_count,
        applied_filters=clauses.filters,
        applied_sort=clauses.sort,
    )


def stream(
    report: Report,
    dm: Datamodule,
    arg_values: dict[str, list[str]],
    spec: query_filter.QuerySpec,
    fmt: str,
) -> Response:
    """Stream the filtered/sorted result as parquet/csv/json/xlsx.

    Exports cover the full result; an explicit ``page=N`` from the caller
    yields just that slice (UI export links never carry ``page``).
    """
    conn, final_sql, binds, columns = _prepare(report, dm, arg_values)
    try:
        clauses = query_filter.build_clauses(spec, columns)
        limit = offset = None
        if spec.page is not None:
            limit = query_filter.PAGE_SIZE
            offset = (spec.page - 1) * query_filter.PAGE_SIZE
        select_sql, extra = query_filter.wrap_select(final_sql, clauses, limit=limit, offset=offset)
        download_name = f"{report.macro_name}.{fmt}"
        if fmt == "xlsx":
            return stream_service.stream_xlsx(
                conn, select_sql, binds + extra, download_name, [name for name, _ in columns]
            )
        if fmt in stream_service.COPY_FORMATS:
            return stream_service.stream_copy(conn, select_sql, binds + extra, fmt, download_name)
        raise ArgError(f"Unsupported format '{fmt}'")
    except Exception:
        conn.close()  # idempotent — the stream writers close on their own paths
        raise


# Constraint-query options, memoised per (arg_id, live_version_id). Editing a
# report replaces its arg rows (new ids) and a rebuild yields a new version id,
# so stale entries simply fall out of use.
_options_cache: dict[tuple[int, int], list[tuple[str, str]]] = {}


def arg_options_map(
    report: Report, dm: Datamodule, args: list[ReportArg]
) -> dict[int, list[tuple[str, str]]]:
    """Options for select/multiselect args, from the datamodule's *current* live version.

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
                conn = duckdb.connect(":memory:")
                conn.execute(f"ATTACH '{vpath}' AS d (READ_ONLY)")
                conn.execute("USE d")
            try:
                rows = conn.execute(arg.constraint_query).fetchall()
                opts = [
                    (str(r[0]), str(r[1]) if len(r) > 1 and r[1] is not None else str(r[0]))
                    for r in rows
                    if r[0] is not None
                ]
            except duckdb.Error as exc:
                log.warning("constraint_query for arg '%s' failed: %s", arg.name, exc)
                opts = []
            _options_cache[key] = opts
            result[arg.id] = opts
    finally:
        if conn is not None:
            conn.close()
    return result
