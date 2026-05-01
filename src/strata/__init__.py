"""Strata - A reporting system with DuckDB query engine."""

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import apsw
from flask import Flask, render_template, request

from strata.config import KEY_MAP, REGISTRY, parse_value


def get_user_timezone() -> ZoneInfo:
    """Get user's timezone from request header or cookie."""
    tz_name = request.headers.get("X-Timezone") or request.cookies.get("tz") or "UTC"
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("UTC")


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    """Application factory for Strata."""
    # Resolve database path
    db_path = os.environ.get("STRATA_DB")
    if not db_path:
        if "STRATA_ROOT" in os.environ:
            project_root = Path(os.environ["STRATA_ROOT"])
        else:
            source_root = Path(__file__).parent.parent.parent
            if (source_root / "src" / "strata" / "__init__.py").exists():
                project_root = source_root
            else:
                project_root = Path.cwd()
        db_path = str(project_root / "instance" / "strata.sqlite3")
        instance_path = project_root / "instance"
    else:
        instance_path = Path(db_path).parent

    instance_path.mkdir(parents=True, exist_ok=True)

    app = Flask(
        __name__,
        instance_path=str(instance_path),
        instance_relative_config=True,
    )

    # Minimal defaults before DB config is loaded
    app.config.from_mapping(
        SECRET_KEY="dev",
        DATABASE_PATH=db_path,
    )

    if test_config is not None:
        app.config.from_mapping(test_config)
    else:
        _load_config_from_db(app)
        _check_schema_version(db_path)

    # Ensure cache directory exists. Priority: env > DB-loaded config > default.
    cache_dir = (
        os.environ.get("CACHE_DIRECTORY")
        or app.config.get("CACHE_DIRECTORY")
        or str(instance_path / "cache")
    )
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    app.config["CACHE_DIRECTORY"] = cache_dir

    from strata.db import close_db

    app.teardown_appcontext(close_db)

    # Gatekeeper client integration
    _init_gatekeeper(app)

    # Jinja filters for date formatting
    @app.template_filter("localdate")
    def localdate_filter(iso_string: str | None) -> str:
        """Format ISO date string in user's timezone (date only)."""
        if not iso_string:
            return ""
        try:
            dt = datetime.fromisoformat(iso_string.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            user_tz = get_user_timezone()
            local_dt = dt.astimezone(user_tz)
            return local_dt.strftime("%b %d, %Y")
        except Exception:
            return iso_string[:10] if iso_string else ""

    @app.template_filter("localdatetime")
    def localdatetime_filter(iso_string: str | None) -> str:
        """Format ISO datetime string in user's timezone (with time and tz)."""
        if not iso_string:
            return ""
        try:
            dt = datetime.fromisoformat(iso_string.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            user_tz = get_user_timezone()
            local_dt = dt.astimezone(user_tz)
            tz_abbr = local_dt.strftime("%Z")
            return local_dt.strftime(f"%b %d, %Y %H:%M {tz_abbr}")
        except Exception:
            return iso_string[:16].replace("T", " ") if iso_string else ""

    @app.template_filter("duration")
    def duration_filter(ms: int | None) -> str:
        """Format a duration in milliseconds to a human-readable string."""
        if ms is None:
            return ""
        if ms < 1000:
            return f"{ms}ms"
        seconds = ms / 1000
        if seconds < 60:
            return f"{seconds:.1f}s"
        minutes = int(seconds // 60)
        remaining = seconds % 60
        return f"{minutes}m {remaining:.0f}s"

    # Register blueprints
    from strata.blueprints import admin, api, auth, connections, reports, schedules, tags

    app.register_blueprint(auth.bp)
    app.register_blueprint(reports.bp)
    app.register_blueprint(tags.bp)
    app.register_blueprint(schedules.bp)
    app.register_blueprint(api.bp)
    app.register_blueprint(admin.bp)
    app.register_blueprint(connections.bp)

    @app.route("/")
    def index() -> str:
        return render_template("index.html")

    return app


def _init_gatekeeper(app: Flask) -> None:
    """Initialize Gatekeeper client for SSO authentication."""
    import logging

    from flask import g

    logger = logging.getLogger(__name__)

    gk_db_path = os.environ.get("GATEKEEPER_DB") or app.config.get("GATEKEEPER_DB_PATH", "")
    gk_url = app.config.get("GATEKEEPER_URL", "")
    gk_api_key = app.config.get("GATEKEEPER_API_KEY", "")
    admin_group = os.environ.get("STRATA_ADMIN_GROUP", "strata-admins")
    app.config["STRATA_ADMIN_GROUP"] = admin_group

    try:
        if gk_db_path:
            from gatekeeper import GatekeeperClient

            gk = GatekeeperClient(db_path=gk_db_path)
            gk.init_app(app, cookie_name="gk_session")
            app.config["GATEKEEPER_CLIENT"] = gk
            logger.info("Gatekeeper client initialized (local DB)")
        elif gk_url and gk_api_key:
            from gatekeeper import GatekeeperClient

            gk = GatekeeperClient(server_url=gk_url, api_key=gk_api_key)
            gk.init_app(app, cookie_name="gk_session")
            app.config["GATEKEEPER_CLIENT"] = gk
            logger.info("Gatekeeper client initialized (HTTP)")
        else:
            logger.info("Gatekeeper not configured, authentication disabled")
    except Exception as e:
        logger.warning(f"Failed to initialize Gatekeeper client: {e}")

    # Derive g.strata_is_admin from g.user's group membership. Runs after
    # gatekeeper's own before_request that populates g.user.
    @app.before_request
    def _populate_is_admin() -> None:
        user = g.get("user")
        g.strata_is_admin = bool(user and user.in_group(admin_group))


def _check_schema_version(db_path: str) -> None:
    """Abort startup with a clear message if the DB schema is older than the code.

    Catches the "image rebuilt without re-running migrations" footgun before
    a request hits a missing-column error.
    """
    from strata.db import EXPECTED_SCHEMA_VERSION

    try:
        conn = apsw.Connection(db_path, flags=apsw.SQLITE_OPEN_READONLY)
    except apsw.CantOpenError:
        # Fresh deploy with no DB yet; init-db will create one.
        return
    try:
        try:
            row = conn.execute(
                "SELECT value FROM db_metadata WHERE key='schema_version'"
            ).fetchone()
        except apsw.SQLError:
            row = None
    finally:
        conn.close()
    have = int(row[0]) if row else 0
    if have < EXPECTED_SCHEMA_VERSION:
        raise RuntimeError(
            f"strata DB schema_version={have}, code expects "
            f">= {EXPECTED_SCHEMA_VERSION}. "
            "Run: docker compose run --rm app strata-admin init-db "
            "(or: make db-init)"
        )


def _load_config_from_db(app: Flask) -> None:
    """Load configuration from the database into Flask app.config."""
    db_path = app.config["DATABASE_PATH"]

    try:
        conn = apsw.Connection(db_path, flags=apsw.SQLITE_OPEN_READONLY)
    except apsw.CantOpenError:
        return

    try:
        rows = conn.execute("SELECT key, value FROM app_setting").fetchall()
    except apsw.SQLError:
        conn.close()
        return

    db_values = {str(r[0]): str(r[1]) for r in rows}
    conn.close()

    # Load SECRET_KEY from database
    if "secret_key" in db_values:
        app.config["SECRET_KEY"] = db_values["secret_key"]

    # Apply registry entries
    for entry in REGISTRY:
        flask_key = KEY_MAP.get(entry.key)
        if not flask_key:
            continue

        raw = db_values.get(entry.key)
        if raw is not None:
            value = parse_value(entry, raw)
        else:
            value = entry.default

        app.config[flask_key] = value

    # Env vars override DB-loaded proxy config (so docker can enable ProxyFix
    # without seeding the SQLite settings table).
    for env_key in (
        "PROXY_X_FORWARDED_FOR",
        "PROXY_X_FORWARDED_PROTO",
        "PROXY_X_FORWARDED_HOST",
        "PROXY_X_FORWARDED_PREFIX",
    ):
        raw = os.environ.get(env_key)
        if raw is not None:
            app.config[env_key] = int(raw)

    # Apply ProxyFix if any proxy values are non-zero
    x_for = app.config.get("PROXY_X_FORWARDED_FOR", 0)
    x_proto = app.config.get("PROXY_X_FORWARDED_PROTO", 0)
    x_host = app.config.get("PROXY_X_FORWARDED_HOST", 0)
    x_prefix = app.config.get("PROXY_X_FORWARDED_PREFIX", 0)
    if any((x_for, x_proto, x_host, x_prefix)):
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(  # type: ignore[assignment]
            app.wsgi_app, x_for=x_for, x_proto=x_proto, x_host=x_host, x_prefix=x_prefix
        )
