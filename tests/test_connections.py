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


# ---------------------------------------------------------------------------
# The connection form's Driver select fetches only the new driver's fields: the
# form used to re-submit itself as a GET, putting every typed field (a password
# too) into the URL.
# ---------------------------------------------------------------------------


def test_the_driver_select_sends_only_the_driver(strata: Strata) -> None:
    page = strata.client.get("/admin/connections/new").get_data(as_text=True)
    assert "method='get'" not in page and "form.submit" not in page
    assert 'hx-get="/admin/connections/params"' in page and 'hx-params="driver"' in page
    assert 'id="driver-params"' in page


def test_the_driver_fields_come_alone_and_echo_nothing_else(strata: Strata) -> None:
    resp = strata.client.get(
        "/admin/connections/params?driver=postgres&param_password=hunter2&name=leak"
    )
    assert resp.status_code == 200
    fields = resp.get_data(as_text=True)
    assert '<fieldset id="driver-params">' in fields
    assert 'name="param_host"' in fields and 'name="param_password"' in fields
    assert "hunter2" not in fields and "leak" not in fields


def test_the_driver_fields_of_a_connection_keep_its_secret_hint(strata: Strata) -> None:
    _new_connection(
        strata, "warehouse", "postgres", host="db1", database="dw", user="u", password="pw"
    )
    with strata.app.app_context():
        from strata.models.connection import Connection

        crow = Connection.get_by_name("warehouse")
        assert crow is not None
    fields = strata.client.get(
        f"/admin/connections/params?driver=postgres&connection={crow.uuid}"
    ).get_data(as_text=True)
    assert 'value="db1"' in fields and "leave blank to keep current" in fields
    assert 'value="pw"' not in fields
    assert (
        strata.client.get("/admin/connections/params?driver=x&connection=nope").status_code == 404
    )


def test_a_refused_connection_form_keeps_what_was_typed(strata: Strata) -> None:
    resp = strata.client.post(
        "/admin/connections/new",
        data={"name": "bad name", "driver": "postgres", "param_host": "db9"},
    )
    page = resp.get_data(as_text=True)
    assert resp.status_code == 200 and 'value="db9"' in page
    assert '<option value="postgres" selected>' in page


def test_the_driver_fields_are_for_admins(strata: Strata) -> None:
    resp = strata.client.get(
        "/admin/connections/params?driver=postgres", headers={"X-User": "viewer"}
    )
    assert resp.status_code == 302 and ("error", "Admin access required.") in strata.flashes()
