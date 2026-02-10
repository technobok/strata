"""Parquet-based result cache service."""

import logging
from pathlib import Path
from typing import Any

import duckdb

log = logging.getLogger(__name__)


def _get_cache_dir() -> Path:
    """Get the cache directory path."""
    try:
        from flask import current_app

        cache_dir = current_app.config.get("CACHE_DIRECTORY", "instance/cache")
    except RuntimeError:
        cache_dir = "instance/cache"
    return Path(cache_dir)


def cache_path(result_hash: str) -> Path:
    """Get the path for a cached result file."""
    cache_dir = _get_cache_dir()
    subdir = cache_dir / result_hash[:2]
    subdir.mkdir(parents=True, exist_ok=True)
    return subdir / f"{result_hash}.parquet"


def write_result(
    result_hash: str,
    columns: list[str],
    types: list[str],
    rows: list[tuple[Any, ...]],
) -> Path:
    """Write query results to a Parquet file."""
    path = cache_path(result_hash)

    if path.exists():
        return path

    conn = duckdb.connect(":memory:")
    try:
        # Create table from results
        col_defs = ", ".join(f'"{col}" VARCHAR' for col in columns)
        conn.execute(f"CREATE TABLE results ({col_defs})")

        if rows:
            placeholders = ", ".join("?" for _ in columns)
            for row in rows:
                conn.execute(f"INSERT INTO results VALUES ({placeholders})", list(row))

        conn.execute(f"COPY results TO '{path}' (FORMAT PARQUET)")
    finally:
        conn.close()

    return path


def read_result(
    result_hash: str,
    sort_col: str | None = None,
    sort_dir: str = "asc",
    filter_text: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> tuple[list[str], list[tuple[Any, ...]], int]:
    """Read cached results from Parquet, optionally sorted/filtered.

    Returns (columns, rows, total_count).
    """
    path = cache_path(result_hash)
    if not path.exists():
        return [], [], 0

    conn = duckdb.connect(":memory:")
    try:
        # Get total count
        count_result = conn.execute(f"SELECT COUNT(*) FROM read_parquet('{path}')").fetchone()
        total_count = int(count_result[0]) if count_result else 0

        # Build query
        sql = f"SELECT * FROM read_parquet('{path}')"

        if filter_text:
            # Filter across all columns using CAST to VARCHAR
            rel = conn.execute(f"SELECT * FROM read_parquet('{path}') LIMIT 0")
            cols = [desc[0] for desc in rel.description]
            filter_clauses = [f'CAST("{col}" AS VARCHAR) ILIKE ?' for col in cols]
            sql += " WHERE " + " OR ".join(filter_clauses)
            filter_params = [f"%{filter_text}%"] * len(cols)
        else:
            filter_params = []

        if sort_col:
            direction = "DESC" if sort_dir.lower() == "desc" else "ASC"
            sql += f' ORDER BY "{sort_col}" {direction} NULLS LAST'

        if limit:
            sql += f" LIMIT {limit} OFFSET {offset}"

        result = conn.execute(sql, filter_params)
        columns = [desc[0] for desc in result.description]
        rows = result.fetchall()

        if filter_text:
            count_sql = f"SELECT COUNT(*) FROM read_parquet('{path}')"
            count_sql += " WHERE " + " OR ".join(filter_clauses)
            count_result = conn.execute(count_sql, filter_params).fetchone()
            total_count = int(count_result[0]) if count_result else 0

        return columns, rows, total_count
    finally:
        conn.close()


def result_exists(result_hash: str) -> bool:
    """Check if a cached result exists."""
    return cache_path(result_hash).exists()


def delete_result(result_hash: str) -> bool:
    """Delete a cached result file."""
    path = cache_path(result_hash)
    if path.exists():
        path.unlink()
        return True
    return False


def purge_old_cache(valid_hashes: set[str]) -> int:
    """Delete cache files not in the valid set. Returns count deleted."""
    cache_dir = _get_cache_dir()
    deleted = 0

    if not cache_dir.exists():
        return 0

    for subdir in cache_dir.iterdir():
        if not subdir.is_dir():
            continue
        for parquet_file in subdir.glob("*.parquet"):
            file_hash = parquet_file.stem
            if file_hash not in valid_hashes:
                parquet_file.unlink()
                deleted += 1

    return deleted
