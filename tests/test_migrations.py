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
