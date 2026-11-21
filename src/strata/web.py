"""Web server entry point for strata-web."""

import os

import click
from flask import Flask


def _int_env(name: str) -> int | None:
    value = os.environ.get(name)
    return int(value) if value else None


@click.command()
@click.option("--host", default=None, help="Bind address (overrides config)")
@click.option("--port", default=None, type=int, help="Port (overrides config)")
@click.option("--workers", default=2, help="Number of gunicorn workers")
@click.option("--dev", is_flag=True, help="Run Flask development server with debug mode")
def main(host: str | None, port: int | None, workers: int, dev: bool) -> None:
    """Start the Strata web server."""
    if dev:
        from strata import create_app

        app = create_app()
        # No separate worker in dev: requested builds run in the request.
        app.config["WORKER_INLINE"] = True
        run_host = host or os.environ.get("HOST") or app.config.get("DEV_HOST", "127.0.0.1")
        run_port = port or _int_env("PORT") or app.config.get("DEV_PORT", 5000)
        app.run(debug=True, host=run_host, port=run_port)
    else:
        # The gunicorn master reads its settings only: building the app here would
        # import duckdb (and start its threads) before the workers fork. See
        # strata.server_settings.
        from strata import server_settings

        config = server_settings()
        run_host = host or os.environ.get("HOST") or config.get("HOST", "0.0.0.0")
        run_port = port or _int_env("PORT") or config.get("PORT", 5000)

        import gunicorn.app.base

        # gthread: requests run on threads while each worker's main loop keeps
        # heartbeating, so a long export or build is not killed at `timeout`
        # (with the sync worker it was: any request over 30s died).
        threads = int(config.get("SERVER_THREADS", 8))
        timeout = int(config.get("SERVER_TIMEOUT", 120))

        class StrataApp(gunicorn.app.base.BaseApplication):
            def load_config(self) -> None:
                self.cfg.set("bind", f"{run_host}:{run_port}")  # type: ignore[union-attr]
                self.cfg.set("workers", str(workers))  # type: ignore[union-attr]
                self.cfg.set("worker_class", "gthread")  # type: ignore[union-attr]
                self.cfg.set("threads", str(threads))  # type: ignore[union-attr]
                self.cfg.set("timeout", str(timeout))  # type: ignore[union-attr]
                # No preload: each worker builds its app after the fork.
                self.cfg.set("preload_app", False)  # type: ignore[union-attr]

            def load(self) -> Flask:
                from strata import create_app

                return create_app()

        StrataApp().run()
