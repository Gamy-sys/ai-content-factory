import pytest
from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.main import create_app
from tests.helpers import make_settings


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def db(settings):
    conn = connect(settings.database_path)
    init_db(conn)
    yield conn
    conn.close()


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client
