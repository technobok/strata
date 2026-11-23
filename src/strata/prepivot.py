"""Replace a database from the Strata before datamodules (``strata-admin adopt-prepivot``).

That Strata's schema (report-centric: report, report_parameter, api_link, ...)
cannot be migrated: the datamodule pivot reshaped reports with no conversion,
and ``init-db`` refuses such a file. Its settings and connections have the same
shape in both, so adopting keeps them:

1. read everything needed from the old file (nothing changes if this fails);
2. make the old file self-contained (checkpoint, ``journal_mode=DELETE``) and
   rename it beside itself as ``<name>.prepivot-<UTC stamp>.sqlite3``: the backup;
3. create a new database at the old path (``init_db_at``: the whole schema);
4. copy across the settings this Strata knows, the ``secret_key`` (connection
   passwords are encrypted with it) and the connections.

The old reports, schedules, runs and API links stay in the backup only.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import apsw
from cryptography.fernet import InvalidToken

from strata.config import BOOTSTRAP_ONLY, resolve_entry
from strata.db import PREPIVOT_TABLES, init_db_at, schema_tables

CONNECTION_COLUMNS = (
    "id",
    "uuid",
    "name",
    "driver",
    "params_encrypted",
    "description",
    "created_by",
    "modified_by",
    "created_at",
    "modified_at",
)
# The old content that has no place in this Strata, counted for the summary.
LEFT_BEHIND = {
    "report": "reports",
    "report_parameter": "report parameters",
    "schedule": "schedules",
    "report_run": "report runs",
    "api_link": "API links",
    "report_access": "access-log rows",
    "tag": "tags",
}


class AdoptError(RuntimeError):
    pass


@dataclass
class Adoption:
    db_path: str
    backup_path: str
    dry_run: bool
    kept_settings: list[str]
    dropped_settings: list[str]
    connections: list[str]
    undecryptable: list[str]  # connections whose params the old secret_key cannot open
    old_reports: list[str]
    left_behind: dict[str, int]  # label -> rows, for the old tables not carried

    def summary(self) -> list[str]:
        lines = ["Dry run: nothing was changed."] if self.dry_run else []
        verb = "would be" if self.dry_run else "is"
        lines += [
            f"Old database:  {self.db_path}",
            f"Backup {verb}:  {self.backup_path}",
            f"Settings kept ({len(self.kept_settings)}): {', '.join(self.kept_settings) or '-'}",
        ]
        if self.dropped_settings:
            lines.append(
                f"Settings dropped, unknown to this Strata ({len(self.dropped_settings)}): "
                + ", ".join(self.dropped_settings)
            )
        lines.append(
            f"Connections kept ({len(self.connections)}): {', '.join(self.connections) or '-'}"
        )
        if self.undecryptable:
            lines.append(
                "WARNING: the old secret key cannot open the saved parameters of: "
                + ", ".join(self.undecryptable)
                + ". Re-enter their passwords under Connections."
            )
        left = ", ".join(f"{n} {label}" for label, n in self.left_behind.items() if n)
        lines.append(f"Left in the backup only: {left or 'nothing'}")
        if self.old_reports:
            lines.append(
                "Old reports (re-create them as datamodules + reports): "
                + ", ".join(self.old_reports)
            )
        if not self.dry_run:
            lines.append(f"New database ready at {self.db_path}. Start Strata again.")
        return lines


def adopt(db_path: str, *, dry_run: bool = False, now: datetime | None = None) -> Adoption:
    path = Path(db_path)
    if not path.is_file():
        raise AdoptError(f"{db_path} does not exist: there is nothing to adopt")
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.stem}.prepivot-{stamp}{path.suffix}")

    old = apsw.Connection(str(path))
    try:
        old.execute("PRAGMA busy_timeout = 5000;")
        _require_prepivot(old, db_path)
        result = _read(old, db_path, str(backup), dry_run)
        if dry_run:
            return result
        _make_self_contained(old, db_path)
    finally:
        old.close()
    if Path(f"{path}-wal").exists():
        raise AdoptError(
            f"{db_path}-wal is still there: another program has the database open. "
            "Stop strata-web and strata-worker, then run this again."
        )
    if backup.exists():
        raise AdoptError(f"{backup} already exists; wait a second and run this again")

    path.rename(backup)
    try:
        init_db_at(str(path))
        _write(str(path), str(backup))
    except Exception as exc:
        raise AdoptError(
            f"adopting failed after the old database was moved to {backup}: {exc}. "
            f"To undo, delete {path} (and any {path.name}-wal / {path.name}-shm), then "
            f"rename {backup.name} back to {path.name}."
        ) from exc
    return result


def _require_prepivot(conn: apsw.Connection, db_path: str) -> None:
    try:
        recorded = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()
    except apsw.SQLError:  # no ledger at all
        recorded = (0,)
    if recorded and recorded[0]:
        raise AdoptError(
            f"{db_path} is already a database of this Strata (its migrations are "
            "recorded); there is nothing to adopt. `strata-admin init-db` updates it."
        )
    if not PREPIVOT_TABLES & set(schema_tables(conn.cursor())):
        raise AdoptError(
            f"{db_path} is not a database from the Strata before datamodules (it has none "
            "of the old report tables); there is nothing to adopt."
        )


def _read(conn: apsw.Connection, db_path: str, backup: str, dry_run: bool) -> Adoption:
    from strata.crypto import fernet_for

    settings = conn.execute("SELECT key, value FROM app_setting ORDER BY key").fetchall()
    kept = [str(k) for k, _v in settings if _carried(str(k))]
    dropped = [str(k) for k, _v in settings if not _carried(str(k))]
    secret = next((str(v) for k, v in settings if k == "secret_key" and v), None)

    cols = ", ".join(CONNECTION_COLUMNS)
    connections = conn.execute(f"SELECT {cols} FROM connection ORDER BY name").fetchall()
    undecryptable: list[str] = []
    for row in connections:
        name, token = str(row[2]), str(row[4])
        try:
            if secret is None:
                raise InvalidToken
            fernet_for(secret).decrypt(token.encode())
        except InvalidToken:
            undecryptable.append(name)

    tables = set(schema_tables(conn.cursor()))
    left_behind = {
        label: _count(conn, table) for table, label in LEFT_BEHIND.items() if table in tables
    }
    reports = (
        [str(n) for (n,) in conn.execute("SELECT name FROM report ORDER BY name").fetchall()]
        if "report" in tables
        else []
    )
    return Adoption(
        db_path=db_path,
        backup_path=backup,
        dry_run=dry_run,
        kept_settings=kept,
        dropped_settings=dropped,
        connections=[str(r[2]) for r in connections],
        undecryptable=undecryptable,
        old_reports=reports,
        left_behind=left_behind,
    )


def _count(conn: apsw.Connection, table: str) -> int:
    row = conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()
    return int(row[0]) if row else 0


def _carried(key: str) -> bool:
    """secret_key (it opens the connections' params) and the settings this Strata
    knows; bootstrap-only ones are environment-only and never stored."""
    if key == "secret_key":
        return True
    return resolve_entry(key) is not None and key not in BOOTSTRAP_ONLY


def _make_self_contained(conn: apsw.Connection, db_path: str) -> None:
    """Fold any WAL back into the file and leave WAL mode, so the renamed file is
    the whole database and no -wal file is left to meet the new one."""
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        mode = conn.execute("PRAGMA journal_mode = DELETE;").fetchone()
    except apsw.BusyError as exc:
        raise AdoptError(
            f"{db_path} is in use by another program. Stop strata-web and strata-worker, "
            "then run this again."
        ) from exc
    if not mode or str(mode[0]).lower() != "delete":
        raise AdoptError(
            f"{db_path} is in use by another program (it stays in WAL mode). Stop "
            "strata-web and strata-worker, then run this again."
        )


def _write(db_path: str, backup: str) -> None:
    old = apsw.Connection(backup, flags=apsw.SQLITE_OPEN_READONLY)
    try:
        settings = old.execute("SELECT key, value, description FROM app_setting").fetchall()
        cols = ", ".join(CONNECTION_COLUMNS)
        connections = old.execute(f"SELECT {cols} FROM connection").fetchall()
    finally:
        old.close()
    new = apsw.Connection(db_path)
    try:
        new.execute("PRAGMA busy_timeout = 5000;")
        marks = ", ".join("?" for _ in CONNECTION_COLUMNS)
        with new:  # one transaction
            for key, value, description in settings:
                if _carried(str(key)):
                    new.execute(
                        "INSERT OR REPLACE INTO app_setting (key, value, description) "
                        "VALUES (?, ?, ?)",
                        (key, value, description),
                    )
            for row in connections:
                new.execute(f"INSERT INTO connection ({cols}) VALUES ({marks})", row)
    finally:
        new.close()
