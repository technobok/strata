"""Who may use each connection (AC3).

A connection's "May use" list (blank = the admins only) says who may bind it to a
datamodule they own. Every owner of a datamodule must be on the list of each
connection bound to it: checked when binding, when the owners change, and at every
build, since Gatekeeper's groups change out of Strata's sight.
"""

from pathlib import Path
from typing import Any

import apsw
import pytest
from markupsafe import escape

from strata.db import _migration_files
from tests.conftest import FakeGatekeeper, Strata, as_user
from tests.test_connections import _new_connection

ALICE = as_user("alice")
SCRIPT = "CREATE TABLE t AS SELECT * FROM ext.items"


@pytest.fixture
def source_db(tmp_path: Path) -> str:
    path = str(tmp_path / "ext.sqlite3")
    conn = apsw.Connection(path)
    conn.execute("CREATE TABLE items (n INTEGER); INSERT INTO items VALUES (1), (2)")
    conn.close()
    return path


def _connection_id(strata: Strata, name: str) -> int:
    with strata.app.app_context():
        from strata.models.connection import Connection

        crow = Connection.get_by_name(name)
        assert crow is not None
        return crow.id


def _bind(strata: Strata, dm_uuid: str, name: str, headers: dict[str, str] | None = None) -> Any:
    return strata.client.post(
        f"/datamodules/{dm_uuid}/connections",
        data={"connection_id": str(_connection_id(strata, name)), "local_name": "ext"},
        headers=headers or {},
    )


def _bindings(strata: Strata, dm_uuid: str) -> list[Any]:
    with strata.app.app_context():
        from strata.models.datamodule_connection import DatamoduleConnection

        return DatamoduleConnection.get_for_datamodule(strata.datamodule(dm_uuid).id)


def _alices(strata: Strata, owners: str = "alice") -> str:
    dm_uuid = strata.new_datamodule(script=SCRIPT)
    assert strata.set_access(dm_uuid, owners=owners).status_code == 302
    return dm_uuid


def _edit_users(strata: Strata, name: str, may_use: str, source_db: str) -> Any:
    with strata.app.app_context():
        from strata.models.connection import Connection

        crow = Connection.get_by_name(name)
        assert crow is not None
        uuid = crow.uuid
    return strata.client.post(
        f"/admin/connections/{uuid}/edit",
        data={"name": name, "driver": "sqlite", "param_path": source_db, "may_use": may_use},
    )


def test_the_list_is_saved_and_shown(strata: Strata, source_db: str) -> None:
    _new_connection(strata, "ext_db", "sqlite", may_use="alice\n@brs-finance", path=source_db)
    _new_connection(strata, "admins_db", "sqlite", may_use="", path=source_db)
    page = strata.client.get("/admin/connections/").get_data(as_text=True)
    assert "alice, @brs-finance" in page and "admins only" in page
    _edit_users(strata, "ext_db", "*", source_db)
    with strata.app.app_context():
        from strata.models.connection import Connection

        assert Connection.get_by_name("ext_db").users == ["*"]  # type: ignore[union-attr]
        assert Connection.get_by_name("admins_db").users is None  # type: ignore[union-attr]


def test_a_bad_list_is_refused(strata: Strata, source_db: str, mail: list[Any]) -> None:
    for may_use, error in (
        (
            "two words",
            "May use: 'two words' is not a username, an @group or * (everyone signed in).",
        ),
        ("nobody", "May use: Gatekeeper has no user or group nobody."),
    ):
        data = {"name": "ext_db", "driver": "sqlite", "param_path": source_db, "may_use": may_use}
        resp = strata.client.post("/admin/connections/new", data=data)
        assert resp.status_code == 200 and str(escape(error)) in resp.get_data(as_text=True)
    _new_connection(strata, "ext_db", "sqlite", may_use="jsmith", path=source_db)
    resp = _edit_users(strata, "ext_db", "nobody", source_db)
    assert resp.status_code == 200
    with strata.app.app_context():
        from strata.models.connection import Connection

        assert Connection.get_by_name("ext_db").users == ["jsmith"]  # type: ignore[union-attr]


def test_an_owner_binds_only_what_they_may_use(strata: Strata, source_db: str) -> None:
    _new_connection(strata, "ext_db", "sqlite", may_use="alice", path=source_db)
    _new_connection(strata, "admins_db", "sqlite", may_use="", path=source_db)
    dm_uuid = _alices(strata)
    page = strata.client.get(f"/datamodules/{dm_uuid}/edit", headers=ALICE).get_data(as_text=True)
    assert ">ext_db (sqlite)<" in page and ">admins_db (sqlite)<" not in page
    assert _bind(strata, dm_uuid, "admins_db", headers=ALICE).status_code == 302
    assert ("error", "Choose a connection.") in strata.flashes()
    assert _bindings(strata, dm_uuid) == []
    assert _bind(strata, dm_uuid, "ext_db", headers=ALICE).status_code == 302
    assert [b.local_name for b in _bindings(strata, dm_uuid)] == ["ext"]


def test_every_owner_must_be_allowed_the_connection(
    strata: Strata, source_db: str, gatekeeper: FakeGatekeeper
) -> None:
    _new_connection(strata, "ext_db", "sqlite", may_use="alice", path=source_db)
    dm_uuid = _alices(strata, owners="alice\nbob")
    _bind(strata, dm_uuid, "ext_db", headers=ALICE)
    assert (
        "error",
        "Not bound: bob (owners of this datamodule) may not use the connection 'ext_db'. "
        'An admin can add them to the connection\'s "May use" list.',
    ) in strata.flashes()
    assert _bindings(strata, dm_uuid) == []


def test_new_owners_must_be_allowed_every_bound_connection(
    strata: Strata, source_db: str, gatekeeper: FakeGatekeeper
) -> None:
    _new_connection(strata, "ext_db", "sqlite", may_use="alice", path=source_db)
    dm_uuid = _alices(strata)
    _bind(strata, dm_uuid, "ext_db", headers=ALICE)
    resp = strata.set_access(dm_uuid, owners="alice\nbob", headers=ALICE)
    assert resp.status_code == 200
    assert str(escape("Access: bob may not use the connection 'ext_db' (bound as ext).")) in (
        resp.get_data(as_text=True)
    )
    assert strata.datamodule(dm_uuid).owners == ["alice"]


def test_every_build_checks_the_owners_may_use_its_connections(
    strata: Strata, source_db: str, gatekeeper: FakeGatekeeper
) -> None:
    _new_connection(strata, "ext_db", "sqlite", may_use="alice", path=source_db)
    dm_uuid = _alices(strata)
    _bind(strata, dm_uuid, "ext_db", headers=ALICE)
    strata.build(dm_uuid)
    assert strata.datamodule(dm_uuid).build_status == "ready"
    _edit_users(strata, "ext_db", "carol", source_db)  # an admin narrows the list
    assert any(
        "their next build fails and locks them: test/sales (alice)" in msg
        for _cat, msg in strata.flashes()
    )
    strata.build(dm_uuid)
    dm = strata.datamodule(dm_uuid)
    assert dm.build_status == "failed"
    assert dm.last_build_error == "alice may not use the connection 'ext_db' (bound as ext)"
    assert [v.status for v in strata.versions(dm_uuid)] == ["failed", "ready"]


def test_a_build_that_cannot_check_does_not_build(
    strata: Strata, source_db: str, gatekeeper: FakeGatekeeper
) -> None:
    _new_connection(strata, "ext_db", "sqlite", may_use="alice", path=source_db)
    dm_uuid = _alices(strata, owners="alice\n@brs-finance")
    _bind(strata, dm_uuid, "ext_db", headers=ALICE)  # refused: bob, carol, jsmith may not
    _edit_users(strata, "ext_db", "@brs-finance", source_db)
    _bind(strata, dm_uuid, "ext_db")  # an admin binds it: every owner is in @brs-finance
    assert len(_bindings(strata, dm_uuid)) == 1
    strata.app.config["GATEKEEPER_CLIENT"] = None  # Gatekeeper's file cannot be read
    strata.build(dm_uuid)
    dm = strata.datamodule(dm_uuid)
    assert dm.build_status == "failed" and dm.last_build_error is not None
    assert dm.last_build_error.startswith(
        "access could not be checked: Gatekeeper is not configured"
    )


def test_a_datamodule_owned_by_the_admins_uses_an_admins_connection(
    strata: Strata, source_db: str
) -> None:
    """The datamodules made before access control (owners []) keep building."""
    _new_connection(strata, "ext_db", "sqlite", may_use="", path=source_db)
    dm_uuid = _alices(strata, owners="@strata-admins")
    assert _bind(strata, dm_uuid, "ext_db").status_code == 302
    strata.build(dm_uuid)
    assert strata.datamodule(dm_uuid).build_status == "ready"


def test_0010_leaves_existing_connections_to_the_admins(tmp_path: Path, monkeypatch) -> None:
    from strata import db

    path = str(tmp_path / "m.sqlite3")
    monkeypatch.setattr(
        db, "_migration_files", lambda: [f for f in _migration_files() if f[0] <= 9]
    )
    db.init_db_at(path)
    conn = apsw.Connection(path)
    now = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO connection (uuid, name, driver, params_encrypted, created_by, modified_by, "
        "created_at, modified_at) VALUES ('c1', 'navi', 'odbc', 'x', 'jo', 'jo', ?, ?)",
        (now, now),
    )
    conn.close()
    monkeypatch.setattr(db, "_migration_files", _migration_files)
    db.init_db_at(path)
    conn = apsw.Connection(path)
    assert conn.execute("SELECT name, users_json FROM connection").fetchall() == [("navi", None)]
    conn.close()


def test_binding_and_owners_are_refused_when_they_cannot_be_checked(
    strata: Strata, source_db: str
) -> None:
    """No Gatekeeper: who is in @brs-finance (or an admin) cannot be told, so no."""
    _new_connection(strata, "ext_db", "sqlite", may_use="alice", path=source_db)
    dm_uuid = _alices(strata, owners="@brs-finance")
    _bind(strata, dm_uuid, "ext_db")
    assert any(
        msg.startswith("Not bound: access could not be checked: Gatekeeper is not configured")
        for _cat, msg in strata.flashes()
    )
    assert _bindings(strata, dm_uuid) == []
    pinned = strata.new_datamodule(path="test/pinned", script=SCRIPT)
    strata.set_access(pinned, owners="@strata-admins")
    assert _bind(strata, pinned, "ext_db").status_code == 302
    resp = strata.set_access(pinned, owners="@brs-finance")
    assert resp.status_code == 200
    assert "Access: the owners could not be checked: Gatekeeper is not configured" in (
        resp.get_data(as_text=True)
    )
    assert strata.datamodule(pinned).owners == ["@strata-admins"]
