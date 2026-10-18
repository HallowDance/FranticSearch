"""WSGI entry point for production servers: ``gunicorn franticsearch.wsgi:app``."""

from .app import create_app

app = create_app()
