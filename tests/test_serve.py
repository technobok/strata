"""The public serve path: formats, args, the filter grammar, version pinning."""

import json

import pytest

from tests.conftest import Strata


@pytest.fixture
def report(strata: Strata) -> tuple[str, str]:
    dm_uuid = strata.new_datamodule()
    strata.build(dm_uuid)
    return dm_uuid, strata.new_report(dm_uuid)


def _rows(strata: Strata, url: str) -> list[dict]:
    resp = strata.client.get(url)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return json.loads(resp.get_data(as_text=True))


def test_html_renders_the_table(strata: Strata, report: tuple[str, str]) -> None:
    _, rpt = report
    resp = strata.client.get(f"/r/{rpt}.html")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "North" in body and "South" in body


@pytest.mark.parametrize("ext", ["csv", "json", "parquet", "xlsx"])
def test_data_formats_stream(strata: Strata, report: tuple[str, str], ext: str) -> None:
    _, rpt = report
    resp = strata.client.get(f"/r/{rpt}.{ext}")
    assert resp.status_code == 200
    data = resp.get_data()
    assert data
    assert resp.headers["Content-Length"] == str(len(data))
    assert resp.headers["X-Row-Count"] == "3"


def test_filters_sort_and_search(strata: Strata, report: tuple[str, str]) -> None:
    _, rpt = report
    rows = _rows(strata, f"/r/{rpt}.json?region=North&sort=-revenue")
    assert [r["product"] for r in rows] == ["A", "C"]
    rows = _rows(strata, f"/r/{rpt}.json?revenue__between=80,300&sort=revenue")
    assert [r["product"] for r in rows] == ["A", "B"]
    rows = _rows(strata, f"/r/{rpt}.json?q=sou")
    assert [r["product"] for r in rows] == ["B"]


def test_unknown_key_is_an_error(strata: Strata, report: tuple[str, str]) -> None:
    _, rpt = report
    resp = strata.client.get(f"/r/{rpt}.json?colour=red")
    assert resp.status_code == 400


def test_declared_args_bind(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    strata.build(dm_uuid)
    rpt = strata.new_report(
        dm_uuid,
        body="SELECT * FROM sales WHERE region = $region",
        args=[{"name": "region", "type": "text", "widget": "input", "required": "yes"}],
    )
    rows = _rows(strata, f"/r/{rpt}.json?region=South")
    assert [r["product"] for r in rows] == ["B"]


def test_version_pin_serves_an_old_snapshot(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule(script="CREATE TABLE t AS SELECT 1 AS n")
    strata.build(dm_uuid)
    [first] = strata.versions(dm_uuid)
    resp = strata.client.post(
        f"/datamodules/{dm_uuid}/edit",
        data={"path": "test/sales", "build_script": "CREATE TABLE t AS SELECT 2 AS n"},
    )
    assert resp.status_code == 302
    strata.build(dm_uuid)
    rpt = strata.new_report(dm_uuid, body="SELECT * FROM t")

    assert _rows(strata, f"/r/{rpt}.json") == [{"n": 2}]
    assert _rows(strata, f"/r/{rpt}.json?version={first.uuid}") == [{"n": 1}]


def test_unbuilt_datamodule_builds_on_first_serve(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    rpt = strata.new_report(dm_uuid)
    assert len(_rows(strata, f"/r/{rpt}.json")) == 3
    assert strata.datamodule(dm_uuid).build_status == "ready"
