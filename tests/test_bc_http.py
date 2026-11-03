from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
import respx

from app.bc.http import BCAuthError, BCError, BCHttp, retry_after_seconds

TENANT = "contoso.onmicrosoft.com"
SECRET = "il-mio-secret"
TOKEN_URL = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
API = f"https://api.businesscentral.dynamics.com/v2.0/{TENANT}/Sandbox/api/microsoft/automation/v2.0"


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def http(sleeps):
    with BCHttp(TENANT, "cid", SECRET, "Sandbox", sleep=sleeps.append, max_retries=3) as h:
        yield h


@pytest.fixture
def mock_token():
    with respx.mock(assert_all_called=False) as m:
        m.post(TOKEN_URL).respond(json={"access_token": "tok", "expires_in": 3600})
        yield m


def test_token_is_cached(http, mock_token):
    mock_token.get(f"{API}/companies").respond(json={"value": []})
    http.request("GET", "/companies")
    http.request("GET", "/companies")
    assert mock_token.routes[0].call_count == 1


def test_retry_after_seconds_is_respected(http, mock_token, sleeps):
    route = mock_token.get(f"{API}/companies")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(503),
        httpx.Response(200, json={"value": []}),
    ]
    assert http.request("GET", "/companies").status_code == 200
    assert sleeps[0] == 7
    assert len(sleeps) == 2 and 4 <= sleeps[1] <= 5  # backoff senza Retry-After


def test_retry_after_http_date():
    when = datetime.now(UTC) + timedelta(seconds=30)
    r = httpx.Response(429, headers={"Retry-After": format_datetime(when, usegmt=True)})
    assert 25 <= retry_after_seconds(r) <= 30
    assert retry_after_seconds(httpx.Response(429, headers={"Retry-After": "boh"})) is None
    assert retry_after_seconds(httpx.Response(429, headers={"Retry-After": "99999"})) == 120


def test_gives_up_after_max_retries(http, mock_token):
    mock_token.get(f"{API}/companies").respond(500, json={"error": {"code": "Internal", "message": "kaboom"}})
    with pytest.raises(BCError, match="HTTP 500: Internal: kaboom"):
        http.request("GET", "/companies")
    assert mock_token.routes[1].call_count == 4


def test_401_refreshes_token_once(http, sleeps):
    with respx.mock() as m:
        tok = m.post(TOKEN_URL)
        tok.side_effect = [
            httpx.Response(200, json={"access_token": "old", "expires_in": 3600}),
            httpx.Response(200, json={"access_token": "new", "expires_in": 3600}),
        ]
        api = m.get(f"{API}/companies")
        api.side_effect = [httpx.Response(401), httpx.Response(200, json={"value": []})]
        http.request("GET", "/companies")
        assert api.calls[1].request.headers["authorization"] == "Bearer new"


def test_odata_error_message_is_extracted(http, mock_token):
    mock_token.post(f"{API}/companies(x)/extensionUpload").respond(
        400, json={"error": {"code": "BadRequest", "message": "The schedule value is invalid."}}
    )
    with pytest.raises(BCError) as exc:
        http.request("POST", "/companies(x)/extensionUpload", json={})
    assert exc.value.status_code == 400
    assert "BadRequest: The schedule value is invalid." in str(exc.value)


def test_body_is_reopened_on_each_retry(http, mock_token, tmp_path):
    """Lo script originale ritentava il PATCH con lo stream già consumato (body vuoto)."""
    f = tmp_path / "a.app"
    f.write_bytes(b"x" * 100_000)
    route = mock_token.patch(f"{API}/content")
    route.side_effect = [httpx.Response(503), httpx.Response(204)]
    http.request("PATCH", "/content", content_factory=lambda: open(f, "rb"))
    assert [len(c.request.content) for c in route.calls] == [100_000, 100_000]


def test_network_errors_are_retried(http, mock_token):
    route = mock_token.get(f"{API}/companies")
    route.side_effect = [httpx.ConnectError("down"), httpx.Response(200, json={"value": []})]
    assert http.request("GET", "/companies").status_code == 200


def test_auth_error_never_contains_secret(http):
    with respx.mock() as m:
        m.post(TOKEN_URL).respond(400, json={"error": "invalid_client", "error_description": "AADSTS7000215: bad"})
        with pytest.raises(BCAuthError) as exc:
            http.token()
    assert "AADSTS7000215" in str(exc.value)
    assert SECRET not in str(exc.value)
    assert SECRET not in repr(http)


def test_special_characters_in_environment_are_quoted():
    h = BCHttp("t", "c", "s", "My Env/../x")
    assert "/My%20Env%2F..%2Fx/" in h.base
