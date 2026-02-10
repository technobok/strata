"""Admin blueprint — settings, SQL console, system info."""

import logging

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
from strata.config import REGISTRY
from strata.db import get_db
from strata.models.app_setting import get_setting, set_setting

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
    """View and edit application configuration."""
    if request.method == "POST":
        for entry in REGISTRY:
            form_key = f"config_{entry.key}"
            value = request.form.get(form_key, "").strip()
            if value:
                set_setting(entry.key, value, entry.description)

        flash("Configuration saved. Restart the app for changes to take effect.", "success")
        return redirect(url_for("admin.config"))

    # Load current values
    config_values: dict[str, str] = {}
    for entry in REGISTRY:
        val = get_setting(entry.key)
        config_values[entry.key] = val if val is not None else str(entry.default)

    return render_template(
        "admin/config.html",
        registry=REGISTRY,
        config_values=config_values,
    )


@bp.route("/sql", methods=["GET", "POST"])
@admin_required
def sql_console() -> str:
    """SQL console for ad-hoc metadata queries."""
    query = ""
    columns: list[str] = []
    rows: list[tuple] = []
    error: str | None = None
    row_count = 0

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
                    if cursor.description:
                        columns = [desc[0] for desc in cursor.description]
                    rows = cursor.fetchall()
                    row_count = len(rows)
                except apsw.SQLError as e:
                    error = str(e)

    return render_template(
        "admin/sql.html",
        query=query,
        columns=columns,
        rows=rows,
        error=error,
        row_count=row_count,
    )
