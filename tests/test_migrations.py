"""The migration runner: idempotent, and safe when app and worker migrate at once.

Both containers run ``strata-admin init-db`` on start, so two runners can race
on a fresh or outdated DB. Each must re-check a version under the write lock;
otherwise the second applies it again and fails (duplicate schema_migrations
row, or ``ALTER TABLE ... ADD COLUMN`` of a column that now exists).
"""

import threading

import apsw

from strata import db


def _state(db_path: str) -> tuple[list[tuple[int, int]], str, int]:
    conn = apsw.Connection(db_path, flags=apsw.SQLITE_OPEN_READONLY)
    try:
        versions = conn.execute(
            "SELECT version, COUNT(*) FROM schema_migrations GROUP BY version ORDER BY version"
        ).fetchall()
        (schema_version,) = conn.execute(
            "SELECT value FROM db_metadata WHERE key = 'schema_version'"
        ).fetchone()
        (secret_keys,) = conn.execute(
            "SELECT COUNT(*) FROM app_setting WHERE key = 'secret_key'"
        ).fetchone()
    finally:
        conn.close()
    return versions, schema_version, secret_keys


def test_concurrent_init_applies_each_migration_once(tmp_path, monkeypatch):
    db_path = str(tmp_path / "race.sqlite3")
    real_files = db._migration_files
    # Both runners read their (empty) applied set before either migrates: the
    # barrier sits between that read and the per-version loop.
    barrier = threading.Barrier(2, timeout=10)

    def files_after_both_read() -> list:
        barrier.wait()
        return real_files()

    monkeypatch.setattr(db, "_migration_files", files_after_both_read)

    errors: list[BaseException] = []

    def runner() -> None:
        try:
            db.init_db_at(db_path)
        except BaseException as e:  # noqa: BLE001 -- the test reports it
            errors.append(e)

    threads = [threading.Thread(target=runner) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "a migration runner hung"
    assert errors == []

    expected = [v for v, _ in real_files()]
    versions, schema_version, secret_keys = _state(db_path)
    assert versions == [(v, 1) for v in expected]
    assert schema_version == str(max(expected))
    assert secret_keys == 1


def test_wal_switch_waits_out_a_writer_on_a_fresh_file(tmp_path):
    # On a rollback-journal file, a WAL switch next to a held write lock gets
    # SQLITE_BUSY at once (no busy-handler wait): the first-deploy race where
    # one container is already migrating while the other opens the DB.
    path = str(tmp_path / "fresh.sqlite3")
    holder = apsw.Connection(path)
    holder.execute("CREATE TABLE t (x)")
    holder.execute("BEGIN IMMEDIATE;")
    release = threading.Timer(0.3, lambda: holder.execute("COMMIT;"))
    release.start()
    conn = apsw.Connection(path)
    try:
        db._configure_connection(conn)
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    finally:
        release.join()
        conn.close()
        holder.close()


def test_init_db_is_idempotent(tmp_path):
    db_path = str(tmp_path / "again.sqlite3")
    db.init_db_at(db_path)
    first = _state(db_path)
    db.init_db_at(db_path)
    assert _state(db_path) == first
    assert first[1] == str(db.EXPECTED_SCHEMA_VERSION)


def test_0008_keeps_admin_keys_and_their_access_log(tmp_path, monkeypatch):
    """Migration 0008 adds the Excel-link columns to api_key; existing keys become
    ordinary admin keys and keep their access log."""
    path = str(tmp_path / "m.sqlite3")
    every = db._migration_files
    monkeypatch.setattr(db, "_migration_files", lambda: [f for f in every() if f[0] <= 7])
    db.init_db_at(path)
    conn = apsw.Connection(path)
    conn.execute("PRAGMA foreign_keys = ON")
    now = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO datamodule (uuid, path, build_script, created_by, modified_by, "
        "created_at, modified_at) VALUES ('d1', 't/x', '', 'jo', 'jo', ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO api_key (uuid, token_hash, token_prefix, name, scope_type, "
        "datamodule_id, created_by, created_at, use_count) "
        "VALUES ('k1', 'h1', 'p1', 'excel', 'datamodule', 1, 'jo', ?, 2)",
        (now,),
    )
    conn.execute(
        "INSERT INTO api_key_access_log (api_key_id, accessed_at, status) "
        "VALUES (1, ?, 200), (1, ?, 403)",
        (now, now),
    )
    conn.close()

    monkeypatch.setattr(db, "_migration_files", every)
    db.init_db_at(path)
    conn = apsw.Connection(path)
    assert conn.execute(
        "SELECT id, uuid, scope_type, use_count, excel_link, token_encrypted FROM api_key"
    ).fetchall() == [(1, "k1", "datamodule", 2, 0, None)]
    assert conn.execute(
        "SELECT api_key_id, status FROM api_key_access_log ORDER BY id"
    ).fetchall() == [(1, 200), (1, 403)]
    conn.close()
