"""Plain builds refresh only stale dependencies; 'with all dependencies' rebuilds them all."""

import pytest

from tests.conftest import Strata


@pytest.fixture
def diamond(strata: Strata) -> dict[str, str]:
    """top -> left, right; left, right -> base. No TTLs: nothing goes stale. All built."""
    base = strata.new_datamodule(path="base", script="CREATE TABLE t AS SELECT 1 AS n")
    left = strata.new_datamodule(path="left", script="CREATE TABLE t AS SELECT n FROM b.t")
    right = strata.new_datamodule(path="right", script="CREATE TABLE t AS SELECT n FROM b.t")
    top = strata.new_datamodule(
        path="top", script="CREATE TABLE t AS SELECT * FROM l.t UNION ALL SELECT * FROM r.t"
    )
    strata.add_dependency(left, base, "b")
    strata.add_dependency(right, base, "b")
    strata.add_dependency(top, left, "l")
    strata.add_dependency(top, right, "r")
    strata.build(top)  # first build: every dependency is missing, so all build
    return {"base": base, "left": left, "right": right, "top": top}


def _counts(strata: Strata, dms: dict[str, str]) -> dict[str, int]:
    return {name: len(strata.versions(uuid)) for name, uuid in dms.items()}


def test_first_build_builds_missing_dependencies_once(strata: Strata, diamond) -> None:
    assert _counts(strata, diamond) == {"base": 1, "left": 1, "right": 1, "top": 1}


def test_a_plain_build_leaves_fresh_dependencies_alone(strata: Strata, diamond) -> None:
    strata.build(diamond["top"])
    assert _counts(strata, diamond) == {"base": 1, "left": 1, "right": 1, "top": 2}


def test_a_plain_build_refreshes_a_stale_dependency(strata: Strata, diamond) -> None:
    resp = strata.client.post(
        f"/datamodules/{diamond['left']}/edit",
        data={
            "path": "left",
            "build_script": "CREATE TABLE t AS SELECT n FROM b.t",
            "ttl_seconds": "0",
        },
    )
    assert resp.status_code == 302
    strata.build(diamond["top"])
    assert _counts(strata, diamond) == {"base": 1, "left": 2, "right": 1, "top": 2}


def test_build_with_all_dependencies_rebuilds_each_once(strata: Strata, diamond) -> None:
    resp = strata.client.post(f"/datamodules/{diamond['top']}/build", data={"cascade": "1"})
    assert resp.status_code == 302
    assert _counts(strata, diamond) == {"base": 2, "left": 2, "right": 2, "top": 2}
    with strata.app.app_context():
        from strata.models.build_job import BuildJob

        [job, _] = BuildJob.recent_for_datamodule(strata.datamodule(diamond["top"]).id)
    assert job.cascade and job.force and job.status == "done"


def test_a_failing_dependency_fails_the_cascade_and_keeps_top_live(strata: Strata, diamond) -> None:
    live_before = strata.datamodule(diamond["top"]).live_version_id
    strata.set_script(diamond["base"], "CREATE TABLE t AS SELECT * FROM nowhere")
    strata.client.post(f"/datamodules/{diamond['top']}/build", data={"cascade": "1"})
    top = strata.datamodule(diamond["top"])
    assert top.build_status == "failed" and "nowhere" in (top.last_build_error or "")
    assert top.live_version_id == live_before


def test_the_button_only_appears_with_dependencies(strata: Strata, diamond) -> None:
    with_deps = strata.client.get(f"/datamodules/{diamond['top']}/edit").get_data(as_text=True)
    without = strata.client.get(f"/datamodules/{diamond['base']}/edit").get_data(as_text=True)
    assert "Build with all dependencies" in with_deps
    assert "Build with all dependencies" not in without


def test_a_cascade_request_upgrades_a_queued_job(strata: Strata) -> None:
    strata.app.config["WORKER_INLINE"] = False
    dm_uuid = strata.new_datamodule()
    with strata.app.app_context():
        from strata.models.build_job import BuildJob

        dm_id = strata.datamodule(dm_uuid).id
        first = BuildJob.enqueue(dm_id, "default", "stale-read", force=False)
        joined = BuildJob.enqueue(dm_id, "default", "manual:x", force=False, cascade=True)
        assert joined.id == first.id and joined.cascade and joined.force
