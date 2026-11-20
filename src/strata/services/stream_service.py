"""Constant-memory result streaming.

DuckDB's ``COPY (<query>) TO <file>`` writes parquet/csv/json straight to disk,
so we never buffer the full result set in Python; XLSX goes through openpyxl's
write-only mode in fetchmany batches. Every writer spools to a temp file,
streams it to the client in fixed-size chunks, and unlinks it when the response
closes.
"""

import logging
import os
import tempfile
from collections.abc import Iterator
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

import duckdb
from flask import Response
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableColumn, TableStyleInfo

from strata.services import duck

log = logging.getLogger(__name__)

_CHUNK = 64 * 1024

# fmt -> (COPY options, content type). Werkzeug adds charset=utf-8 to text/*.
COPY_FORMATS: dict[str, tuple[str, str]] = {
    "parquet": ("(FORMAT parquet)", "application/vnd.apache.parquet"),
    "csv": ("(FORMAT csv, HEADER)", "text/csv"),
    "json": ("(FORMAT json, ARRAY true)", "application/json"),
}
XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
STREAM_FORMATS = frozenset(COPY_FORMATS) | {"xlsx"}

_XLSX_CHUNK = 1000  # fetchmany batch size
_XLSX_PROBE_ROWS = 100  # rows buffered up front to size columns
_XLSX_MIN_WIDTH = 8
_XLSX_MAX_WIDTH = 50


def _stream_file(path: str) -> Iterator[bytes]:
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(_CHUNK)
                if not chunk:
                    break
                yield chunk
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _attachment(
    path: str, content_type: str, download_name: str, row_count: int | None = None
) -> Response:
    # The temp file is complete before streaming starts, so the exact
    # Content-Length is known (Flask won't derive it from a generator body).
    headers = {
        "Content-Disposition": f'attachment; filename="{download_name}"',
        "Content-Length": str(os.path.getsize(path)),
    }
    if row_count is not None:
        headers["X-Row-Count"] = str(row_count)
    return Response(_stream_file(path), mimetype=content_type, headers=headers)


def stream_copy(
    conn: duckdb.DuckDBPyConnection,
    final_sql: str,
    binds: list[Any],
    fmt: str,
    download_name: str,
) -> Response:
    """COPY ``final_sql`` to a temp file in ``fmt`` and stream it as an attachment.

    Closes ``conn`` once the file is written (the response streams the file, not
    the connection).
    """
    options, content_type = COPY_FORMATS[fmt]
    fd, tmp_path = tempfile.mkstemp(suffix=f".{fmt}", dir=duck.exports_dir())
    os.close(fd)
    try:
        cursor = conn.execute(f"COPY ({final_sql}) TO '{tmp_path}' {options}", binds)
        row = cursor.fetchone()  # COPY returns the rows-copied count
        rows_copied = int(row[0]) if row else None
    except Exception:
        os.unlink(tmp_path)
        raise
    finally:
        conn.close()

    return _attachment(tmp_path, content_type, download_name, row_count=rows_copied)


def _xlsx_value(value: Any) -> Any:
    """Coerce a DuckDB value to something openpyxl accepts (no tz, no STRUCTs)."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo is not None else value
    if isinstance(value, time):
        return value.replace(tzinfo=None) if value.tzinfo is not None else value
    if value is None or isinstance(value, (bool, int, float, str, Decimal, date)):
        return value
    return str(value)  # STRUCT / LIST / BLOB / UUID / INTERVAL ...


def stream_xlsx(
    conn: duckdb.DuckDBPyConnection,
    final_sql: str,
    binds: list[Any],
    download_name: str,
    columns: list[str],
) -> Response:
    """Write the result to a temp .xlsx via openpyxl's write-only mode and stream it.

    Rows pass through in fetchmany batches (constant memory); the first
    _XLSX_PROBE_ROWS rows are buffered up front to size the columns, which
    write-only mode requires before the first append.
    """
    fd, tmp_path = tempfile.mkstemp(suffix=".xlsx", dir=duck.exports_dir())
    os.close(fd)
    try:
        cursor = conn.execute(final_sql, binds)
        probe: list[tuple[Any, ...]] = []
        while len(probe) < _XLSX_PROBE_ROWS:
            batch = cursor.fetchmany(_XLSX_PROBE_ROWS - len(probe))
            if not batch:
                break
            probe.extend(batch)

        wb = Workbook(write_only=True)
        title = download_name.rsplit(".", 1)[0][:31] or "Report"
        ws = wb.create_sheet(title=title)

        for i, name in enumerate(columns):
            longest = max(
                [len(str(name))]
                + [len(str(_xlsx_value(row[i]))) for row in probe if row[i] is not None]
            )
            width = min(max(longest + 2, _XLSX_MIN_WIDTH), _XLSX_MAX_WIDTH)
            ws.column_dimensions[get_column_letter(i + 1)].width = width

        header = []
        for name in columns:
            cell = WriteOnlyCell(ws, value=str(name))
            cell.font = Font(bold=True)
            header.append(cell)
        ws.append(header)

        nrows = 0
        batch = probe
        while batch:
            for row in batch:
                ws.append([_xlsx_value(v) for v in row])
            nrows += len(batch)
            batch = cursor.fetchmany(_XLSX_CHUNK)

        # A real Excel table needs >=1 data row and unique, non-empty headers.
        # tableColumns must be pre-populated: openpyxl's write path otherwise
        # subscripts the sheet to derive them, which write-only mode can't do.
        if nrows >= 1 and columns and all(columns) and len(set(columns)) == len(columns):
            ref = f"A1:{get_column_letter(len(columns))}{nrows + 1}"
            table = Table(displayName="StrataReport", ref=ref)
            table.tableColumns = [
                TableColumn(id=i, name=str(name)) for i, name in enumerate(columns, 1)
            ]
            table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium9", showRowStripes=True)
            ws.add_table(table)

        wb.save(tmp_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    finally:
        conn.close()

    return _attachment(tmp_path, XLSX_CONTENT_TYPE, download_name, row_count=nrows)
