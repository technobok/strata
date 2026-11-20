"""Build -> atomic swap -> live pointer, failure handling, and retention."""

from pathlib import Path

from tests.conftest import Strata


def _version_file(strata: Strata, dm_uuid: str, version_uuid: str) -> Path:
    root = Path(strata.app.config["DATA_ROOT"])
    return root / dm_uuid[:2] / dm_uuid / "versions" / f"{version_uuid}.duckdb"


def test_build_publishes_a_live_version(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    strata.build(dm_uuid)

    dm = strata.datamodule(dm_uuid)
    assert dm.build_status == "ready"
    [version] = strata.versions(dm_uuid)
    assert version.status == "ready"
    assert dm.live_version_id == version.id
    assert _version_file(strata, dm_uuid, version.uuid).exists()
    assert version.build_script_snapshot == dm.build_script


def test_failed_build_keeps_the_previous_live_version(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    strata.build(dm_uuid)
    good = strata.datamodule(dm_uuid).live_version_id

    resp = strata.client.post(
        f"/datamodules/{dm_uuid}/edit",
        data={"path": "test/sales", "build_script": "SELECT * FROM no_such_table"},
    )
    assert resp.status_code == 302
    strata.build(dm_uuid)

    dm = strata.datamodule(dm_uuid)
    assert dm.build_status == "failed"
    assert "no_such_table" in (dm.last_build_error or "")
    assert dm.live_version_id == good
    statuses = sorted(v.status for v in strata.versions(dm_uuid))
    assert statuses == ["failed", "ready"]


def test_rebuild_supersedes_and_retention_prunes(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    resp = strata.client.post(
        f"/datamodules/{dm_uuid}/edit",
        data={
            "path": "test/sales",
            "build_script": "CREATE TABLE t AS SELECT 1 AS x",
            "retention_kind": "count",
            "retention_count": "2",
        },
    )
    assert resp.status_code == 302
    for _ in range(4):
        strata.build(dm_uuid)

    versions = strata.versions(dm_uuid)
    assert len(versions) == 2
    live = strata.datamodule(dm_uuid).live_version_id
    assert sorted(v.status for v in versions) == ["ready", "superseded"]
    assert any(v.id == live for v in versions)
    files = list(_version_file(strata, dm_uuid, "x").parent.glob("*.duckdb"))
    assert len(files) == 2
