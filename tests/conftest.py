import os

# Su alcune macchine Windows i criteri di controllo applicazioni bloccano le estensioni C di SQLAlchemy
os.environ.setdefault("DISABLE_SQLALCHEMY_CEXT_RUNTIME", "1")

import re  # noqa: E402

import pytest  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.bc.http import token_cache  # noqa: E402
from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.security.auth import hash_password  # noqa: E402

PASSWORD = "una-password-di-test-lunga"


@pytest.fixture(scope="session")
def pw_hash():
    return hash_password(PASSWORD)


@pytest.fixture
def settings(tmp_path, pw_hash):
    return Settings(
        _env_file=None,
        app_env="development",
        data_dir=tmp_path,
        admin_password_hash=pw_hash,
        fernet_keys=Fernet.generate_key().decode(),
        cookie_secure=True,
    )


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app, base_url="https://testserver") as c:
        yield c


@pytest.fixture(autouse=True)
def _clear_token_cache():
    token_cache.clear()
    yield
    token_cache.clear()


def login(client, username="admin", password=PASSWORD, next_url="/"):
    page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    return client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": token, "next": next_url},
        follow_redirects=False,
    )


def csrf_token(client) -> str:
    page = client.get("/")
    return re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)


@pytest.fixture
def auth_client(client):
    r = login(client)
    assert r.status_code == 303
    client.headers["X-CSRF-Token"] = csrf_token(client)
    return client
