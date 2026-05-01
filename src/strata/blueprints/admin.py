"""Admin blueprint — settings, SQL console, system info."""

import logging
import os

import apsw
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
    ConfigType,
    parse_value,
    resolve_effective,
)
from strata.db import get_db
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
    for table in ("report", "report_run", "schedule", "api_link", "tag"):
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

    return render_template("admin/index.html", stats=stats, registry=REGISTRY)


@bp.route("/config", methods=["GET", "POST"])
@admin_required
def config() -> str | Response:
    """View and edit application configuration.

    Each row resolves to (effective, source) where source is 'env', 'db', or
    'default'. Env-overridden rows render read-only. Bootstrap-only rows (the
    DB / project-root paths needed before the DB opens) are also read-only.
    """
    if request.method == "POST":
        action = request.form.get("action", "save")
        if action == "clear":
            key = request.form.get("clear_key", "")
            if any(e.key == key for e in REGISTRY) and key not in BOOTSTRAP_ONLY:
                clear_setting(key)
                flash(f"Cleared '{key}' (now using default).", "success")
            return redirect(url_for("admin.config"))

        # action == 'save': iterate all registry entries.
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
            flash("Configuration saved. Most changes need an app restart.", "success")
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
            }
        )

    return render_template("admin/config.html", rows=rows)


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
                db = get_db()
                try:
                    cursor = db.execute(query)
                    # APSW raises ExecutionCompleteError if the cursor has no
                    # remaining rows (empty SELECT, side-effect PRAGMA, etc.).
                    try:
                        columns = [desc[0] for desc in cursor.description]
                    except apsw.ExecutionCompleteError:
                        columns = []
                    rows = cursor.fetchall()
                    row_count = len(rows)
                    executed = True
                except apsw.SQLError as e:
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
