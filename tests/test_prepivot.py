"""A database from the Strata before datamodules: init-db refuses it and leaves it
untouched; ``strata-admin adopt-prepivot`` keeps it as a backup and starts a new
database that keeps its settings, its secret key and its connections.
"""

import json
from pathlib import Path
from typing import Any

import apsw
import pytest
from click.testing import CliRunner

from strata.cli import main
from strata.crypto import fernet_for
from strata.db import EXPECTED_SCHEMA_VERSION, UnusableDatabase, init_db_at, schema_tables
from strata.prepivot import AdoptError, adopt

SCHEMA = Path(__file__).parent / "fixtures" / "prepivot_schema.sql"
OLD_SECRET = "old-secret-key-0123456789"
PARAMS = {"dsn": "coredb", "password": "s3cret"}
NOW = "2026-01-01T00:00:00+00:00"


def _prepivot_db(path: Path) -> Path:
    conn = apsw.Connection(str(path))
    for _ in conn.execute(SCHEMA.read_text()):
        pass
    conn.execute("PRAGMA journal_mode = WAL;")  # as the refused init-db leaves it
    token = fernet_for(OLD_SECRET).encrypt(json.dumps(PARAMS).encode()).decode()
    with conn:
        for key, value in (
            ("secret_key", OLD_SECRET),
            ("auth.admin_group", "it-admins"),
            ("cache.root", "/old/cache"),
        ):
            conn.execute("INSERT INTO app_setting VALUES (?, ?, '')", (key, value))
        for row in ((1, "c-1", "coredb", token), (2, "c-2", "broken", "not-a-fernet-token")):
            conn.execute(
                "INSERT INTO connection VALUES (?, ?, ?, 'odbc', ?, '', 'jo', 'jo', ?, ?)",
                (*row, NOW, NOW),
            )
        conn.execute(
            "INSERT INTO report (id, uuid, name, sql_template, created_by, modified_by, "
            "created_at, modified_at) VALUES (1, 'r-1', 'Negative stock', 'SELECT 1', 'jo', "
            "'jo', ?, ?)",
            (NOW, NOW),
        )
        conn.execute(
            "INSERT INTO api_link (id, uuid, report_id, name, created_by, created_at) "
            "VALUES (1, 'k-1', 1, 'finance workbook', 'jo', ?)",
            (NOW,),
        )
    conn.close()
    return path


def _dump(path: Path) -> dict[str, Any]:
    conn = apsw.Connection(str(path), flags=apsw.SQLITE_OPEN_READONLY)
    try:
        return {
            "tables": schema_tables(conn.cursor()),
            "report": conn.execute("SELECT * FROM report").fetchall(),
            "connection": conn.execute("SELECT * FROM connection").fetchall(),
            "app_setting": conn.execute("SELECT * FROM app_setting ORDER BY key").fetchall(),
        }
    finally:
        conn.close()


def _setting(conn: apsw.Connection, key: str) -> Any:
    row = conn.execute("SELECT value FROM app_setting WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


@pytest.fixture
def old_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = _prepivot_db(tmp_path / "strata.sqlite3")
    monkeypatch.setenv("STRATA_DB", str(db))
    return db


def test_init_db_refuses_a_prepivot_database_and_changes_nothing(old_db: Path) -> None:
    before = _dump(old_db)
    with pytest.raises(UnusableDatabase, match="adopt-prepivot"):
        init_db_at(str(old_db))
    assert _dump(old_db) == before


def test_the_init_db_command_says_why_without_a_traceback(old_db: Path) -> None:
    result = CliRunner().invoke(main, ["init-db"])
    assert result.exit_code == 1
    assert "before datamodules" in result.output and "adopt-prepivot" in result.output
    assert "Traceback" not in result.output and "no such column" not in result.output


def test_init_db_refuses_a_database_strata_did_not_make(tmp_path: Path) -> None:
    db = tmp_path / "other.sqlite3"
    conn = apsw.Connection(str(db))
    conn.execute("CREATE TABLE invoices (id INTEGER PRIMARY KEY)")
    conn.close()
    with pytest.raises(UnusableDatabase, match=r"holds tables \(invoices\).*did not make it"):
        init_db_at(str(db))


def test_adopt_keeps_settings_and_connections_and_the_old_file(
    old_db: Path, tmp_path: Path
) -> None:
    result = CliRunner().invoke(main, ["adopt-prepivot"])
    assert result.exit_code == 0, result.output

    [backup] = tmp_path.glob("strata.prepivot-*.sqlite3")
    assert _dump(backup)["report"][0][2] == "Negative stock"  # the old file, whole
    assert not Path(f"{backup}-wal").exists()
    check = apsw.Connection(str(backup), flags=apsw.SQLITE_OPEN_READONLY)
    assert check.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    check.close()

    new = apsw.Connection(str(old_db))
    versions = [v for (v,) in new.execute("SELECT version FROM schema_migrations ORDER BY 1")]
    assert versions == list(range(1, EXPECTED_SCHEMA_VERSION + 1))
    assert _setting(new, "secret_key") == OLD_SECRET
    assert _setting(new, "auth.admin_group") == "it-admins"
    assert _setting(new, "cache.root") is None
    names = [n for (n,) in new.execute("SELECT name FROM connection ORDER BY name")]
    assert names == ["broken", "coredb"]
    (token,) = new.execute(
        "SELECT params_encrypted FROM connection WHERE name = 'coredb'"
    ).fetchone()
    assert json.loads(fernet_for(_setting(new, "secret_key")).decrypt(token.encode())) == PARAMS
    assert new.execute("SELECT count(*) FROM report").fetchone()[0] == 0
    new.close()

    out = result.output
    assert "Settings kept (2): auth.admin_group, secret_key" in out
    assert "Settings dropped, unknown to this Strata (1): cache.root" in out
    assert "Connections kept (2): broken, coredb" in out
    assert "WARNING" in out and ": broken." in out
    assert "1 reports" in out and "1 API links" in out and "Negative stock" in out
    init_db_at(str(old_db))  # the new database is an ordinary one now


def test_adopt_dry_run_changes_nothing(old_db: Path, tmp_path: Path) -> None:
    before = (_dump(old_db), sorted(p.name for p in tmp_path.iterdir()))
    result = CliRunner().invoke(main, ["adopt-prepivot", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Dry run: nothing was changed." in result.output
    assert "Backup would be:" in result.output and "Connections kept (2)" in result.output
    assert (_dump(old_db), sorted(p.name for p in tmp_path.iterdir())) == before


def test_adopt_refuses_a_current_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "strata.sqlite3"
    init_db_at(str(db))
    monkeypatch.setenv("STRATA_DB", str(db))
    result = CliRunner().invoke(main, ["adopt-prepivot"])
    assert result.exit_code == 1 and "already a database of this Strata" in result.output
    assert list(tmp_path.glob("*.prepivot-*")) == []


def test_adopt_refuses_while_another_program_has_it_open(old_db: Path, tmp_path: Path) -> None:
    before = _dump(old_db)
    holder = apsw.Connection(str(old_db))  # e.g. a strata-web still running
    holder.execute("SELECT count(*) FROM report").fetchone()
    try:
        with pytest.raises(AdoptError, match="in use by another program"):
            adopt(str(old_db))
    finally:
        holder.close()
    assert list(tmp_path.glob("*.prepivot-*")) == []
    assert _dump(old_db) == before
