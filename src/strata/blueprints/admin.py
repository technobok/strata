"""Admin blueprint — settings, SQL console, system info."""

import logging
import os

import apsw
import apsw.ext
from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.wrappers import Response

from strata.blueprints.auth import admin_required
from strata.config import (
    BOOTSTRAP_ONLY,
    ENV_OVERRIDES,
    REGISTRY,
    Applies,
    ConfigEntry,
    ConfigType,
    live_str,
    parse_value,
    resolve_effective,
    resolve_entry,
    serialize_value,
)
from strata.db import get_db, get_db_path
from strata.models.app_setting import clear_setting, set_setting

log = logging.getLogger(__name__)

bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.route("/")
@admin_required
def index() -> str:
    """Admin dashboard."""
    db = get_db()

    # Gather stats
    stats: dict[str, int] = {}
    for table in ("datamodule", "datamodule_version", "report", "schedule", "tag"):
        try:
            row = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            stats[table] = int(row[0]) if row else 0
        except apsw.SQLError:
            stats[table] = 0

    # Active schedules
    try:
        row = db.execute("SELECT COUNT(*) FROM schedule WHERE enabled = 1").fetchone()
        stats["active_schedules"] = int(row[0]) if row else 0
    except apsw.SQLError:
        stats["active_schedules"] = 0

    from strata.models.build_job import BuildJob
    from strata.models.worker_process import WorkerProcess

    return render_template(
        "admin/index.html",
        stats=stats,
        registry=REGISTRY,
        workers=WorkerProcess.alive(),
        job_counts=BuildJob.counts_by_status(),
        public_url_missing=not live_str("server.public_url"),
    )


@bp.route("/config", methods=["GET", "POST"])
@admin_required
def config() -> str | Response:
    """View and edit application configuration.

    Each row resolves to (effective, source) where source is 'env', 'db', or
    'default', and says when a change takes effect (``ConfigEntry.applies``).
    Env-overridden rows render read-only. Bootstrap-only rows (the DB /
    project-root paths needed before the DB opens) are also read-only.
    """
    if request.method == "POST":
        action = request.form.get("action", "save")
        if action == "clear":
            key = request.form.get("clear_key", "")
            entry = resolve_entry(key)
            if entry is not None and key not in BOOTSTRAP_ONLY:
                clear_setting(key)
                flash(f"Cleared '{key}' (now using the default; {entry.applies.value}).", "success")
            return redirect(url_for("admin.config"))

        # action == 'save': iterate all registry entries.
        before = _effective_stored()
        inert_overrides: list[str] = []
        errors: list[str] = []
        for entry in REGISTRY:
            if entry.key in BOOTSTRAP_ONLY:
                continue
            form_key = f"config_{entry.key}"
            present = request.form.get(f"{form_key}_present") == "1"
            if not present:
                continue

            if entry.type is ConfigType.BOOL:
                raw = "true" if request.form.get(form_key) else "false"
            elif entry.secret:
                # Empty submission keeps the existing stored secret (never overwrite
                # with blank just because the operator didn't retype it).
                raw = request.form.get(form_key, "")
                if not raw:
                    continue
            else:
                raw = request.form.get(form_key, "").strip()

            if raw == "":
                clear_setting(entry.key)
                continue

            # Validate INT now so we don't store something that fails parse at boot.
            if entry.type is ConfigType.INT:
                try:
                    parse_value(entry, raw)
                except ValueError:
                    errors.append(f"{entry.key}: '{raw}' is not a valid integer")
                    continue

            set_setting(entry.key, raw, entry.description)
            env_name = ENV_OVERRIDES.get(entry.key)
            if env_name and os.environ.get(env_name):
                inert_overrides.append(f"{entry.key} (overridden by ${env_name})")

        for err in errors:
            flash(err, "error")
        if inert_overrides:
            flash(
                "Saved, but these are currently inert because of env-var overrides "
                "(value will take effect once the env var is unset): " + ", ".join(inert_overrides),
                "info",
            )
        if not errors:
            after = _effective_stored()
            changed = [e for e in REGISTRY if before[e.key] != after[e.key]]
            flash(_saved_message(changed), "success")
        return redirect(url_for("admin.config"))

    rows = []
    for entry in REGISTRY:
        effective, source = resolve_effective(entry, os.environ.get)
        rows.append(
            {
                "entry": entry,
                "effective": effective,
                "source": source,
                "env_name": ENV_OVERRIDES.get(entry.key, ""),
                "bootstrap_only": entry.key in BOOTSTRAP_ONLY,
                "type": entry.type.value,
                "applies": entry.applies,
            }
        )

    return render_template("admin/config.html", rows=rows, live=Applies.LIVE)


def _effective_stored() -> dict[str, str]:
    """Each setting's stored value, or its default when no row is stored."""
    stored = {str(k): str(v) for k, v in get_db().execute("SELECT key, value FROM app_setting")}
    return {e.key: stored.get(e.key, serialize_value(e, e.default)) for e in REGISTRY}


def _saved_message(changed: list[ConfigEntry]) -> str:
    """'Configuration saved. Applies immediately: a. Restart the app: b.'"""
    if not changed:
        return "Configuration saved (no changes)."
    parts = []
    for applies in Applies:
        keys = [e.key for e in changed if e.applies is applies]
        if keys:
            label = applies.value[0].upper() + applies.value[1:]
            parts.append(f"{label}: {', '.join(keys)}.")
    return "Configuration saved. " + " ".join(parts)


def _get_schema() -> list[dict[str, object]]:
    """Return [{name, columns}] for every user table in the metadata DB."""
    db = get_db()
    tables: list[dict[str, object]] = []
    for (name,) in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall():
        cols = [row[1] for row in db.execute(f"PRAGMA table_info({name})").fetchall()]
        tables.append({"name": name, "columns": cols})
    return tables


def _deny_attach(operation: int, *_: object) -> int:
    if operation in (apsw.SQLITE_ATTACH, apsw.SQLITE_DETACH):
        return apsw.SQLITE_DENY
    return apsw.SQLITE_OK


def _run_readonly(query: str) -> tuple[list[str], list[tuple]]:
    """Run one statement on a read-only connection to the metadata DB.

    The connection is opened read-only and refuses ATTACH, so nothing typed in
    the console can change the metadata DB or open another database file. A
    second statement after the first is refused rather than silently run.
    """
    conn = apsw.Connection(get_db_path(), flags=apsw.SQLITE_OPEN_READONLY)
    try:
        conn.authorizer = _deny_attach
        remaining = apsw.ext.query_info(conn, query).query_remaining
        if remaining and remaining.strip():
            raise ValueError("Run one statement at a time.")
        cursor = conn.execute(query)
        # APSW raises ExecutionCompleteError if the cursor has no remaining
        # rows (empty SELECT, side-effect PRAGMA, etc.).
        try:
            columns = [desc[0] for desc in cursor.description]
        except apsw.ExecutionCompleteError:
            columns = []
        return columns, cursor.fetchall()
    finally:
        conn.close()


@bp.route("/sql", methods=["GET", "POST"])
@admin_required
def sql_console() -> str:
    """SQL console for ad-hoc metadata queries."""
    query = ""
    columns: list[str] = []
    rows: list[tuple] = []
    error: str | None = None
    row_count = 0
    executed = False

    if request.method == "POST":
        query = request.form.get("query", "").strip()
        if not query:
            error = "No query provided."
        else:
            # Only allow SELECT statements for safety
            first_word = query.split()[0].upper() if query.split() else ""
            if first_word not in ("SELECT", "PRAGMA", "EXPLAIN"):
                error = "Only SELECT, PRAGMA, and EXPLAIN queries are allowed."
            else:
                try:
                    columns, rows = _run_readonly(query)
                    row_count = len(rows)
                    executed = True
                except (apsw.Error, ValueError) as e:
                    error = str(e)

    return render_template(
        "admin/sql.html",
        query=query,
        columns=columns,
        rows=rows,
        error=error,
        row_count=row_count,
        executed=executed,
        schema=_get_schema(),
    )
