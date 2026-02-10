"""Query service for DuckDB execution and result handling."""

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import duckdb

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


def execute_report(
    sql_template: str,
    structural_params: dict[str, str],
    value_params: dict[str, str],
    param_types: dict[str, str],
) -> QueryResult:
    """Execute a report's SQL template with the given parameters.

    1. Validate structural parameters
    2. Render Jinja2 structural template
    3. Cast value parameters to proper types
    4. Execute via DuckDB with bind parameters
    """
    result = QueryResult()

    # Validate structural parameters
    for name, value in structural_params.items():
        error = validate_structural_value(name, value)
        if error:
            result.error = error
            return result

    # Render structural template
    try:
        rendered_sql = render_structural(sql_template, structural_params)
        result.rendered_sql = rendered_sql
    except Exception as e:
        result.error = f"Template rendering error: {e}"
        return result

    # Cast value parameters
    bind_params: dict[str, Any] = {}
    for name, value in value_params.items():
        data_type = param_types.get(name, "string")
        try:
            bind_params[name] = cast_value(value, data_type)
        except (ValueError, TypeError) as e:
            result.error = f"Parameter '{name}': {e}"
            return result

    # Execute query
    start_time = time.monotonic()
    try:
        conn = duckdb.connect(":memory:")
        try:
            rel = conn.execute(rendered_sql, bind_params)
            result.columns = [desc[0] for desc in rel.description]
            result.types = [desc[1] for desc in rel.description]
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
