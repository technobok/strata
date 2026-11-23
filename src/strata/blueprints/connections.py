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
from strata.models.datamodule import Datamodule
from strata.services import access
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


def _parse_users() -> tuple[list[str], str | None]:
    """The "May use" list (empty = the admins only), or why it is refused."""
    users = access.principal_lines(request.form.get("may_use", ""))
    problem = access.check_principals(users)
    if problem:
        return [], f"May use: {problem}"
    try:
        unknown = access.Directory().unknown(users)
    except access.DirectoryUnavailable as exc:
        return [], f"May use: the names could not be checked: {exc}."
    if unknown:
        return [], f"May use: Gatekeeper has no user or group {', '.join(unknown)}."
    return users, None


def _warn_bound(connection: Connection) -> None:
    """Say which datamodules bound to ``connection`` have owners not on its list: their
    next build fails (the list is the admins' to set; this is not a refusal)."""
    from strata.db import get_db

    rows = get_db().execute(
        "SELECT DISTINCT datamodule_id FROM datamodule_connection WHERE connection_id = ?",
        (connection.id,),
    )
    affected: list[str] = []
    for (dm_id,) in rows.fetchall():
        assert isinstance(dm_id, int)
        dm = Datamodule.get_by_id(dm_id)
        if dm is None:
            continue
        try:
            missing = access.Directory().missing(access.owners_of(dm), connection.users or [])
        except access.DirectoryUnavailable:
            missing = ["(could not be checked)"]
        if missing:
            affected.append(f"{dm.path} ({', '.join(access.describe(p) for p in missing)})")
    if affected:
        flash(
            "These datamodules have owners who may not use this connection, so their next "
            f"build fails: {'; '.join(affected)}.",
            "error",
        )


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
        elif (users_error := _parse_users()[1]) is not None:
            flash(users_error, "error")
        else:
            params = _form_params(driver)
            Connection.create(
                name=name,
                driver=driver,
                params=params,
                created_by=g.user.username,
                description=description,
                users=_parse_users()[0],
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
        elif (users_error := _parse_users()[1]) is not None:
            flash(users_error, "error")
        else:
            params = _form_params(driver, existing=connection.params)
            connection.update(
                modified_by=g.user.username,
                name=name,
                driver=driver,
                params=params,
                description=description,
                users=_parse_users()[0],
            )
            flash(f"Connection '{name}' updated.", "success")
            _warn_bound(connection)
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
