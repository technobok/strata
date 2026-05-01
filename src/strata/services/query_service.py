"""Query service for DuckDB execution and result handling."""

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
from flask import current_app

from strata.services.template_service import (
    cast_value,
    render_structural,
    validate_structural_value,
)

log = logging.getLogger(__name__)


@dataclass
class QueryResult:
    columns: list[str] = field(default_factory=list)
    types: list[str] = field(default_factory=list)
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    row_count: int = 0
    duration_ms: int = 0
    rendered_sql: str = ""
    error: str | None = None


def compute_result_hash(
    report_id: int,
    rendered_sql: str,
    bind_params: dict[str, Any],
) -> str:
    """Compute a SHA256 hash for caching."""
    data = json.dumps(
        {"report_id": report_id, "sql": rendered_sql, "params": bind_params},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(data.encode()).hexdigest()


def _materialised_path(name: str) -> Path:
    """Resolve the .duckdb file path for a materialised report."""
    cache_dir = Path(current_app.config.get("CACHE_DIRECTORY", "instance/cache"))
    named_dir = cache_dir / "named"
    named_dir.mkdir(parents=True, exist_ok=True)
    return named_dir / f"{name}.duckdb"


def execute_report(
    sql_template: str,
    structural_params: dict[str, str],
    value_params: dict[str, str],
    param_types: dict[str, str],
    materialise_as: str | None = None,
) -> QueryResult:
    """Execute a report's SQL template with the given parameters.

    1. Validate structural parameters
    2. Render Jinja2 structural template, collecting ref('...') and conn('...')
       usages as side effects
    3. Cast value parameters to proper types
    4. If materialise_as is set, target the corresponding named .duckdb file;
       otherwise run in :memory:
    5. ATTACH each ref()'d materialised file as mat_<name>
    6. ATTACH each conn()'d external source under its own name
    7. Execute via DuckDB with bind parameters; if materialising, write the
       result into a `result` table inside the target file.
    """
    result = QueryResult()

    # Validate structural parameters
    for name, value in structural_params.items():
        error = validate_structural_value(name, value)
        if error:
            result.error = error
            return result

    # Render structural template
    refs_used: list[str] = []
    conns_used: list[str] = []
    try:
        rendered_sql = render_structural(
            sql_template,
            structural_params,
            refs_collector=refs_used,
            conns_collector=conns_used,
        )
        result.rendered_sql = rendered_sql
    except Exception as e:
        result.error = f"Template rendering error: {e}"
        return result

    # Reject self-referential materialisation.
    if materialise_as is not None and materialise_as in refs_used:
        result.error = (
            f"Report cannot ref('{materialise_as}') while materialising as '{materialise_as}'"
        )
        return result

    # Resolve ref()'d source files (must already exist on disk).
    ref_paths: dict[str, Path] = {}
    for ref_name in refs_used:
        path = _materialised_path(ref_name)
        if not path.exists():
            result.error = (
                f"Materialised report '{ref_name}' has not been run yet (expected file: {path})"
            )
            return result
        ref_paths[ref_name] = path

    # Resolve conn()'d connections (must already exist).
    from strata.models.connection import Connection

    conn_rows = {}
    for conn_name in conns_used:
        conn_row = Connection.get_by_name(conn_name)
        if conn_row is None:
            result.error = f"conn('{conn_name}'): no connection defined with that name"
            return result
        conn_rows[conn_name] = conn_row

    # Cast value parameters
    bind_params: dict[str, Any] = {}
    for name, value in value_params.items():
        data_type = param_types.get(name, "string")
        try:
            bind_params[name] = cast_value(value, data_type)
        except (ValueError, TypeError) as e:
            result.error = f"Parameter '{name}': {e}"
            return result

    # Decide where the query runs.
    target_path: Path | None = None
    if materialise_as is not None:
        target_path = _materialised_path(materialise_as)

    start_time = time.monotonic()
    try:
        conn = duckdb.connect(str(target_path) if target_path else ":memory:")
        try:
            if target_path is not None:
                # Reset stale state in the target file before re-materialising.
                conn.execute("DROP TABLE IF EXISTS result")

            # ATTACH ref()'d materialised sources read-only.
            for ref_name, path in ref_paths.items():
                conn.execute(f"ATTACH '{path}' AS mat_{ref_name} (READ_ONLY)")

            # ATTACH each conn()'d external source under its own name.
            from strata.services.connection_service import attach_into

            for conn_name, conn_row in conn_rows.items():
                attach_into(conn, conn_name, conn_row.driver, conn_row.params)

            if target_path is not None:
                # Materialise into the named .duckdb file as a single `result` table,
                # then read it back so the live UI sees the same rows.
                conn.execute(f"CREATE TABLE result AS {rendered_sql}", bind_params)
                rel = conn.execute("SELECT * FROM result")
            else:
                rel = conn.execute(rendered_sql, bind_params)

            result.columns = [desc[0] for desc in rel.description]
            result.types = [str(desc[1]) for desc in rel.description]
            result.rows = rel.fetchall()
            result.row_count = len(result.rows)
        finally:
            conn.close()
    except duckdb.Error as e:
        result.error = f"Query error: {e}"
        return result
    except Exception as e:
        result.error = f"Unexpected error: {e}"
        return result

    elapsed = time.monotonic() - start_time
    result.duration_ms = int(elapsed * 1000)

    return result
