"""Serving never builds: bounded wait, stale fallback, 'as of', and failure backoff."""

import json
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from tests.conftest import SALES_SCRIPT, Strata


def _jobs(strata: Strata, dm_uuid: str, reason: str = "stale-read"):
    """This datamodule's jobs for ``reason``, newest first (fixtures add manual ones)."""
    with strata.app.app_context():
        from strata.models.build_job import BuildJob

        jobs = BuildJob.recent_for_datamodule(strata.datamodule(dm_uuid).id)
        return [j for j in jobs if j.reason == reason]


def _worker(strata: Strata):
    from strata.worker import Worker

    return Worker(strata.app, ["default"], concurrency=1)


def _make_stale(strata: Strata, dm_uuid: str) -> None:
    """Age the live version past its TTL."""
    with strata.app.app_context():
        from strata.db import transaction

        dm = strata.datamodule(dm_uuid)
        old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        with transaction() as cur:
            cur.execute(
                "UPDATE datamodule_version SET build_ended_at = ? WHERE id = ?",
                (old, dm.live_version_id),
            )


@pytest.fixture
def stale_report(strata: Strata) -> tuple[str, str]:
    """A built datamodule (TTL 1h) whose live version is 2h old, with a report."""
    dm_uuid = strata.new_datamodule(ttl="3600")
    strata.build(dm_uuid)
    rpt = strata.new_report(dm_uuid)
    _make_stale(strata, dm_uuid)
    return dm_uuid, rpt


def test_fresh_data_carries_its_as_of_time(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    strata.build(dm_uuid)
    rpt = strata.new_report(dm_uuid)
    resp = strata.client.get(f"/r/{rpt}.csv")
    [version] = strata.versions(dm_uuid)
    assert resp.headers["X-Data-As-Of"] == version.build_ended_at
    assert "X-Data-Stale" not in resp.headers
    page = strata.client.get(f"/r/{rpt}.html").get_data(as_text=True)
    assert "Data as of" in page


def test_stale_data_is_rebuilt_when_the_build_finishes_in_time(
    strata: Strata, stale_report: tuple[str, str]
) -> None:
    dm_uuid, rpt = stale_report  # inline mode: the rebuild runs within the request
    resp = strata.client.get(f"/r/{rpt}.csv")
    assert resp.status_code == 200 and "X-Data-Stale" not in resp.headers
    assert len(strata.versions(dm_uuid)) == 2
    [job] = _jobs(strata, dm_uuid)
    assert job.reason == "stale-read" and not job.force and job.status == "done"


def test_the_request_waits_for_a_worker_to_finish(
    strata: Strata, stale_report: tuple[str, str]
) -> None:
    dm_uuid, rpt = stale_report
    strata.app.config.update(WORKER_INLINE=False, SERVE_MAX_WAIT_SECONDS=20)
    worker = _worker(strata)

    def run_soon() -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not worker.run_one():
            time.sleep(0.1)

    thread = threading.Thread(target=run_soon)
    thread.start()
    resp = strata.client.get(f"/r/{rpt}.csv")
    thread.join(timeout=30)
    assert resp.status_code == 200 and "X-Data-Stale" not in resp.headers
    assert len(strata.versions(dm_uuid)) == 2


def test_stale_data_is_served_when_the_rebuild_is_slow(
    strata: Strata, stale_report: tuple[str, str]
) -> None:
    dm_uuid, rpt = stale_report
    strata.app.config.update(WORKER_INLINE=False, SERVE_MAX_WAIT_SECONDS=0)
    resp = strata.client.get(f"/r/{rpt}.json")
    assert resp.status_code == 200
    assert resp.headers["X-Data-Stale"] == "1"
    assert len(json.loads(resp.get_data(as_text=True))) == 3
    page = strata.client.get(f"/r/{rpt}.html").get_data(as_text=True)
    assert "A rebuild is in progress" in page and "Showing data as of" in page
    [job] = _jobs(strata, dm_uuid)  # both requests joined one queued job
    assert job.status == "queued"


def test_never_built_is_503_for_data_and_a_waiting_page_for_html(strata: Strata) -> None:
    strata.app.config.update(WORKER_INLINE=False, SERVE_MAX_WAIT_SECONDS=0)
    dm_uuid = strata.new_datamodule()
    rpt = strata.new_report(dm_uuid)
    resp = strata.client.get(f"/r/{rpt}.csv")
    assert resp.status_code == 503 and resp.headers["Retry-After"] == "30"
    page = strata.client.get(f"/r/{rpt}.html")
    assert page.status_code == 503
    assert 'http-equiv="refresh"' in page.get_data(as_text=True)


def _break_script(strata: Strata, dm_uuid: str) -> None:
    strata.set_script(dm_uuid, "CREATE TABLE sales AS SELECT * FROM no_such_table")


def test_a_failed_rebuild_serves_the_last_good_data_and_backs_off(
    strata: Strata, stale_report: tuple[str, str]
) -> None:
    dm_uuid, rpt = stale_report
    _break_script(strata, dm_uuid)

    resp = strata.client.get(f"/r/{rpt}.json")
    assert resp.status_code == 200 and resp.headers["X-Data-Stale"] == "1"
    assert len(json.loads(resp.get_data(as_text=True))) == 3  # the last good data
    dm = strata.datamodule(dm_uuid)
    assert dm.consecutive_failures == 1 and dm.retry_after is not None
    retry_at = datetime.fromisoformat(dm.retry_after)
    assert 50 <= (retry_at - datetime.now(UTC)).total_seconds() <= 61

    # Backed off: more requests do not retry the build.
    for _ in range(3):
        again = strata.client.get(f"/r/{rpt}.json")
        assert again.headers["X-Data-Stale"] == "1"
    assert len(_jobs(strata, dm_uuid)) == 1
    page = strata.client.get(f"/r/{rpt}.html").get_data(as_text=True)
    assert "could not be refreshed" in page and "no_such_table" in page


def test_a_manual_build_ignores_the_backoff_and_failures_grow_it(
    strata: Strata, stale_report: tuple[str, str]
) -> None:
    dm_uuid, rpt = stale_report
    _break_script(strata, dm_uuid)
    strata.client.get(f"/r/{rpt}.json")  # first failure: 60s backoff
    strata.build(dm_uuid)  # manual: runs despite the backoff, fails again
    dm = strata.datamodule(dm_uuid)
    assert dm.consecutive_failures == 2
    retry_at = datetime.fromisoformat(dm.retry_after or "")
    assert 110 <= (retry_at - datetime.now(UTC)).total_seconds() <= 121

    strata.set_script(dm_uuid, SALES_SCRIPT)
    strata.build(dm_uuid)
    dm = strata.datamodule(dm_uuid)
    assert dm.consecutive_failures == 0 and dm.retry_after is None


def test_a_dependency_in_backoff_fails_an_automatic_downstream_build(strata: Strata) -> None:
    up = strata.new_datamodule(path="core/sales", ttl="3600")
    strata.build(up)
    down = strata.new_datamodule(
        path="mart/totals",
        ttl="3600",
        script="CREATE TABLE totals AS SELECT sum(revenue) AS r FROM up.sales",
    )
    resp = strata.client.post(
        f"/datamodules/{down}/dependencies", data={"local_name": "up", "depends_on_uuid": up}
    )
    assert resp.status_code == 302
    strata.build(down)
    rpt = strata.new_report(down, body="SELECT * FROM totals", macro="totals")

    _break_script(strata, up)
    _make_stale(strata, up)
    strata.build(up)  # fails: up is now in backoff (and stale)
    _make_stale(strata, down)

    resp = strata.client.get(f"/r/{rpt}.json")
    assert resp.headers["X-Data-Stale"] == "1"  # down kept its last good version
    [job] = _jobs(strata, down)
    assert job.status == "skipped"
    assert "core/sales" in (job.error or "") and "no automatic retry" in (job.error or "")
