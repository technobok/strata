"""Datamodule build runner: build into tmp, atomic-swap live, TTL freshness.

A build writes a brand-new immutable ``versions/<uuid>.duckdb`` file, then a
single SQLite transaction marks the version ready and flips
``datamodule.live_version_id``. Servers resolve the live version at request
start and open it read-only, so a rebuild never disturbs in-flight readers
(they finish on their own inode).

Single-flight is a cross-process lease on the datamodule row
(``build_token`` / ``build_lease_until``): whoever builds holds it, a daemon
thread renews it every HEARTBEAT_SECONDS, and an expired lease means the
builder died. A version row is 'building' only while its builder holds the
lease, so taking over an expired lease fails the dead builder's rows.
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

import apsw
import duckdb
from flask import current_app
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from strata.db import get_db, get_db_path, transaction
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

# ---------------------------------------------------------------------------
# Build lease (cross-process single-flight)
# ---------------------------------------------------------------------------

LEASE_SECONDS = 60
HEARTBEAT_SECONDS = 15
LEASE_POLL_SECONDS = 1.0
DEAD_BUILD_ERROR = "build stopped: the process running it died or was killed"


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class BuildLease:
    """A held build lease on one datamodule, renewed from a daemon thread.

    The heartbeat uses its own SQLite connection (APSW connections are not
    shared across threads here). DuckDB releases the GIL while it executes, so
    the heartbeat keeps running during a long build.
    """

    def __init__(self, dm_id: int, token: str) -> None:
        self.dm_id = dm_id
        self.token = token
        self._db_path = get_db_path()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._beat, name=f"lease-{dm_id}", daemon=True)
        self._thread.start()

    @staticmethod
    def try_acquire(dm_id: int) -> BuildLease | None:
        now = datetime.now(UTC)
        token = uuid_lib.uuid4().hex
        with transaction() as cur:
            cur.execute(
                "UPDATE datamodule SET build_token = ?, build_lease_until = ? WHERE id = ? "
                "AND (build_token IS NULL OR build_lease_until IS NULL OR build_lease_until < ?)",
                (token, _iso(now + timedelta(seconds=LEASE_SECONDS)), dm_id, _iso(now)),
            )
            if int(cur.execute("SELECT changes()").fetchone()[0]) == 0:  # type: ignore[index]
                return None
            # Any row still 'building' belonged to a builder whose lease expired.
            cur.execute(
                "UPDATE datamodule_version SET status = 'failed', build_ended_at = ?, "
                "error_message = ? WHERE datamodule_id = ? AND status = 'building'",
                (_iso(now), DEAD_BUILD_ERROR, dm_id),
            )
        return BuildLease(dm_id, token)

    @staticmethod
    def acquire_waiting(dm_id: int) -> BuildLease:
        """Take the lease, waiting while another builder holds it.

        Always ends: a live holder finishes, a dead one's lease expires within
        LEASE_SECONDS.
        """
        while True:
            lease = BuildLease.try_acquire(dm_id)
            if lease is not None:
                return lease
            time.sleep(LEASE_POLL_SECONDS)

    def _beat(self) -> None:
        conn = apsw.Connection(self._db_path)
        conn.execute("PRAGMA busy_timeout = 5000;")
        try:
            while not self._stop.wait(HEARTBEAT_SECONDS):
                until = _iso(datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS))
                try:
                    conn.execute(
                        "UPDATE datamodule SET build_lease_until = ? "
                        "WHERE id = ? AND build_token = ?",
                        (until, self.dm_id, self.token),
                    )
                except apsw.Error as exc:
                    log.warning("lease heartbeat failed (dm=%s): %s", self.dm_id, exc)
        finally:
            conn.close()

    def release(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)
        with transaction() as cur:
            cur.execute(
                "UPDATE datamodule SET build_token = NULL, build_lease_until = NULL "
                "WHERE id = ? AND build_token = ?",
                (self.dm_id, self.token),
            )


def reap_dead_builds(dry_run: bool = False) -> list[str]:
    """Fail 'building' rows (and statuses) whose datamodule has no valid lease.

    Returns "<path>: <version uuid prefix>" per dead build. Used by reconcile
    and the worker's periodic sweep; a takeover in BuildLease.try_acquire does
    the same for one datamodule.
    """
    now = _iso(datetime.now(UTC))
    no_lease = "(d.build_token IS NULL OR d.build_lease_until IS NULL OR d.build_lease_until < ?)"
    rows = (
        get_db()
        .execute(
            "SELECT d.path, v.id, v.uuid FROM datamodule_version v "
            "JOIN datamodule d ON d.id = v.datamodule_id "
            f"WHERE v.status = 'building' AND {no_lease} ORDER BY d.path, v.id",
            (now,),
        )
        .fetchall()
    )
    if not dry_run:
        with transaction() as cur:
            for _, version_id, _ in rows:
                cur.execute(
                    "UPDATE datamodule_version SET status = 'failed', build_ended_at = ?, "
                    "error_message = ? WHERE id = ? AND status = 'building'",
                    (now, DEAD_BUILD_ERROR, version_id),
                )
            cur.execute(
                "UPDATE datamodule SET build_status = 'failed', last_build_error = ? "
                "WHERE build_status = 'building' AND id IN (SELECT d.id FROM datamodule d "
                f"WHERE {no_lease})",
                (DEAD_BUILD_ERROR, now),
            )
    return [f"{path}: {str(vuuid)[:8]}" for path, _, vuuid in rows]


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
# Failure backoff
# ---------------------------------------------------------------------------

BACKOFF_BASE_SECONDS = 60
BACKOFF_MAX_SECONDS = 3600


class BackoffError(RuntimeError):
    """An automatic rebuild was not attempted: the datamodule's last build failed recently."""


@dataclass
class BuildRun:
    """How one requested build treats the datamodules it touches.

    ``auto``: an automatic trigger (a stale read). Stale datamodules whose last
    build failed are then not retried before their ``retry_after``; manual and
    scheduled builds retry regardless.
    """

    auto: bool = False


def backoff_seconds(failures: int) -> int:
    return min(BACKOFF_BASE_SECONDS * 2 ** max(failures - 1, 0), BACKOFF_MAX_SECONDS)


def in_backoff(dm: Datamodule) -> bool:
    if not dm.retry_after:
        return False
    try:
        until = datetime.fromisoformat(dm.retry_after)
    except ValueError:
        return False
    return datetime.now(UTC) < until


def _backoff_error(dm: Datamodule) -> BackoffError:
    return BackoffError(
        f"'{dm.path}' is stale and its last build failed; no automatic retry before "
        f"{dm.retry_after} (last error: {dm.last_build_error})"
    )


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def _resolve_dependencies(
    dm: Datamodule, chain: frozenset[int], run: BuildRun
) -> list[tuple[str, str]]:
    """Refresh each dependency that is stale (TTL pull); return (local_name, path)."""
    attachments: list[tuple[str, str]] = []
    for dep in Dependency.get_for_datamodule(dm.id):
        dep_dm = Datamodule.get_by_id(dep.depends_on_id)
        if dep_dm is None:
            raise RuntimeError(f"dependency '{dep.local_name}': datamodule was deleted")
        if dep_dm.id in chain:
            raise RuntimeError(f"dependency cycle detected via '{dep_dm.path}'")
        dep_version = ensure_fresh(
            dep_dm, f"dependency:{dm.path}", dep_dm.ttl_seconds, _stack=chain, run=run
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
    dm: Datamodule,
    triggered_by: str,
    _stack: frozenset[int] = frozenset(),
    run: BuildRun | None = None,
) -> DatamoduleVersion:
    """Build unconditionally under the datamodule's lease (waiting for it if held).

    A forced build: if someone else is building, wait for them, then build the
    current script anyway. Stale dependencies are refreshed first.
    """
    run = run or BuildRun()
    lease = BuildLease.acquire_waiting(dm.id)
    try:
        fresh = Datamodule.get_by_id(dm.id)
        if fresh is None:
            raise RuntimeError("datamodule was deleted")
        return _build_leased(fresh, triggered_by, _stack, run)
    finally:
        lease.release()


def _build_leased(
    dm: Datamodule, triggered_by: str, _stack: frozenset[int], run: BuildRun
) -> DatamoduleVersion:
    """Build the datamodule into a new version and swap it live (lease held).

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
        dep_attachments = _resolve_dependencies(dm, chain, run)
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
                "UPDATE datamodule SET build_status = 'failed', last_build_error = ?, "
                "consecutive_failures = consecutive_failures + 1 WHERE id = ?",
                (str(exc), dm.id),
            )
            row = cur.execute(
                "SELECT consecutive_failures FROM datamodule WHERE id = ?", (dm.id,)
            ).fetchone()
            failures = int(row[0]) if row else 1
            retry_at = datetime.now(UTC) + timedelta(seconds=backoff_seconds(failures))
            cur.execute(
                "UPDATE datamodule SET retry_after = ? WHERE id = ?",
                (retry_at.isoformat(), dm.id),
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
            "storage_version = ?, last_build_error = NULL, consecutive_failures = 0, "
            "retry_after = NULL, modified_at = ? WHERE id = ?",
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
            deadline = datetime.fromisoformat(dm.valid_until)
        except ValueError:
            deadline = None
        if deadline is not None:
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=UTC)  # stored values are UTC
            if now > deadline:
                return True
    if effective_ttl is None:
        return False  # never auto-expire
    if effective_ttl <= 0:
        return True  # ephemeral: always rebuild
    built = version.build_ended_at or version.created_at
    try:
        built_at = datetime.fromisoformat(built)
    except ValueError:
        return True
    if built_at.tzinfo is None:
        built_at = built_at.replace(tzinfo=UTC)
    return (now - built_at).total_seconds() > effective_ttl


def is_stale(dm: Datamodule, version: DatamoduleVersion, effective_ttl: int | None) -> bool:
    return _is_stale(dm, version, effective_ttl)


def ensure_fresh(
    dm: Datamodule,
    triggered_by: str,
    effective_ttl: int | None,
    _stack: frozenset[int] = frozenset(),
    run: BuildRun | None = None,
) -> DatamoduleVersion:
    """Return a ready live version, rebuilding under single-flight if stale.

    ``effective_ttl``: None = never auto-expire, <=0 = always rebuild.
    ``_stack`` carries the current build chain for dependency cycle detection.
    For an automatic ``run``, a stale datamodule in failure backoff raises
    BackoffError instead of being rebuilt.
    """
    run = run or BuildRun()
    version = live_version(dm)
    if version is not None and not _is_stale(dm, version, effective_ttl):
        return version
    if run.auto and in_backoff(dm):
        raise _backoff_error(dm)

    lease = BuildLease.acquire_waiting(dm.id)
    try:
        # Re-read under the lease: the builder we waited for may have rebuilt it.
        fresh = Datamodule.get_by_id(dm.id)
        if fresh is None:
            raise RuntimeError("datamodule was deleted")
        version = live_version(fresh)
        if version is not None and not _is_stale(fresh, version, effective_ttl):
            return version
        if run.auto and in_backoff(fresh):
            raise _backoff_error(fresh)
        return _build_leased(fresh, triggered_by, _stack, run)
    finally:
        lease.release()


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

    report.stuck_builds = reap_dead_builds(dry_run=dry_run)
    for dm in dms:
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
