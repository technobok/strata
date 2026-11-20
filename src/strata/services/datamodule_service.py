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
import re
import shutil
import threading
import time
import uuid as uuid_lib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
from flask import current_app
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from strata.db import transaction
from strata.models.connection import Connection
from strata.models.datamodule import Datamodule
from strata.models.datamodule_connection import DatamoduleConnection
from strata.models.datamodule_version import DatamoduleVersion
from strata.models.dependency import Dependency
from strata.models.source_file import SourceFile
from strata.services import duck
from strata.services.connection_service import attach_into

log = logging.getLogger(__name__)

# source('name') references in build scripts -> the on-disk path of an uploaded
# file. Best-effort raw-text match (like template_service.find_refs).
_SOURCE_RE = re.compile(r"source\(\s*(['\"])(.*?)\1\s*\)")

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


def _storage_version(conn: duckdb.DuckDBPyConnection) -> str:
    """The effective storage_compatibility_version this snapshot was written with.

    Read from the build connection (a build script may SET its own pin); DuckDB's
    default is a broadly-compatible floor (v0.10.2 on 1.4.x). The file itself only
    records the minimum-reader family, so the setting value is recorded here.
    """
    row = conn.execute("SELECT current_setting('storage_compatibility_version')").fetchone()
    return str(row[0]) if row else "unknown"


# ---------------------------------------------------------------------------
# Source files (uploaded build inputs)
# ---------------------------------------------------------------------------


def sources_dir(dm_uuid: str) -> Path:
    d = dm_dir(dm_uuid) / "sources"
    d.mkdir(parents=True, exist_ok=True)
    return d


def source_disk_path(dm_uuid: str, stored_name: str) -> Path:
    return sources_dir(dm_uuid) / stored_name


def save_source(
    dm: Datamodule, file_storage: FileStorage, filename: str, uploaded_by: str
) -> SourceFile:
    """Store an uploaded file under sources/ and record it (replacing same-name)."""
    safe = secure_filename(filename) or "file"
    stored_name = f"{uuid_lib.uuid4().hex}_{safe}"
    dest = source_disk_path(dm.uuid, stored_name)
    file_storage.save(str(dest))
    byte_size = dest.stat().st_size

    existing = SourceFile.get_by_filename(dm.id, filename)
    if existing is not None:
        delete_source(dm, existing)
    return SourceFile.create(
        datamodule_id=dm.id,
        filename=filename,
        stored_name=stored_name,
        byte_size=byte_size,
        content_type=file_storage.mimetype or None,
        uploaded_by=uploaded_by,
    )


def delete_source(dm: Datamodule, source_file: SourceFile) -> None:
    source_disk_path(dm.uuid, source_file.stored_name).unlink(missing_ok=True)
    source_file.delete()


def _render_sources(build_script: str, source_map: dict[str, str]) -> str:
    """Expand source('name') references to the on-disk path as a SQL string literal."""

    def repl(match: re.Match[str]) -> str:
        name = match.group(2)
        if name not in source_map:
            raise RuntimeError(
                f"build script references source('{name}') which is not an uploaded source file"
            )
        return "'" + source_map[name].replace("'", "''") + "'"

    return _SOURCE_RE.sub(repl, build_script)


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def _resolve_dependencies(dm: Datamodule, chain: frozenset[int]) -> list[tuple[str, str]]:
    """Ensure each dependency is fresh (TTL pull) and return (local_name, path)."""
    attachments: list[tuple[str, str]] = []
    for dep in Dependency.get_for_datamodule(dm.id):
        dep_dm = Datamodule.get_by_id(dep.depends_on_id)
        if dep_dm is None:
            raise RuntimeError(f"dependency '{dep.local_name}': datamodule was deleted")
        if dep_dm.id in chain:
            raise RuntimeError(f"dependency cycle detected via '{dep_dm.path}'")
        dep_version = ensure_fresh(
            dep_dm, f"dependency:{dm.path}", dep_dm.ttl_seconds, _stack=chain
        )
        attachments.append((dep.local_name, str(version_path(dep_dm.uuid, dep_version.uuid))))
    return attachments


def _resolve_connections(dm: Datamodule) -> list[tuple[str, Connection]]:
    attachments: list[tuple[str, Connection]] = []
    for dmc in DatamoduleConnection.get_for_datamodule(dm.id):
        crow = Connection.get_by_id(dmc.connection_id)
        if crow is None:
            raise RuntimeError(f"connection binding '{dmc.local_name}': connection was deleted")
        attachments.append((dmc.local_name, crow))
    return attachments


def build_datamodule(
    dm: Datamodule, triggered_by: str, _stack: frozenset[int] = frozenset()
) -> DatamoduleVersion:
    """Build the datamodule into a new version and swap it live.

    Before running the build script, dependencies are ensure_fresh-ed (TTL pull)
    and ATTACHed read-only by local_name, bound connections are attached via
    connection_service, and ``source('name')`` references expand to uploaded
    files. Returns the ready version; on failure records the failed version +
    datamodule.build_status and re-raises. ``_stack`` carries the datamodule ids
    already building in this chain, for cycle detection.
    """
    chain = _stack | {dm.id}
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
        # Resolve build inputs (may rebuild dependencies) before opening the file.
        dep_attachments = _resolve_dependencies(dm, chain)
        conn_attachments = _resolve_connections(dm)
        source_map = {
            sf.filename: str(source_disk_path(dm.uuid, sf.stored_name))
            for sf in SourceFile.get_for_datamodule(dm.id)
        }
        rendered = _render_sources(dm.build_script, source_map)

        tmp_file.unlink(missing_ok=True)
        conn = duckdb.connect(str(tmp_file))
        try:
            for local_name, dep_path in dep_attachments:
                conn.execute(f"ATTACH '{dep_path}' AS {local_name} (READ_ONLY)")
            for local_name, crow in conn_attachments:
                attach_into(conn, local_name, crow.driver, crow.params)
            # Trusted setup is done: from here the author's script can only
            # read this datamodule's sources/ and write its tmp/ (services/duck).
            duck.lock_down_build(conn, base)
            if rendered.strip():
                conn.execute(rendered)
            conn.execute("CHECKPOINT")
            storage_version = _storage_version(conn)
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

    # Retention runs best-effort after every successful flip; never fails a build.
    try:
        pruned = prune_versions(dm)
        if pruned.rows_deleted:
            log.info(
                "pruned %d version(s), freed %d bytes (uuid=%s)",
                pruned.rows_deleted,
                pruned.bytes_freed,
                dm.uuid,
            )
    except Exception as exc:
        log.warning("post-build prune failed (uuid=%s): %s", dm.uuid, exc)

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


def ensure_fresh(
    dm: Datamodule,
    triggered_by: str,
    effective_ttl: int | None,
    _stack: frozenset[int] = frozenset(),
) -> DatamoduleVersion:
    """Return a ready live version, rebuilding under single-flight if stale.

    ``effective_ttl`` is the resolved TTL for this serve (report override falling
    back to the datamodule TTL); None = never auto-expire, <=0 = always rebuild.
    ``_stack`` carries the current build chain for dependency cycle detection.
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
        return build_datamodule(fresh, triggered_by, _stack=_stack)


def build_now(dm: Datamodule, triggered_by: str) -> DatamoduleVersion:
    """Rebuild unconditionally, under the same per-datamodule lock as ensure_fresh.

    The manual Build button: if a TTL rebuild is already running in this
    process, wait for it and then build again (the author asked for a fresh
    build of the current script).
    """
    with _lock_for(dm.uuid):
        fresh = Datamodule.get_by_id(dm.id)
        if fresh is None:
            raise RuntimeError("datamodule was deleted")
        return build_datamodule(fresh, triggered_by)


# ---------------------------------------------------------------------------
# Retention (prune old versions: rows AND files)
# ---------------------------------------------------------------------------


@dataclass
class PruneResult:
    rows_deleted: int = 0
    files_deleted: int = 0
    bytes_freed: int = 0
    victims: list[str] = field(default_factory=list)  # "ab12cd34 (superseded)"


def prune_candidates(dm: Datamodule) -> list[DatamoduleVersion]:
    """Version rows the retention policy would delete.

    Never the live version, never in-flight ``building`` rows. Re-reads the
    datamodule row: right after a build the caller's dataclass still carries
    the pre-flip live_version_id and policy.
    """
    fresh = Datamodule.get_by_id(dm.id)
    if fresh is None:
        return []
    versions = DatamoduleVersion.list_for(fresh.id)
    eligible = [v for v in versions if v.status != "building" and v.id != fresh.live_version_id]
    if fresh.retention_kind == "age":
        if fresh.retention_age_seconds is None:
            return []  # blank age = keep everything
        cutoff = (datetime.now(UTC) - timedelta(seconds=fresh.retention_age_seconds)).isoformat()
        return [v for v in eligible if (v.build_ended_at or v.created_at) < cutoff]
    # count: keep the newest N non-building rows (failures consume slots);
    # the live version is additionally always kept.
    keep = max(1, fresh.retention_count)
    ranked = sorted(
        (v for v in versions if v.status != "building"),
        key=lambda v: ((v.build_ended_at or v.created_at), v.id),
        reverse=True,
    )
    window = {v.id for v in ranked[:keep]}
    return [v for v in eligible if v.id not in window]


def prune_versions(dm: Datamodule) -> PruneResult:
    """Enforce the retention policy: unlink each victim's file, then delete its row.

    The DELETE re-checks live/building in SQL, so a stale candidate list can
    never null the live pointer (live_version_id is FK ON DELETE SET NULL).
    An in-flight reader of an unlinked file finishes on its own inode.
    """
    result = PruneResult()
    for v in prune_candidates(dm):
        path = version_path(dm.uuid, v.uuid)
        try:
            size = path.stat().st_size
            path.unlink()
            result.files_deleted += 1
            result.bytes_freed += size
        except FileNotFoundError:
            pass  # failed builds never produced a file
        except OSError as exc:
            log.warning("prune: could not remove %s: %s", path, exc)
            continue  # keep the row so the file stays discoverable
        with transaction() as cur:
            cur.execute(
                "DELETE FROM datamodule_version WHERE id = ? AND status != 'building' "
                "AND id != COALESCE((SELECT live_version_id FROM datamodule WHERE id = ?), -1)",
                (v.id, dm.id),
            )
            changed = cur.execute("SELECT changes()").fetchone()
        if changed and changed[0] > 0:
            result.rows_deleted += 1
            result.victims.append(f"{v.uuid[:8]} ({v.status})")
    return result


# ---------------------------------------------------------------------------
# Reconcile (retention sweep + filesystem/DB consistency)
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_FAN_RE = re.compile(r"^[0-9a-f]{2}$")
_TMP_MAX_AGE_SECONDS = 24 * 3600
# A version row still 'building' after this long belongs to a process that died
# (the build never reached its success flip or failure handler).
_STUCK_BUILD_SECONDS = 24 * 3600
_STUCK_BUILD_ERROR = "build never finished (its process died); marked failed by reconcile"


@dataclass
class ReconcileReport:
    dry_run: bool
    prune_lines: list[str] = field(default_factory=list)  # "<path>: ab12cd34 (superseded)"
    rows_deleted: int = 0
    files_deleted: int = 0
    bytes_freed: int = 0
    orphan_dirs: list[str] = field(default_factory=list)
    stale_tmp: list[str] = field(default_factory=list)
    orphan_files: list[str] = field(default_factory=list)
    stuck_builds: list[str] = field(default_factory=list)  # "<path>: ab12cd34"
    stale_scratch: list[str] = field(default_factory=list)  # DATA_ROOT/_exports + _tmp
    warnings: list[str] = field(default_factory=list)


def reconcile(dry_run: bool = False) -> ReconcileReport:
    """Enforce retention everywhere and sweep the data root back into consistency.

    Removes: versions beyond each datamodule's retention policy, tmp files older
    than a day (crashed builds), version files with no DB row, and whole
    datamodule directories whose datamodule row was deleted. A live version with
    a missing file is only ever WARNED about. Prune runs first so its unlinked
    files never double-count as orphans.
    """
    report = ReconcileReport(dry_run=dry_run)
    dms = Datamodule.get_all()
    known = {dm.uuid for dm in dms}

    for dm in dms:
        _reap_stuck_builds(dm, report)
        if dry_run:
            for v in prune_candidates(dm):
                report.prune_lines.append(f"{dm.path}: {v.uuid[:8]} ({v.status})")
                report.rows_deleted += 1
                try:
                    size = version_path(dm.uuid, v.uuid).stat().st_size
                    report.files_deleted += 1
                    report.bytes_freed += size
                except OSError:
                    pass
        else:
            pruned = prune_versions(dm)
            report.prune_lines.extend(f"{dm.path}: {victim}" for victim in pruned.victims)
            report.rows_deleted += pruned.rows_deleted
            report.files_deleted += pruned.files_deleted
            report.bytes_freed += pruned.bytes_freed

        base = dm_dir(dm.uuid)
        if not base.exists():
            continue

        # Crashed builds leave <uuid>.duckdb plus its .wal and .tmp/ spill dir.
        report.stale_tmp.extend(_sweep_old(base / "tmp", dry_run))

        version_uuids = {v.uuid for v in DatamoduleVersion.list_for(dm.id)}
        for f in sorted((base / "versions").glob("*.duckdb")):
            if f.stem in version_uuids:
                continue
            report.orphan_files.append(str(f))
            if not dry_run:
                try:
                    f.unlink()
                except OSError:
                    continue

        live = live_version(dm)
        if live is not None and not version_path(dm.uuid, live.uuid).exists():
            report.warnings.append(f"{dm.path}: live version {live.uuid[:8]} has no file on disk")

    root = data_root()
    for scratch in ("_exports", "_tmp"):  # serve-path exports and spill dirs
        report.stale_scratch.extend(_sweep_old(root / scratch, dry_run))
    if root.exists():
        for fan in sorted(root.iterdir()):
            if not fan.is_dir() or not _FAN_RE.fullmatch(fan.name):
                continue
            for d in sorted(fan.iterdir()):
                if not d.is_dir() or not _UUID_RE.fullmatch(d.name):
                    continue
                if d.name[:2] != fan.name or d.name in known:
                    continue
                report.orphan_dirs.append(str(d))
                if not dry_run:
                    shutil.rmtree(d, ignore_errors=True)
                    try:
                        fan.rmdir()  # removes the fan-out dir only when empty
                    except OSError:
                        pass
    return report


def _sweep_old(directory: Path, dry_run: bool) -> list[str]:
    """Remove (or list) entries of ``directory`` untouched for over a day."""
    swept: list[str] = []
    if not directory.is_dir():
        return swept
    now = time.time()
    for entry in sorted(directory.iterdir()):
        try:
            if now - entry.stat().st_mtime <= _TMP_MAX_AGE_SECONDS:
                continue
            swept.append(str(entry))
            if not dry_run:
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink()
        except OSError:
            continue  # racing an in-flight build or export
    return swept


def _reap_stuck_builds(dm: Datamodule, report: ReconcileReport) -> None:
    """Mark long-dead 'building' versions failed, and the datamodule with them.

    A process killed mid-build (OOM, worker restart) never reaches either the
    success flip or the failure handler, so its version row stays 'building'
    forever, and retention never prunes 'building' rows.
    """
    cutoff = (datetime.now(UTC) - timedelta(seconds=_STUCK_BUILD_SECONDS)).isoformat()
    building = [v for v in DatamoduleVersion.list_for(dm.id) if v.status == "building"]
    stuck = [v for v in building if (v.build_started_at or v.created_at) < cutoff]
    for v in stuck:
        report.stuck_builds.append(f"{dm.path}: {v.uuid[:8]}")
        if not report.dry_run:
            v.mark_failed(_STUCK_BUILD_ERROR, None)
    if stuck and len(stuck) == len(building) and not report.dry_run:
        with transaction() as cur:
            cur.execute(
                "UPDATE datamodule SET build_status = 'failed', last_build_error = ? "
                "WHERE id = ? AND build_status = 'building'",
                (_STUCK_BUILD_ERROR, dm.id),
            )
