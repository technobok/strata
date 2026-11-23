"""Connections in builds: a build script reads each bound connection by its local
name (an ODBC handle is ``getvariable('<local name>')``), the datamodule page shows
how, and a build that reads an ODBC handle that is not there says which ones are.
"""

import dataclasses
import html
from typing import Any

import pytest

from strata.services import connection_service, datamodule_service
from tests.conftest import Strata


class _Recorder:
    def __init__(self) -> None:
        self.sql: list[str] = []

    def execute(self, sql: str) -> None:
        self.sql.append(sql)


def _new_connection(
    strata: Strata, name: str, driver: str, may_use: str = "@strata-writers", **params: str
) -> None:
    """A connection the writers may use (the owners of a datamodule on the defaults)."""
    data = {
        "name": name,
        "driver": driver,
        "may_use": may_use,
        **{f"param_{k}": v for k, v in params.items()},
    }
    resp = strata.client.post("/admin/connections/new", data=data)
    assert resp.status_code == 302, strata.flashes()


def _bind(strata: Strata, dm_uuid: str, connection: str, local_name: str) -> None:
    with strata.app.app_context():
        from strata.models.connection import Connection

        crow = Connection.get_by_name(connection)
        assert crow is not None
        conn_id = crow.id
    resp = strata.client.post(
        f"/datamodules/{dm_uuid}/connections",
        data={"connection_id": str(conn_id), "local_name": local_name},
    )
    assert resp.status_code == 302
    assert any("Bound connection" in msg for _cat, msg in strata.flashes())


@pytest.fixture
def odbc_without_a_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real odbc_scanner extension, but a handle that needs no SQL Server."""

    def attach(conn: Any, alias: str, _params: dict) -> None:
        conn.execute(f"SET VARIABLE {alias} = 42")

    spec = connection_service.DRIVERS["odbc"]
    monkeypatch.setitem(
        connection_service.DRIVERS, "odbc", dataclasses.replace(spec, attach=attach)
    )


def test_the_odbc_handle_is_the_local_name() -> None:
    rec = _Recorder()
    connection_service._odbc_attach(rec, "navi_brs", {"connection_string": "DSN=x;PWD=it's"})  # type: ignore[arg-type]
    assert rec.sql == ["SET VARIABLE navi_brs = odbc_connect('DSN=x;PWD=it''s')"]


def test_the_page_shows_how_the_script_reads_each_binding(strata: Strata, tmp_path) -> None:
    _new_connection(strata, "coredb", "odbc", connection_string="DSN=coredb")
    _new_connection(strata, "local_db", "sqlite", path=str(tmp_path / "x.sqlite3"))
    dm_uuid = strata.new_datamodule()
    _bind(strata, dm_uuid, "coredb", "navi_brs")
    _bind(strata, dm_uuid, "local_db", "ext")
    page = html.unescape(strata.client.get(f"/datamodules/{dm_uuid}/edit").get_data(as_text=True))
    assert "<code>odbc_query(getvariable('navi_brs'), '<SQL Server query>')</code>" in page
    assert "<code>ext.<table></code>" in page


def test_a_missing_odbc_handle_names_the_bound_ones(
    strata: Strata, odbc_without_a_server: None
) -> None:
    _new_connection(strata, "coredb", "odbc", connection_string="DSN=coredb")
    dm_uuid = strata.new_datamodule(
        script="CREATE TABLE t AS SELECT * FROM odbc_query(getvariable('navi'), 'SELECT 1')"
    )
    _bind(strata, dm_uuid, "coredb", "navi_brs")
    strata.build(dm_uuid)
    dm = strata.datamodule(dm_uuid)
    assert dm.build_status == "failed"
    error = dm.last_build_error or ""
    assert "ODBC connection must be not NULL" in error  # DuckDB's own words stay
    assert "This datamodule's ODBC connections: getvariable('navi_brs')." in error


def test_odbc_query_with_no_odbc_binding_says_to_bind_one(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule(
        script="CREATE TABLE t AS SELECT * FROM odbc_query(getvariable('navi'), 'SELECT 1')"
    )
    strata.build(dm_uuid)
    error = strata.datamodule(dm_uuid).last_build_error or ""
    assert "This datamodule has no ODBC connection bound" in error


def test_other_build_errors_get_no_connection_hint(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule(script="CREATE TABLE t AS SELECT * FROM nowhere")
    strata.build(dm_uuid)
    error = strata.datamodule(dm_uuid).last_build_error or ""
    assert "nowhere" in error and "getvariable" not in error and "ODBC" not in error
    assert datamodule_service._connection_hint("Catalog Error: x", []) is None
