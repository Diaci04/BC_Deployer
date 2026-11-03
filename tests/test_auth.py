import re
from datetime import timedelta

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.main import ROUTERS, create_app
from app.models import UserSession

from .conftest import PASSWORD, login

PUBLIC = {("/login", "GET"), ("/login", "POST"), ("/health", "GET"), ("/favicon.ico", "GET")}


def test_favicon_is_public(client):
    r = client.get("/favicon.ico", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/static/favicon.svg"
    icon = client.get("/static/favicon.svg")
    assert icon.status_code == 200 and icon.headers["content-type"].startswith("image/svg+xml")
    assert 'rel="icon" href="/static/favicon.svg"' in client.get("/login").text


def test_unauthenticated_html_redirects_to_login(client):
    r = client.get("/connections", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=/connections"


def test_every_route_requires_login(client):
    """Nessuna pagina accessibile senza login: verifica su tutte le route registrate."""
    checked = 0
    for route in (r for router in ROUTERS for r in router.routes):
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods:
            if (route.path, method) in PUBLIC:
                continue
            path = re.sub(r"{[^}]+}", "1", route.path)
            r = client.request(method, path, follow_redirects=False)
            assert r.status_code in (401, 303), f"{method} {route.path} -> {r.status_code}"
            if r.status_code == 303:
                assert r.headers["location"].startswith("/login")
            checked += 1
    assert checked >= 8


def test_login_sets_hardened_cookie(client):
    r = login(client)
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = next(c for c in r.headers.get_list("set-cookie") if c.startswith("__Host-bcd_session="))
    low = cookie.lower()
    assert "httponly" in low and "secure" in low and "samesite=strict" in low and "path=/" in low
    assert client.get("/").status_code == 200


def test_wrong_credentials(client):
    assert login(client, password="sbagliata").status_code == 401
    assert login(client, username="root").status_code == 401


def test_login_requires_form_token(client):
    r = client.post("/login", data={"username": "admin", "password": PASSWORD, "csrf_token": "x"})
    assert r.status_code == 400


def test_rate_limit_blocks_even_correct_password(client, settings):
    for _ in range(settings.login_max_attempts):
        assert login(client, password="sbagliata").status_code == 401
    r = login(client)
    assert r.status_code == 429
    assert "Troppi tentativi" in r.text


def test_open_redirect_is_blocked(client):
    r = login(client, next_url="//evil.example.com/x")
    assert r.headers["location"] == "/"
    login(client, next_url="/connections")  # sessione già attiva, ma il redirect resta locale


def test_logout_invalidates_session(auth_client):
    r = auth_client.post("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert auth_client.get("/", follow_redirects=False).status_code in (303, 401)


def test_post_without_csrf_is_rejected(auth_client):
    del auth_client.headers["X-CSRF-Token"]
    r = auth_client.post("/logout", follow_redirects=False)
    assert r.status_code == 403


def test_csrf_from_form_field(auth_client):
    token = auth_client.headers.pop("X-CSRF-Token")
    r = auth_client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303


def test_cross_site_request_is_rejected(auth_client):
    r = auth_client.post("/logout", headers={"origin": "https://evil.example.com"}, follow_redirects=False)
    assert r.status_code == 403
    r = auth_client.post("/logout", headers={"sec-fetch-site": "cross-site"}, follow_redirects=False)
    assert r.status_code == 403


def test_idle_session_expires(auth_client, app, settings):
    with app.state.sessionmaker() as db:
        sess = db.query(UserSession).one()
        sess.last_seen_at -= timedelta(minutes=settings.session_idle_minutes + 1)
        db.commit()
    assert auth_client.get("/", follow_redirects=False).status_code in (303, 401)


def test_htmx_request_without_session_gets_hx_redirect(client):
    r = client.post("/connections/1/test", headers={"hx-request": "true"})
    assert r.status_code == 401
    assert r.headers["hx-redirect"].startswith("/login")


def test_security_headers(client):
    r = client.get("/login")
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "'unsafe-inline'" not in csp
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "no-store"


def test_health_is_public(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["database"] == "ok"


@pytest.fixture
def prod_app(settings):
    return create_app(settings.model_copy(update={"app_env": "production"}))


def test_production_forces_https_and_hsts(prod_app):
    with TestClient(prod_app, base_url="http://testserver") as c:
        r = c.get("/login", follow_redirects=False)
        assert r.status_code == 308 and r.headers["location"].startswith("https://")
        assert c.get("/health").status_code == 200  # probe interni in HTTP
    with TestClient(prod_app, base_url="https://testserver") as c:
        assert "max-age" in c.get("/login").headers["strict-transport-security"]
