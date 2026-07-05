"""Constant-memory result streaming.

DuckDB's ``COPY (<query>) TO <file>`` writes the result straight to disk, so we
never buffer the full result set in Python. We COPY to a temp file, stream it to
the client in fixed-size chunks, and unlink it when the response closes. Phase 1
ships Parquet; CSV/JSON/XLSX writers arrive in Phase 4.
"""

import logging
import os
import tempfile
from collections.abc import Iterator
from typing import Any

import duckdb
from flask import Response

log = logging.getLogger(__name__)

_CHUNK = 64 * 1024


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


def stream_parquet(
    conn: duckdb.DuckDBPyConnection,
    final_sql: str,
    binds: list[Any],
    download_name: str,
) -> Response:
    """COPY ``final_sql`` to a temp Parquet file and stream it as an attachment.

    Closes ``conn`` once the file is written (the response streams the file, not
    the connection).
    """
    fd, tmp_path = tempfile.mkstemp(suffix=".parquet")
    os.close(fd)
    try:
        conn.execute(f"COPY ({final_sql}) TO '{tmp_path}' (FORMAT parquet)", binds)
    except Exception:
        os.unlink(tmp_path)
        raise
    finally:
        conn.close()

    return Response(
        _stream_file(tmp_path),
        mimetype="application/vnd.apache.parquet",
        headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
    )
