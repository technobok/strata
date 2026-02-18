"""Authentication blueprint using Gatekeeper SSO."""

import functools
from collections.abc import Callable
from typing import Any

from flask import (
    Blueprint,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.wrappers import Response

bp = Blueprint("auth", __name__, url_prefix="/auth")


def login_required(view: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator that redirects anonymous users to the login page."""

    @functools.wraps(view)
    def wrapped_view(*args: Any, **kwargs: Any) -> Any:
        if g.get("user") is None:
            return redirect(url_for("auth.login", next=request.url))
        return view(*args, **kwargs)

    return wrapped_view


def admin_required(view: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator that requires admin role."""

    @functools.wraps(view)
    def wrapped_view(*args: Any, **kwargs: Any) -> Any:
        if g.get("user") is None:
            return redirect(url_for("auth.login", next=request.url))
        if not g.get("strata_is_admin"):
            flash("Admin access required.", "error")
            return redirect(url_for("index"))
        return view(*args, **kwargs)

    return wrapped_view


@bp.route("/login")
def login() -> str | Response:
    """Redirect to Gatekeeper SSO login, or show fallback page."""
    if g.get("user"):
        return redirect(url_for("index"))

    gk = current_app.config.get("GATEKEEPER_CLIENT")
    if not gk:
        return render_template("auth/login.html", login_url=None)

    login_url = gk.get_login_url()
    if not login_url:
        return render_template("auth/login.html", login_url=None)

    next_url = request.args.get("next", "/")
    callback_url = url_for("auth.verify", _external=True)

    return redirect(f"{login_url}?app_name=Strata&callback_url={callback_url}&next={next_url}")


@bp.route("/verify")
def verify() -> str | Response:
    """Verify magic link token from Gatekeeper and establish session."""
    gk = current_app.config.get("GATEKEEPER_CLIENT")
    if not gk:
        flash("Authentication is not configured.", "error")
        return redirect(url_for("index"))

    token = request.args.get("token", "")
    result = gk.verify_magic_link(token)

    if not result:
        flash("Invalid or expired login link. Please request a new one.", "error")
        return redirect(url_for("auth.login"))

    user, redirect_url = result

    response = redirect(redirect_url or url_for("index"))
    flash(f"Welcome, {user.fullname or user.username}!", "success")
    gk.set_session_cookie(response, user)

    return response


@bp.route("/logout")
def logout() -> Response:
    """Log out the current user."""
    response = redirect(url_for("index"))
    response.delete_cookie("gk_session")
    flash("You have been logged out.", "info")
    return response
