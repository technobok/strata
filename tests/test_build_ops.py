"""Build-path stopgaps: the manual build lock, and reconcile's crash cleanup."""

import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.conftest import Strata

TWO_DAYS_AGO = time.time() - 2 * 86400


def test_manual_build_waits_for_the_single_flight_lock(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    from strata.services import datamodule_service

    lock = datamodule_service._lock_for(dm_uuid)
    lock.acquire()
    done = threading.Event()

    def press_build() -> None:
        strata.app.test_client().post(f"/datamodules/{dm_uuid}/build")
        done.set()

    worker = threading.Thread(target=press_build)
    try:
        worker.start()
        assert not done.wait(timeout=0.5), "the build ran without taking the lock"
    finally:
        lock.release()
    worker.join(timeout=30)
    assert done.is_set()
    assert strata.datamodule(dm_uuid).build_status == "ready"


def _reconcile(strata: Strata, dry_run: bool):
    with strata.app.app_context():
        from strata.services import datamodule_service

        return datamodule_service.reconcile(dry_run=dry_run)


def _stuck_build(strata: Strata, dm_uuid: str, started: datetime) -> str:
    """A version row left 'building' by a process that died."""
    with strata.app.app_context():
        from strata.db import transaction
        from strata.models.datamodule_version import DatamoduleVersion

        dm = strata.datamodule(dm_uuid)
        version = DatamoduleVersion.create_building(dm.id, "ttl-pull", dm.build_script)
        with transaction() as cur:
            cur.execute(
                "UPDATE datamodule_version SET build_started_at = ?, created_at = ? WHERE id = ?",
                (started.isoformat(), started.isoformat(), version.id),
            )
            cur.execute("UPDATE datamodule SET build_status = 'building' WHERE id = ?", (dm.id,))
        return version.uuid


def test_reconcile_marks_long_dead_builds_failed(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    stuck = _stuck_build(strata, dm_uuid, datetime.now(UTC) - timedelta(days=2))

    preview = _reconcile(strata, dry_run=True)
    assert preview.stuck_builds == [f"test/sales: {stuck[:8]}"]
    assert strata.datamodule(dm_uuid).build_status == "building"

    _reconcile(strata, dry_run=False)
    [version] = strata.versions(dm_uuid)
    assert version.status == "failed"
    dm = strata.datamodule(dm_uuid)
    assert dm.build_status == "failed"
    assert "never finished" in (dm.last_build_error or "")


def test_reconcile_leaves_a_recent_build_alone(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    _stuck_build(strata, dm_uuid, datetime.now(UTC) - timedelta(minutes=5))
    report = _reconcile(strata, dry_run=False)
    assert report.stuck_builds == []
    [version] = strata.versions(dm_uuid)
    assert version.status == "building"


def test_reconcile_sweeps_old_scratch_but_not_new(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    strata.build(dm_uuid)
    root = Path(strata.app.config["DATA_ROOT"])
    tmp = root / dm_uuid[:2] / dm_uuid / "tmp"
    old_wal = tmp / "dead.duckdb.wal"
    old_spill = tmp / "dead.duckdb.tmp"
    old_export = root / "_exports" / "tmpabc.csv"
    fresh_export = root / "_exports" / "tmpnew.csv"
    old_spill.mkdir(parents=True)
    old_export.parent.mkdir(parents=True, exist_ok=True)
    for f in (old_wal, old_export, fresh_export):
        f.write_text("x")
    for p in (old_wal, old_spill, old_export):
        os.utime(p, (TWO_DAYS_AGO, TWO_DAYS_AGO))

    report = _reconcile(strata, dry_run=False)
    assert str(old_wal) in report.stale_tmp and str(old_spill) in report.stale_tmp
    assert report.stale_scratch == [str(old_export)]
    assert not old_wal.exists() and not old_spill.exists() and not old_export.exists()
    assert fresh_export.exists()
