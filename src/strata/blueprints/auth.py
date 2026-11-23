"""Authentication blueprint using Gatekeeper SSO."""

import functools
from collections.abc import Callable
from typing import Any, NoReturn

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.exceptions import HTTPException, NotFound
from werkzeug.wrappers import Response

bp = Blueprint("auth", __name__, url_prefix="/auth")


def whoami_url() -> str:
    """Gatekeeper's account page, where people see who they are signed in as (and
    sign out). It sits beside gatekeeper's login page, so it is derived from the
    login URL gatekeeper reports, falling back to where Caddy serves it. The
    nav pattern shared with the sibling apps links the username here."""
    gk = current_app.config.get("GATEKEEPER_CLIENT")
    login_url = gk.get_login_url() if gk else None
    if login_url and login_url.endswith("/auth/login"):
        return login_url.removesuffix("/login") + "/whoami"
    return "/gatekeeper/auth/whoami"


@bp.app_context_processor
def _inject_whoami_url() -> dict[str, Any]:
    # the function, not its value: only the nav of a signed-in page calls it
    return {"whoami_url": whoami_url}


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


def forbid(message: str) -> NoReturn:
    """Answer 403 with ``message`` (rendered by the app's 403 handler)."""
    abort(403, description=message)


def owner_required(view: Callable[..., Any]) -> Callable[..., Any]:
    """The owners of the datamodule the URL names (by ``uuid``, or a report's,
    schedule's or key's uuid) and the admins; anyone else signed in gets 403."""

    @functools.wraps(view)
    def wrapped_view(*args: Any, **kwargs: Any) -> Any:
        from strata.services import access

        if g.get("user") is None:
            return redirect(url_for("auth.login", next=request.url))
        dm = access.datamodule_for(kwargs)
        if dm is None:
            raise NotFound()
        if not access.can_own(dm):
            forbid("Only this datamodule's owners and the Strata admins can do that.")
        return view(*args, **kwargs)

    return wrapped_view


def writer_required(view: Callable[..., Any]) -> Callable[..., Any]:
    """Writers (``auth.writer_group``) and admins; anyone else signed in gets 403."""

    @functools.wraps(view)
    def wrapped_view(*args: Any, **kwargs: Any) -> Any:
        from strata.services import access

        if g.get("user") is None:
            return redirect(url_for("auth.login", next=request.url))
        if not access.is_writer():
            forbid("Only writers and the Strata admins can create datamodules.")
        return view(*args, **kwargs)

    return wrapped_view


@bp.app_errorhandler(403)
def _forbidden(exc: HTTPException) -> tuple[str, int]:
    return render_template("forbidden.html", message=exc.description), 403


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

    next_url = request.args.get("next", url_for("index"))
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
