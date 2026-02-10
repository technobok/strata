"""WSGI entry point for Strata (gunicorn wsgi:app)."""

from strata import create_app

app = create_app()
