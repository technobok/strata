"""Build jobs: coalescing, claiming, the worker loop, and orphan reaping."""

import threading
from datetime import UTC, datetime, timedelta

import pytest

from tests.conftest import Strata


@pytest.fixture
def queued(strata: Strata) -> Strata:
    """No inline builds: requests only queue jobs, as in production."""
    strata.app.config["WORKER_INLINE"] = False
    return strata


def _enqueue(strata: Strata, dm_uuid: str, force: bool, reason: str = "test"):
    with strata.app.app_context():
        from strata.models.build_job import BuildJob

        return BuildJob.enqueue(strata.datamodule(dm_uuid).id, "default", reason, force)


def _jobs(strata: Strata, dm_uuid: str):
    with strata.app.app_context():
        from strata.models.build_job import BuildJob

        return BuildJob.recent_for_datamodule(strata.datamodule(dm_uuid).id)


def _worker(strata: Strata, queues: list[str] | None = None):
    from strata.worker import Worker

    return Worker(strata.app, queues or ["default"], concurrency=1)


def _set_running(strata: Strata, job_id: int, worker_id: str = "w1") -> None:
    with strata.app.app_context():
        from strata.db import transaction

        with transaction() as cur:
            cur.execute(
                "UPDATE build_job SET status = 'running', started_at = ?, worker_id = ? WHERE id = ?",
                ((datetime.now(UTC) - timedelta(minutes=10)).isoformat(), worker_id, job_id),
            )


def test_requests_coalesce_into_one_queued_job(queued: Strata) -> None:
    dm_uuid = queued.new_datamodule()
    a = _enqueue(queued, dm_uuid, force=False)
    b = _enqueue(queued, dm_uuid, force=False)
    c = _enqueue(queued, dm_uuid, force=True)
    assert a.id == b.id == c.id
    [job] = _jobs(queued, dm_uuid)
    assert job.status == "queued" and job.force


def test_a_running_job_absorbs_reads_but_not_forced_requests(queued: Strata) -> None:
    dm_uuid = queued.new_datamodule()
    first = _enqueue(queued, dm_uuid, force=False)
    _set_running(queued, first.id)
    assert _enqueue(queued, dm_uuid, force=False).id == first.id
    follow_up = _enqueue(queued, dm_uuid, force=True)
    assert follow_up.id != first.id and follow_up.status == "queued"


def test_build_button_queues_and_the_worker_builds(queued: Strata) -> None:
    dm_uuid = queued.new_datamodule()
    queued.build(dm_uuid)
    assert any("queued" in msg for _, msg in queued.flashes())
    assert queued.versions(dm_uuid) == []
    [job] = _jobs(queued, dm_uuid)
    assert job.status == "queued" and job.reason == "manual:tester"

    assert _worker(queued).drain() == 1
    [job] = _jobs(queued, dm_uuid)
    assert job.status == "done" and job.version_id is not None
    assert queued.datamodule(dm_uuid).build_status == "ready"
    [version] = queued.versions(dm_uuid)
    assert version.triggered_by == "manual:tester"


def test_a_failed_build_fails_its_job(queued: Strata) -> None:
    dm_uuid = queued.new_datamodule(script="SELECT * FROM missing_table")
    queued.build(dm_uuid)
    _worker(queued).drain()
    [job] = _jobs(queued, dm_uuid)
    assert job.status == "failed" and "missing_table" in (job.error or "")


def test_an_unforced_job_skips_a_fresh_datamodule(queued: Strata) -> None:
    dm_uuid = queued.new_datamodule()  # ttl blank: never stale once built
    queued.build(dm_uuid)
    _worker(queued).drain()
    _enqueue(queued, dm_uuid, force=False, reason="stale-read")
    _worker(queued).drain()
    jobs = _jobs(queued, dm_uuid)
    assert [j.status for j in jobs] == ["skipped", "done"]
    assert len(queued.versions(dm_uuid)) == 1


def test_one_running_job_per_datamodule_and_queues_are_separate(queued: Strata) -> None:
    a = queued.new_datamodule(path="a")
    b = queued.new_datamodule(path="b")
    resp = queued.client.post(
        f"/datamodules/{b}/edit", data={"path": "b", "build_script": "", "build_queue": "long"}
    )
    assert resp.status_code == 302
    job_a = _enqueue(queued, a, force=True)
    _set_running(queued, job_a.id)
    _enqueue(queued, a, force=True)  # follow-up behind the running job
    queued.build(b)  # goes to queue 'long'
    with queued.app.app_context():
        from strata.models.build_job import BuildJob

        assert BuildJob.claim_next(["default"], "w2") is None  # a is busy; b is on 'long'
        claimed = BuildJob.claim_next(["long"], "w2")
        assert claimed is not None and claimed.datamodule_id == queued.datamodule(b).id


def test_concurrent_claims_never_take_the_same_job(queued: Strata) -> None:
    dms = [queued.new_datamodule(path=f"dm{i}") for i in range(6)]
    for dm_uuid in dms:
        _enqueue(queued, dm_uuid, force=True)
    claimed: list[int] = []
    lock = threading.Lock()

    def claim_all(name: str) -> None:
        with queued.app.app_context():
            from strata.models.build_job import BuildJob

            while (job := BuildJob.claim_next(["default"], name)) is not None:
                with lock:
                    claimed.append(job.id)

    threads = [threading.Thread(target=claim_all, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(claimed) == 6 and len(set(claimed)) == 6


def test_sweep_fails_jobs_of_a_vanished_worker(queued: Strata) -> None:
    dm_uuid = queued.new_datamodule()
    job = _enqueue(queued, dm_uuid, force=True)
    _set_running(queued, job.id, worker_id="ghost:1:abc")
    _worker(queued).sweep()
    [job] = _jobs(queued, dm_uuid)
    assert job.status == "failed" and "stopped" in (job.error or "")


def test_sweep_spares_a_running_job_whose_build_holds_the_lease(queued: Strata) -> None:
    dm_uuid = queued.new_datamodule()
    job = _enqueue(queued, dm_uuid, force=True)
    _set_running(queued, job.id, worker_id="inline:web:1")
    with queued.app.app_context():
        from strata.services.datamodule_service import BuildLease

        lease = BuildLease.try_acquire(queued.datamodule(dm_uuid).id)
        assert lease is not None
        try:
            _worker(queued).sweep()
        finally:
            lease.release()
    [job] = _jobs(queued, dm_uuid)
    assert job.status == "running"


def test_worker_registers_and_unregisters(queued: Strata) -> None:
    worker = _worker(queued)
    worker.stop.set()  # run() registers, sees stop, and exits cleanly
    seen: list[list[str]] = []
    from strata.models import worker_process

    original = worker_process.WorkerProcess.register

    def spy(*args, **kwargs):
        original(*args, **kwargs)
        seen.append([w.id for w in worker_process.WorkerProcess.alive()])

    worker_process.WorkerProcess.register = staticmethod(spy)  # type: ignore[method-assign]
    try:
        worker.run()
    finally:
        worker_process.WorkerProcess.register = staticmethod(original)  # type: ignore[method-assign]
    assert seen == [[worker.id]]
    with queued.app.app_context():
        assert worker_process.WorkerProcess.alive() == []


def test_sweep_keeps_jobs_of_a_live_worker(queued: Strata) -> None:
    dm_uuid = queued.new_datamodule()
    job = _enqueue(queued, dm_uuid, force=True)
    _set_running(queued, job.id, worker_id="alive:1:abc")
    with queued.app.app_context():
        from strata.models.worker_process import WorkerProcess

        WorkerProcess.register("alive:1:abc", "alive", 1, ["default"], 1)
        WorkerProcess.register("ghost:2:def", "ghost", 2, ["default"], 1)
    _worker(queued).sweep()
    [job] = _jobs(queued, dm_uuid)
    assert job.status == "running"


def test_edit_page_shows_jobs_and_a_missing_worker(queued: Strata) -> None:
    dm_uuid = queued.new_datamodule()
    queued.build(dm_uuid)
    page = queued.client.get(f"/datamodules/{dm_uuid}/edit").get_data(as_text=True)
    assert "manual:tester" in page
    assert "No strata-worker has checked in" in page
    assert 'hx-trigger="every 2s"' in page  # polls while the build is queued


def test_status_fragment_reloads_the_page_when_the_build_is_done(queued: Strata) -> None:
    dm_uuid = queued.new_datamodule()
    queued.build(dm_uuid)
    busy = queued.client.get(f"/datamodules/{dm_uuid}/status")
    assert "HX-Refresh" not in busy.headers
    _worker(queued).drain()
    idle = queued.client.get(f"/datamodules/{dm_uuid}/status")
    assert idle.headers.get("HX-Refresh") == "true"


def test_admin_page_lists_live_workers(queued: Strata) -> None:
    assert "No strata-worker" in queued.client.get("/admin/").get_data(as_text=True)
    with queued.app.app_context():
        from strata.models.worker_process import WorkerProcess

        WorkerProcess.register("box:42:abc", "box", 42, ["default", "long"], 3)
    page = queued.client.get("/admin/").get_data(as_text=True)
    assert "box:42:abc" in page and "default,long" in page
