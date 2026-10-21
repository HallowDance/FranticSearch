"""Logging setup (docs/SCALING.md § O3)."""

import logging

import pytest
from conftest import PASSWORD

from franticsearch import logs


@pytest.fixture
def restore_root():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield root
    root.handlers[:] = handlers
    root.setLevel(level)


def test_configure_level_and_format(restore_root, monkeypatch):
    monkeypatch.delenv("JOURNAL_STREAM", raising=False)
    monkeypatch.setenv("FS_LOG_LEVEL", "warning")
    logs.configure()
    assert restore_root.level == logging.WARNING and len(restore_root.handlers) == 1
    assert restore_root.handlers[0].formatter._fmt.startswith("%(asctime)s")
    monkeypatch.setenv("JOURNAL_STREAM", "8:1234")             # the journal timestamps lines itself
    logs.configure("debug")
    assert restore_root.level == logging.DEBUG and len(restore_root.handlers) == 1
    assert restore_root.handlers[0].formatter._fmt.startswith("%(levelname)s")


def test_configure_unknown_level_falls_back(restore_root, monkeypatch, capsys):
    monkeypatch.setenv("FS_LOG_LEVEL", "chatty")
    logs.configure()                  # replaces the root handlers, so read stderr, not caplog
    assert restore_root.level == logging.INFO
    assert "WARNING franticsearch.logs: unknown FS_LOG_LEVEL 'CHATTY', using INFO" in capsys.readouterr().err


def test_logins_are_logged_without_passwords(app, conn, caplog):
    caplog.set_level(logging.INFO)
    c = app.test_client()
    c.post("/login", data={"username": "alice", "password": "wrong password"})
    c.post("/login", data={"username": "alice", "password": PASSWORD})
    msgs = [(r.levelname, r.getMessage()) for r in caplog.records if r.name == "franticsearch.app"]
    assert msgs == [("WARNING", "failed login for 'alice' from 127.0.0.1"),
                    ("INFO", "login: alice from 127.0.0.1")]
    assert PASSWORD not in caplog.text and "wrong password" not in caplog.text


def test_admin_actions_are_logged(client, conn, caplog):
    caplog.set_level(logging.INFO)
    client.post("/admin/users", data={"username": "carol", "password": ""})
    assert "admin alice created user carol" in caplog.text


def test_server_errors_are_logged_with_traceback(conn, db_path, caplog):
    from franticsearch.app import create_app
    app = create_app({"DATABASE": db_path, "AUTO_SYNC": False, "PROPAGATE_EXCEPTIONS": False})
    app.add_url_rule("/boom", "boom", lambda: 1 / 0)
    from franticsearch.app import PUBLIC_ENDPOINTS
    PUBLIC_ENDPOINTS.add("boom")
    try:
        assert app.test_client().get("/boom").status_code == 500
    finally:
        PUBLIC_ENDPOINTS.discard("boom")
    (rec,) = [r for r in caplog.records if r.levelname == "ERROR"]
    assert "Exception on /boom [GET]" in rec.getMessage() and "ZeroDivisionError" in rec.exc_text
