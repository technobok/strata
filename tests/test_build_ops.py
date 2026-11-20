"""Build leases (cross-process single-flight) and reconcile's crash cleanup."""

import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.conftest import Strata

TWO_DAYS_AGO = time.time() - 2 * 86400


def _dm_id(strata: Strata, dm_uuid: str) -> int:
    return strata.datamodule(dm_uuid).id


def _dead_builder(strata: Strata, dm_uuid: str, lease_until: datetime) -> str:
    """Simulate a builder that crashed: a lease it no longer renews + its 'building' row."""
    with strata.app.app_context():
        from strata.db import transaction
        from strata.models.datamodule_version import DatamoduleVersion

        dm = strata.datamodule(dm_uuid)
        version = DatamoduleVersion.create_building(dm.id, "ttl-pull", dm.build_script)
        with transaction() as cur:
            cur.execute(
                "UPDATE datamodule SET build_status = 'building', build_token = 'dead', "
                "build_lease_until = ? WHERE id = ?",
                (lease_until.isoformat(), dm.id),
            )
        return version.uuid


def test_a_held_lease_cannot_be_taken(strata: Strata) -> None:
    dm_id = _dm_id(strata, strata.new_datamodule())
    with strata.app.app_context():
        from strata.services.datamodule_service import BuildLease

        first = BuildLease.try_acquire(dm_id)
        assert first is not None
        assert BuildLease.try_acquire(dm_id) is None
        first.release()
        second = BuildLease.try_acquire(dm_id)
        assert second is not None
        second.release()


def test_manual_build_waits_for_the_lease(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    with strata.app.app_context():
        from strata.services.datamodule_service import BuildLease

        lease = BuildLease.try_acquire(_dm_id(strata, dm_uuid))
        assert lease is not None
    done = threading.Event()

    def press_build() -> None:
        strata.app.test_client().post(f"/datamodules/{dm_uuid}/build")
        done.set()

    worker = threading.Thread(target=press_build)
    try:
        worker.start()
        assert not done.wait(timeout=1.5), "the build ran while another process held the lease"
    finally:
        with strata.app.app_context():
            lease.release()
    worker.join(timeout=30)
    assert done.is_set()
    assert strata.datamodule(dm_uuid).build_status == "ready"


def test_an_expired_lease_is_taken_over_and_its_rows_failed(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    _dead_builder(strata, dm_uuid, datetime.now(UTC) - timedelta(seconds=5))
    strata.build(dm_uuid)
    statuses = sorted(v.status for v in strata.versions(dm_uuid))
    assert statuses == ["failed", "ready"]
    failed = next(v for v in strata.versions(dm_uuid) if v.status == "failed")
    assert "died" in (failed.error_message or "")


def test_the_lease_is_renewed_during_a_long_build(strata: Strata, monkeypatch) -> None:
    from strata.services import datamodule_service

    monkeypatch.setattr(datamodule_service, "HEARTBEAT_SECONDS", 0.2)
    dm_uuid = strata.new_datamodule()
    seen: list[str] = []
    with strata.app.app_context():
        lease = datamodule_service.BuildLease.try_acquire(_dm_id(strata, dm_uuid))
        assert lease is not None
        first = strata.datamodule(dm_uuid).build_lease_until
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = strata.datamodule(dm_uuid).build_lease_until
            if current != first:
                seen.append(current)
                break
            time.sleep(0.1)
        lease.release()
    assert seen, "the heartbeat never moved build_lease_until"
    assert strata.datamodule(dm_uuid).build_token is None


def _reconcile(strata: Strata, dry_run: bool):
    with strata.app.app_context():
        from strata.services import datamodule_service

        return datamodule_service.reconcile(dry_run=dry_run)


def test_reconcile_fails_builds_whose_lease_expired(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    stuck = _dead_builder(strata, dm_uuid, datetime.now(UTC) - timedelta(minutes=5))

    preview = _reconcile(strata, dry_run=True)
    assert preview.stuck_builds == [f"test/sales: {stuck[:8]}"]
    assert strata.datamodule(dm_uuid).build_status == "building"

    _reconcile(strata, dry_run=False)
    [version] = strata.versions(dm_uuid)
    assert version.status == "failed"
    dm = strata.datamodule(dm_uuid)
    assert dm.build_status == "failed"
    assert "died" in (dm.last_build_error or "")


def test_reconcile_leaves_a_build_with_a_live_lease_alone(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    _dead_builder(strata, dm_uuid, datetime.now(UTC) + timedelta(minutes=1))
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
