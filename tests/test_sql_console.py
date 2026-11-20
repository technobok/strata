"""The admin SQL console runs one read-only statement at a time."""

from tests.conftest import Strata


def _run(strata: Strata, query: str) -> str:
    resp = strata.client.post("/admin/sql", data={"query": query})
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def _api_key_count(strata: Strata) -> int:
    with strata.app.app_context():
        from strata.db import get_db

        row = get_db().execute("SELECT count(*) FROM api_key").fetchone()
        return int(row[0]) if row else 0


def test_select_runs(strata: Strata) -> None:
    body = _run(strata, "SELECT 40 + 2 AS answer")
    assert "answer" in body and "42" in body


def test_a_second_statement_is_refused_not_run(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    strata.new_api_key(dm_uuid)
    body = _run(strata, "SELECT 1; DELETE FROM api_key")
    assert "one statement at a time" in body
    assert _api_key_count(strata) == 1


def test_writes_are_refused_by_the_connection(strata: Strata) -> None:
    # The first-word check lets PRAGMA through; the read-only connection must
    # still refuse a PRAGMA that writes.
    _run(strata, "PRAGMA user_version = 7")
    with strata.app.app_context():
        from strata.db import get_db

        row = get_db().execute("PRAGMA user_version").fetchone()
    assert row is not None and row[0] == 0


def test_attach_is_refused(strata: Strata) -> None:
    """The first-word check already refuses ATTACH; the connection refuses it too."""
    import apsw
    import pytest

    from strata.blueprints.admin import _run_readonly

    db_path = strata.app.config["DATABASE_PATH"]
    with strata.app.app_context(), pytest.raises(apsw.AuthError):
        _run_readonly(f"ATTACH '{db_path}' AS x")


def test_non_admin_cannot_use_the_console(strata: Strata) -> None:
    resp = strata.client.post(
        "/admin/sql", data={"query": "SELECT 1"}, headers={"X-User": "viewer"}
    )
    assert resp.status_code == 302
