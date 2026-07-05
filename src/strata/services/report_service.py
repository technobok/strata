"""Serve a report macro over a datamodule's live version.

The report body (``report.body_sql``) is the source of truth in SQLite. At serve
time we ensure the datamodule is fresh (TTL pull), open its live version file
read-only, register the report as a ``TEMP`` macro (so editing a body reflects
immediately with no rebuild), bind the request args, ``DESCRIBE`` to learn the
real output columns, and either render an HTML table or stream the result.

Authors reference args in the body with the familiar ``$arg`` syntax; we rewrite
``$arg`` -> the macro parameter ``arg`` (skipping occurrences inside string /
identifier literals and comments via template_service._strip_literals).
"""

import logging
from dataclasses import dataclass, field
from typing import Any

import duckdb

from strata.models.datamodule import Datamodule
from strata.models.report import Report
from strata.models.report_arg import ReportArg
from strata.services import datamodule_service, stream_service, template_service

log = logging.getLogger(__name__)

# report_arg.data_type -> template_service.cast_value type name.
_CAST_TYPE = {
    "text": "string",
    "integer": "integer",
    "number": "float",
    "decimal": "decimal",
    "date": "date",
    "time": "string",  # Phase 3 extends cast_value with a real time type
    "boolean": "boolean",
}


class ArgError(Exception):
    """A request argument was missing or could not be cast (-> HTTP 400)."""


@dataclass
class TableResult:
    columns: list[str] = field(default_factory=list)
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    row_count: int = 0
    error: str | None = None


def resolve(report_uuid: str) -> tuple[Report, Datamodule] | None:
    report = Report.get_by_uuid(report_uuid)
    if report is None:
        return None
    dm = Datamodule.get_by_id(report.datamodule_id)
    if dm is None:
        return None
    return report, dm


def _rewrite_body(body: str) -> str:
    """Replace ``$arg`` bind syntax with bare macro-parameter references."""
    scrubbed = template_service._strip_literals(body)
    out: list[str] = []
    last = 0
    for match in template_service._BIND_PARAM_RE.finditer(scrubbed):
        start, end = match.span()
        out.append(body[last:start])
        out.append(match.group(1))  # the name without the leading '$'
        last = end
    out.append(body[last:])
    return "".join(out)


def _bind_args(args: list[ReportArg], arg_values: dict[str, str]) -> list[Any]:
    binds: list[Any] = []
    for arg in args:
        raw = arg_values.get(arg.name)
        if (raw is None or raw == "") and arg.default_value:
            raw = arg.default_value
        if raw is None or raw == "":
            if arg.required:
                raise ArgError(f"Argument '{arg.name}' is required")
            binds.append(None)
            continue
        try:
            binds.append(template_service.cast_value(raw, _CAST_TYPE.get(arg.data_type, "string")))
        except (ValueError, TypeError) as exc:
            raise ArgError(f"Argument '{arg.name}': {exc}") from exc
    return binds


def _prepare(
    report: Report, dm: Datamodule, arg_values: dict[str, str]
) -> tuple[duckdb.DuckDBPyConnection, str, list[Any], list[str]]:
    """Ensure fresh, attach live version read-only, register the macro, describe.

    Returns (conn, final_sql, binds, columns). Caller owns closing ``conn``
    (stream_service closes it after COPY; run_table closes it after fetch).
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
        params = ", ".join(arg.name for arg in args)
        body = _rewrite_body(report.body_sql)
        conn.execute(
            f"CREATE OR REPLACE TEMP MACRO {report.macro_name}({params}) AS TABLE ({body})"
        )
        placeholders = ", ".join("?" for _ in args)
        call = f"{report.macro_name}({placeholders})"
        described = conn.execute(f"DESCRIBE SELECT * FROM {call}", binds).fetchall()
        columns = [str(row[0]) for row in described]
        final_sql = f"SELECT * FROM {call}"
    except Exception:
        conn.close()
        raise
    return conn, final_sql, binds, columns


def run_table(report: Report, dm: Datamodule, arg_values: dict[str, str]) -> TableResult:
    """Run the report and return columns + rows for an HTML table.

    Phase 1 buffers rows (small skeleton data); pagination lands in Phase 4.
    ArgError propagates (400); DuckDB errors (e.g. a bad macro body) are
    captured into the result so the serve page can show them inline.
    """
    try:
        conn, final_sql, binds, columns = _prepare(report, dm, arg_values)
    except duckdb.Error as exc:
        return TableResult(error=f"Query error: {exc}")
    try:
        rows = conn.execute(final_sql, binds).fetchall()
    except duckdb.Error as exc:
        return TableResult(error=f"Query error: {exc}")
    finally:
        conn.close()
    return TableResult(columns=columns, rows=rows, row_count=len(rows))


def stream(report: Report, dm: Datamodule, arg_values: dict[str, str], fmt: str) -> Any:
    """Stream the report result in ``fmt`` (Phase 1: parquet)."""
    conn, final_sql, binds, _ = _prepare(report, dm, arg_values)
    download_name = f"{report.macro_name}.{fmt}"
    if fmt == "parquet":
        return stream_service.stream_parquet(conn, final_sql, binds, download_name)
    conn.close()
    raise ArgError(f"Unsupported format '{fmt}'")
