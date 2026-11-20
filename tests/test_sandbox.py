"""The DuckDB sandbox (services/duck.py) and single-SELECT report SQL."""

import json
from pathlib import Path

import pytest

from tests.conftest import Strata

BREAKOUT = (
    "SELECT 1 AS a); CREATE TABLE pwned AS SELECT 42 AS x; CREATE TEMP MACRO z() AS TABLE (SELECT 1"
)


def _last_error(strata: Strata, dm_uuid: str) -> str:
    return strata.datamodule(dm_uuid).last_build_error or ""


# -- report SQL shape ---------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [BREAKOUT, "SELECT 1; DROP TABLE sales", "CREATE TABLE x AS SELECT 1", "COPY sales TO 'x.csv'"],
)
def test_report_body_must_be_one_select(strata: Strata, body: str) -> None:
    dm_uuid = strata.new_datamodule()
    resp = strata.client.post(
        f"/datamodules/{dm_uuid}/reports/new",
        data={"name": "Bad", "macro_name": "bad", "body_sql": body},
    )
    assert resp.status_code == 200  # the form re-renders with the error
    assert "The report body" in resp.get_data(as_text=True)
    with strata.app.app_context():
        from strata.models.report import Report

        assert Report.get_for_datamodule(strata.datamodule(dm_uuid).id) == []


def test_constraint_query_must_be_one_select(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    resp = strata.client.post(
        f"/datamodules/{dm_uuid}/reports/new",
        data={
            "name": "R",
            "macro_name": "r",
            "body_sql": "SELECT * FROM sales WHERE region = $region",
            "arg_name": ["region"],
            "arg_type": ["text"],
            "arg_widget": ["select"],
            "arg_constraint": ["SELECT region FROM sales; DROP TABLE sales"],
        },
    )
    assert resp.status_code == 200  # the form re-renders with the error
    assert "The constraint query for &#39;region&#39;" in resp.get_data(as_text=True)


def test_a_stored_breakout_body_is_refused_at_serve(strata: Strata) -> None:
    """A body that got into the DB some other way never reaches CREATE MACRO."""
    dm_uuid = strata.new_datamodule()
    strata.build(dm_uuid)
    rpt = strata.new_report(dm_uuid)
    with strata.app.app_context():
        from strata.db import transaction

        with transaction() as cur:
            cur.execute("UPDATE report SET body_sql = ? WHERE uuid = ?", (BREAKOUT, rpt))
    resp = strata.client.get(f"/r/{rpt}.json")
    assert resp.status_code == 400
    assert resp.get_data(as_text=True).startswith("The report body")


# -- serve sandbox ---------------------------------------------------------------


def test_report_cannot_read_host_files(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    strata.build(dm_uuid)
    rpt = strata.new_report(dm_uuid, body="SELECT * FROM read_text('/etc/hostname')")
    resp = strata.client.get(f"/r/{rpt}.json")
    assert resp.status_code == 400
    assert "Permission" in resp.get_data(as_text=True)


def test_exports_land_in_the_exports_dir_and_are_removed(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    strata.build(dm_uuid)
    rpt = strata.new_report(dm_uuid)
    resp = strata.client.get(f"/r/{rpt}.csv")
    assert resp.status_code == 200
    resp.get_data()  # drain the stream so the temp file is unlinked
    resp.close()
    exports = Path(strata.app.config["DATA_ROOT"]) / "_exports"
    assert exports.is_dir()
    assert list(exports.iterdir()) == []


# -- build sandbox ---------------------------------------------------------------


def test_build_reads_its_own_sources(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    strata.upload_source(dm_uuid, "prices.csv", b"item,price\nA,1.5\nB,2.5\n")
    strata.set_script(
        dm_uuid, "CREATE TABLE prices AS SELECT * FROM read_csv(source('prices.csv'))"
    )
    strata.build(dm_uuid)
    assert strata.datamodule(dm_uuid).build_status == "ready", _last_error(strata, dm_uuid)


def test_build_cannot_read_host_files(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    strata.set_script(dm_uuid, "CREATE TABLE h AS SELECT * FROM read_text('/etc/hostname')")
    strata.build(dm_uuid)
    assert strata.datamodule(dm_uuid).build_status == "failed"
    assert "Permission" in _last_error(strata, dm_uuid)


def test_build_cannot_attach_the_metadata_db(strata: Strata) -> None:
    db_path = strata.app.config["DATABASE_PATH"]
    dm_uuid = strata.new_datamodule()
    strata.set_script(
        dm_uuid,
        f"ATTACH '{db_path}' AS meta (TYPE sqlite);\n"
        "CREATE TABLE stolen AS SELECT * FROM meta.app_setting",
    )
    strata.build(dm_uuid)
    assert strata.datamodule(dm_uuid).build_status == "failed"
    assert "stolen" not in _last_error(strata, dm_uuid)


def test_build_cannot_read_another_datamodules_version(strata: Strata) -> None:
    victim = strata.new_datamodule(path="secret/payroll")
    strata.build(victim)
    [version] = strata.versions(victim)
    root = Path(strata.app.config["DATA_ROOT"])
    victim_file = root / victim[:2] / victim / "versions" / f"{version.uuid}.duckdb"

    dm_uuid = strata.new_datamodule()
    strata.set_script(
        dm_uuid, f"ATTACH '{victim_file}' AS v (READ_ONLY); CREATE TABLE s AS FROM v.sales"
    )
    strata.build(dm_uuid)
    assert strata.datamodule(dm_uuid).build_status == "failed"
    assert "Permission" in _last_error(strata, dm_uuid)


def test_build_can_still_use_dependencies(strata: Strata) -> None:
    upstream = strata.new_datamodule(path="core/sales")
    downstream = strata.new_datamodule(
        path="mart/sales", script="CREATE TABLE totals AS SELECT sum(revenue) AS r FROM up.sales"
    )
    resp = strata.client.post(
        f"/datamodules/{downstream}/dependencies",
        data={"local_name": "up", "depends_on_uuid": upstream},
    )
    assert resp.status_code == 302
    strata.build(downstream)
    assert strata.datamodule(downstream).build_status == "ready", _last_error(strata, downstream)
    rpt = strata.new_report(downstream, body="SELECT * FROM totals", macro="totals")
    rows = json.loads(strata.client.get(f"/r/{rpt}.json").get_data(as_text=True))
    assert rows == [{"r": 425.75}]


def test_build_sandbox_can_be_switched_off(strata: Strata) -> None:
    strata.app.config["BUILD_SANDBOX"] = False
    dm_uuid = strata.new_datamodule()
    strata.set_script(dm_uuid, "CREATE TABLE h AS SELECT * FROM read_text('/etc/hostname')")
    strata.build(dm_uuid)
    assert strata.datamodule(dm_uuid).build_status == "ready", _last_error(strata, dm_uuid)
