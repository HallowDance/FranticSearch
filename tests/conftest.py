from pathlib import Path

import pytest

from franticsearch import db, scryfall, users
from franticsearch.app import create_app

FIXTURE = Path(__file__).parent / "fixtures_default_cards.json"

SOL_C21 = "00000000-0000-0000-0000-000000000001"
SOL_CMM = "00000000-0000-0000-0000-000000000002"
DELVER = "00000000-0000-0000-0000-000000000003"
BOLT = "00000000-0000-0000-0000-000000000004"
SOL_ORACLE = "aaaaaaaa-0000-0000-0000-000000000001"
ALICE = 1  # admin user created by the conn fixture
PASSWORD = "correct horse"


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def conn(db_path):
    c = db.connect(db_path)
    db.migrate(c)
    with open(FIXTURE, "rb") as fp:
        scryfall.import_stream(c, fp)
    assert users.create(c, "alice", PASSWORD, is_admin=True) == ALICE
    yield c
    c.close()


@pytest.fixture
def app(conn, db_path):
    return create_app({"DATABASE": db_path, "AUTO_SYNC": False, "TESTING": True, "CSRF": False})


def login_as(client, user_id):
    with client.session_transaction() as sess:
        sess.clear()
        sess["uid"] = user_id
    return client


@pytest.fixture
def client(app):
    return login_as(app.test_client(), ALICE)
