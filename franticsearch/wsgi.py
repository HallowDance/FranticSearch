"""WSGI entry point for production servers: ``gunicorn franticsearch.wsgi:app``."""

from . import logs
from .app import create_app

logs.configure()
app = create_app()
