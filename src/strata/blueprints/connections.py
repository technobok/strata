"""Connections admin blueprint — define and manage external DB connections."""

from flask import (
    Blueprint,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.exceptions import NotFound
from werkzeug.wrappers import Response

from strata.blueprints.auth import admin_required
from strata.models.connection import Connection
from strata.services.connection_service import DRIVERS, test_connection
from strata.services.template_service import ALIAS_RE

bp = Blueprint("connections", __name__, url_prefix="/admin/connections")


def _form_params(driver: str, existing: dict | None = None) -> dict:
    """Pull driver-specific fields from request.form. Empty password keeps existing."""
    spec = DRIVERS[driver]
    params: dict = {}
    for field in spec.param_schema:
        raw = request.form.get(f"param_{field.name}", "").strip()
        if not raw:
            if field.secret and existing is not None and field.name in existing:
                params[field.name] = existing[field.name]
                continue
            if field.default is not None:
                raw = field.default
        params[field.name] = raw
    return params


@bp.route("/")
@admin_required
def index() -> str:
    """List all connections."""
    return render_template(
        "admin/connections/index.html",
        connections=Connection.get_all(),
        drivers=DRIVERS,
    )


@bp.route("/new", methods=["GET", "POST"])
@admin_required
def new() -> str | Response:
    """Create a new connection."""
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        driver = request.form.get("driver", "").strip()
        description = request.form.get("description", "").strip()

        if not name:
            flash("Connection name is required.", "error")
        elif not ALIAS_RE.match(name):
            flash(
                f"Connection name '{name}' must start with a letter or underscore "
                "and contain only letters, digits, and underscores (no hyphens, "
                "spaces, or punctuation) so it can be used directly as a SQL alias.",
                "error",
            )
        elif driver not in DRIVERS:
            flash(f"Unknown driver: {driver}", "error")
        elif Connection.get_by_name(name) is not None:
            flash(f"Connection '{name}' already exists.", "error")
        else:
            params = _form_params(driver)
            Connection.create(
                name=name,
                driver=driver,
                params=params,
                created_by=g.user.username,
                description=description,
            )
            flash(f"Connection '{name}' created.", "success")
            return redirect(url_for("connections.index"))

    return render_template(
        "admin/connections/edit.html",
        connection=None,
        drivers=DRIVERS,
        # request.values combines args+form so the driver-change GET resubmit
        # (which puts every field in the URL query string) repopulates correctly.
        selected_driver=request.values.get("driver") or "sqlite",
        form_values=request.values,
    )


@bp.route("/<uuid>/edit", methods=["GET", "POST"])
@admin_required
def edit(uuid: str) -> str | Response:
    """Edit an existing connection."""
    connection = Connection.get_by_uuid(uuid)
    if connection is None:
        raise NotFound()

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        driver = request.form.get("driver", connection.driver).strip()
        description = request.form.get("description", "").strip()

        if not name:
            flash("Connection name is required.", "error")
        elif not ALIAS_RE.match(name):
            flash(
                f"Connection name '{name}' must start with a letter or underscore "
                "and contain only letters, digits, and underscores (no hyphens, "
                "spaces, or punctuation) so it can be used directly as a SQL alias.",
                "error",
            )
        elif driver not in DRIVERS:
            flash(f"Unknown driver: {driver}", "error")
        else:
            params = _form_params(driver, existing=connection.params)
            connection.update(
                modified_by=g.user.username,
                name=name,
                driver=driver,
                params=params,
                description=description,
            )
            flash(f"Connection '{name}' updated.", "success")
            return redirect(url_for("connections.index"))

    return render_template(
        "admin/connections/edit.html",
        connection=connection,
        drivers=DRIVERS,
        selected_driver=request.values.get("driver") or connection.driver,
        form_values=request.values,
    )


@bp.route("/<uuid>/test", methods=["POST"])
@admin_required
def test(uuid: str) -> Response:
    """Probe an existing connection."""
    connection = Connection.get_by_uuid(uuid)
    if connection is None:
        raise NotFound()
    ok, message = test_connection(connection.driver, connection.params)
    flash(
        f"Connection '{connection.name}': {message}",
        "success" if ok else "error",
    )
    return redirect(url_for("connections.index"))


@bp.route("/<uuid>/delete", methods=["POST"])
@admin_required
def delete(uuid: str) -> Response:
    """Delete a connection (linked reports get connection_id set to NULL)."""
    connection = Connection.get_by_uuid(uuid)
    if connection is None:
        raise NotFound()
    name = connection.name
    connection.delete()
    flash(f"Connection '{name}' deleted.", "success")
    return redirect(url_for("connections.index"))
