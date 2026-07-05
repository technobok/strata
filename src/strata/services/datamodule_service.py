"""Datamodule build runner: build into tmp, atomic-swap live, TTL freshness.

A build writes a brand-new immutable ``versions/<uuid>.duckdb`` file, then a
single SQLite transaction marks the version ready and flips
``datamodule.live_version_id``. Servers resolve the live version at request
start and open it read-only, so a rebuild never disturbs in-flight readers
(they finish on their own inode). Single-flight is in-process only in Phase 1
(a per-uuid lock); the cross-process SQLite lease is a later phase.
"""

import logging
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import duckdb
from flask import current_app

from strata.db import transaction
from strata.models.datamodule import Datamodule
from strata.models.datamodule_version import DatamoduleVersion

log = logging.getLogger(__name__)

# Per-datamodule in-process build locks (single-flight within one process).
_build_locks: dict[str, threading.Lock] = {}
_build_locks_guard = threading.Lock()


def _lock_for(dm_uuid: str) -> threading.Lock:
    with _build_locks_guard:
        lock = _build_locks.get(dm_uuid)
        if lock is None:
            lock = threading.Lock()
            _build_locks[dm_uuid] = lock
        return lock


# ---------------------------------------------------------------------------
# Storage layout (flat, keyed by datamodule uuid with 2-char fan-out)
# ---------------------------------------------------------------------------


def data_root() -> Path:
    return Path(current_app.config["DATA_ROOT"])


def dm_dir(dm_uuid: str) -> Path:
    return data_root() / dm_uuid[:2] / dm_uuid


def _ensure_dirs(dm_uuid: str) -> Path:
    base = dm_dir(dm_uuid)
    for sub in ("versions", "sources", "tmp"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    return base


def version_path(dm_uuid: str, version_uuid: str) -> Path:
    return dm_dir(dm_uuid) / "versions" / f"{version_uuid}.duckdb"


def live_version(dm: Datamodule) -> DatamoduleVersion | None:
    """The current ready version pointed at by live_version_id, if any."""
    if dm.live_version_id is None:
        return None
    version = DatamoduleVersion.get_by_id(dm.live_version_id)
    if version is None or version.status != "ready":
        return None
    return version


def live_version_path(dm: Datamodule) -> Path | None:
    version = live_version(dm)
    return version_path(dm.uuid, version.uuid) if version else None


def _storage_version() -> str:
    """Pin the DuckDB library version that wrote a snapshot (forward-read compat)."""
    return duckdb.__version__


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build_datamodule(dm: Datamodule, triggered_by: str) -> DatamoduleVersion:
    """Build the datamodule into a new version and swap it live.

    Returns the ready version. On build failure records the failed version +
    datamodule.build_status and re-raises.
    """
    base = _ensure_dirs(dm.uuid)
    with transaction() as cur:
        cur.execute(
            "UPDATE datamodule SET build_status = 'building', last_build_error = NULL WHERE id = ?",
            (dm.id,),
        )
    version = DatamoduleVersion.create_building(dm.id, triggered_by, dm.build_script)
    tmp_file = base / "tmp" / f"{version.uuid}.duckdb"
    final_file = version_path(dm.uuid, version.uuid)
    start = time.monotonic()

    try:
        tmp_file.unlink(missing_ok=True)
        conn = duckdb.connect(str(tmp_file))
        try:
            if dm.build_script.strip():
                conn.execute(dm.build_script)
            conn.execute("CHECKPOINT")
        finally:
            conn.close()
        os.rename(tmp_file, final_file)
    except Exception as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        version.mark_failed(str(exc), duration_ms)
        with transaction() as cur:
            cur.execute(
                "UPDATE datamodule SET build_status = 'failed', last_build_error = ? WHERE id = ?",
                (str(exc), dm.id),
            )
        tmp_file.unlink(missing_ok=True)
        log.warning("datamodule build failed (uuid=%s): %s", dm.uuid, exc)
        raise

    duration_ms = int((time.monotonic() - start) * 1000)
    byte_size = final_file.stat().st_size
    ended_at = datetime.now(UTC).isoformat()
    storage_version = _storage_version()

    # Atomic success flip: this version ready + live pointer + supersede older.
    with transaction() as cur:
        cur.execute(
            "UPDATE datamodule_version SET status = 'ready', build_ended_at = ?, duration_ms = ?, "
            "byte_size = ?, storage_version = ? WHERE id = ?",
            (ended_at, duration_ms, byte_size, storage_version, version.id),
        )
        cur.execute(
            "UPDATE datamodule_version SET status = 'superseded' "
            "WHERE datamodule_id = ? AND id <> ? AND status = 'ready'",
            (dm.id, version.id),
        )
        cur.execute(
            "UPDATE datamodule SET live_version_id = ?, build_status = 'ready', "
            "storage_version = ?, last_build_error = NULL, modified_at = ? WHERE id = ?",
            (version.id, storage_version, ended_at, dm.id),
        )

    result = DatamoduleVersion.get_by_id(version.id)
    assert result is not None
    log.info("datamodule built (uuid=%s version=%s bytes=%d)", dm.uuid, version.uuid, byte_size)
    return result


# ---------------------------------------------------------------------------
# Freshness (TTL pull)
# ---------------------------------------------------------------------------


def _is_stale(dm: Datamodule, version: DatamoduleVersion, effective_ttl: int | None) -> bool:
    now = datetime.now(UTC)
    if dm.valid_until:
        try:
            if now > datetime.fromisoformat(dm.valid_until):
                return True
        except ValueError:
            pass
    if effective_ttl is None:
        return False  # never auto-expire
    if effective_ttl <= 0:
        return True  # ephemeral: always rebuild
    built = version.build_ended_at or version.created_at
    try:
        built_at = datetime.fromisoformat(built)
    except ValueError:
        return True
    return (now - built_at).total_seconds() > effective_ttl


def ensure_fresh(dm: Datamodule, triggered_by: str, effective_ttl: int | None) -> DatamoduleVersion:
    """Return a ready live version, rebuilding under single-flight if stale.

    ``effective_ttl`` is the resolved TTL for this serve (report override falling
    back to the datamodule TTL); None = never auto-expire, <=0 = always rebuild.
    """
    version = live_version(dm)
    if version is not None and not _is_stale(dm, version, effective_ttl):
        return version

    with _lock_for(dm.uuid):
        # Re-read under the lock: a concurrent request may have rebuilt already.
        fresh = Datamodule.get_by_id(dm.id)
        assert fresh is not None
        version = live_version(fresh)
        if version is not None and not _is_stale(fresh, version, effective_ttl):
            return version
        return build_datamodule(fresh, triggered_by)
