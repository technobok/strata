"""A build keeps only what its script creates: a script that leaves no table or
view of its own fails, and the previous version stays live."""

import pytest

from tests.conftest import Strata


def _assert_nothing_built(strata: Strata, dm_uuid: str) -> None:
    strata.build(dm_uuid)
    dm = strata.datamodule(dm_uuid)
    assert dm.build_status == "failed"
    error = dm.last_build_error or ""
    assert "created no tables or views" in error and "CREATE TABLE <name> AS SELECT" in error


@pytest.mark.parametrize(
    "script",
    [
        "SELECT 42 AS answer",  # rows thrown away
        "CREATE TEMP TABLE t AS SELECT 1 AS x",  # temp objects are not kept
        "CREATE MACRO m() AS 1",  # nothing a report can read
    ],
)
def test_a_script_that_creates_no_table_or_view_fails(strata: Strata, script: str) -> None:
    _assert_nothing_built(strata, strata.new_datamodule(script=script))


def test_the_previous_version_stays_live(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    strata.build(dm_uuid)
    live = strata.datamodule(dm_uuid).live_version_id
    report = strata.new_report(dm_uuid)
    strata.set_script(dm_uuid, "SELECT * FROM range(3)")
    _assert_nothing_built(strata, dm_uuid)
    assert strata.datamodule(dm_uuid).live_version_id == live
    assert [v.status for v in strata.versions(dm_uuid)].count("failed") == 1
    assert strata.client.get(f"/r/{report}.csv").get_data(as_text=True).count("\n") == 4


def test_a_view_is_enough(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule(script="CREATE VIEW v AS SELECT 1 AS x")
    strata.build(dm_uuid)
    assert strata.datamodule(dm_uuid).build_status == "ready"


def test_a_dependencys_tables_do_not_count(strata: Strata) -> None:
    upstream = strata.new_datamodule(path="test/upstream")
    strata.build(upstream)
    dm_uuid = strata.new_datamodule(path="test/reader", script="SELECT * FROM up.sales")
    strata.add_dependency(dm_uuid, upstream, "up")
    _assert_nothing_built(strata, dm_uuid)
