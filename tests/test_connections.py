from urllib.parse import parse_qs

import httpx
import respx
from sqlalchemy import select

from app.models import Connection

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SECRET = "Sup3r~Segreto.Client_Secret"
TOKEN_URL = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
API = f"https://api.businesscentral.dynamics.com/v2.0/{TENANT}/Sandbox/api/microsoft/automation/v2.0"


def _form(**over):
    data = {
        "name": "Cliente Test",
        "tenant_id": TENANT,
        "client_id": CLIENT,
        "client_secret": SECRET,
        "environment": "Sandbox",
        "company": "",
    }
    data.update(over)
    return data


def _create(client, **over):
    return client.post("/connections", data=_form(**over), follow_redirects=False)


def _conn(app) -> Connection:
    with app.state.sessionmaker() as db:
        return db.scalars(select(Connection)).one()


def test_copy_reuses_secret_and_webhook_without_showing_them(auth_client, app):
    hook = "https://hooks.slack.com/services/T/B/SEGRETOHOOK"
    _create(auth_client, customer="Acme", webhook_kind="slack", webhook_url=hook, notify_emails="a@x.it")
    src = _conn(app)
    page = auth_client.get(f"/connections/{src.id}/copy")
    assert page.status_code == 200 and 'name="copy_from"' in page.text and "Cliente Test (copia)" in page.text
    assert SECRET not in page.text and "SEGRETOHOOK" not in page.text and src.client_secret_enc not in page.text
    # Copia con solo nome ed environment diversi: secret e webhook vuoti = quelli dell'originale
    r = auth_client.post("/connections", data=_form(
        name="Acme - UAT", environment="UAT", client_secret="", copy_from=src.id, customer="Acme",
        webhook_kind="slack", notify_emails="a@x.it"), follow_redirects=False)
    assert r.status_code == 303, r.text
    with app.state.sessionmaker() as db:
        copy = db.scalars(select(Connection).where(Connection.name == "Acme - UAT")).one()
        box = app.state.secret_box
        assert box.decrypt(copy.client_secret_enc) == SECRET and box.decrypt(copy.webhook_url_enc) == hook
        assert copy.customer == "Acme" and copy.email_list == ["a@x.it"] and copy.environment == "UAT"
    # Senza copia il secret resta obbligatorio
    r = auth_client.post("/connections", data=_form(name="Altra", client_secret=""))
    assert r.status_code == 422 and "Obbligatorio" in r.text


def test_list_is_grouped_by_customer(auth_client, app):
    _create(auth_client, name="P prod", customer="Acme", environment="Production", is_production="true")
    _create(auth_client, name="P sbx", customer="Acme", environment="Sandbox")
    _create(auth_client, name="Interna", environment="Test")
    text = auth_client.get("/connections").text
    assert text.index("Acme") < text.index("P prod") < text.index("Senza cliente") < text.index("Interna")
    assert "2 environment" in text
    deploy = auth_client.get("/deploy").text
    assert '<optgroup label="Acme">' in deploy


def test_invalid_notify_emails(auth_client):
    r = _create(auth_client, notify_emails="ok@x.it, non-una-email")
    assert r.status_code == 422 and "Indirizzo non valido" in r.text


def test_create_encrypts_secret_and_never_shows_it(auth_client, app):
    r = _create(auth_client, is_production="true")
    assert r.status_code == 303
    conn = _conn(app)
    assert conn.is_production
    assert SECRET not in conn.client_secret_enc
    assert app.state.secret_box.decrypt(conn.client_secret_enc) == SECRET
    for page in (auth_client.get("/connections"), auth_client.get(f"/connections/{conn.id}/edit")):
        assert page.status_code == 200
        assert SECRET not in page.text
        assert conn.client_secret_enc not in page.text


def test_validation_errors_do_not_echo_secret(auth_client):
    r = _create(auth_client, tenant_id="non valido", client_id="xyz")
    assert r.status_code == 422
    assert "Deve essere un GUID" in r.text
    assert SECRET not in r.text


def test_secret_required_on_create(auth_client):
    r = _create(auth_client, client_secret="")
    assert r.status_code == 422


def test_duplicate_name(auth_client):
    _create(auth_client)
    r = _create(auth_client)
    assert r.status_code == 422 and "Esiste già" in r.text


def test_update_with_empty_secret_keeps_old_one(auth_client, app):
    _create(auth_client)
    conn = _conn(app)
    r = auth_client.post(
        f"/connections/{conn.id}", data=_form(client_secret="", environment="Production"), follow_redirects=False
    )
    assert r.status_code == 303
    updated = _conn(app)
    assert updated.environment == "Production"
    assert app.state.secret_box.decrypt(updated.client_secret_enc) == SECRET


def test_update_replaces_secret(auth_client, app):
    _create(auth_client)
    conn = _conn(app)
    auth_client.post(f"/connections/{conn.id}", data=_form(client_secret="nuovo"), follow_redirects=False)
    assert app.state.secret_box.decrypt(_conn(app).client_secret_enc) == "nuovo"


def test_delete(auth_client, app):
    _create(auth_client)
    conn = _conn(app)
    r = auth_client.post(f"/connections/{conn.id}/delete")
    assert r.status_code == 204 and "conn_deleted" in r.headers["hx-redirect"]
    with app.state.sessionmaker() as db:
        assert db.scalars(select(Connection)).first() is None


@respx.mock
def test_test_connection_ok(auth_client, app):
    _create(auth_client, company="CRONUS IT")
    conn = _conn(app)
    token = respx.post(TOKEN_URL).respond(json={"access_token": "tok", "expires_in": 3600})
    companies = respx.get(f"{API}/companies").respond(
        json={"value": [{"id": "c1", "name": "CRONUS IT", "displayName": "CRONUS IT"}]}
    )
    r = auth_client.post(f"/connections/{conn.id}/test")
    assert r.status_code == 200
    assert "Token Entra ID ottenuto" in r.text and "Company &#39;CRONUS IT&#39; trovata" in r.text
    assert 'class="err"' not in r.text
    assert companies.calls.last.request.headers["authorization"] == "Bearer tok"
    sent = token.calls.last.request.content.decode()
    assert "scope=https%3A%2F%2Fapi.businesscentral.dynamics.com%2F.default" in sent


@respx.mock
def test_test_connection_bad_secret_shows_entra_error_without_secret(auth_client, app):
    _create(auth_client)
    conn = _conn(app)
    respx.post(TOKEN_URL).respond(
        401,
        json={
            "error": "invalid_client",
            "error_description": "AADSTS7000215: Invalid client secret provided.\r\nTrace ID: x",
        },
    )
    r = auth_client.post(f"/connections/{conn.id}/test")
    assert "AADSTS7000215" in r.text and 'class="err"' in r.text
    assert SECRET not in r.text and "Trace ID" not in r.text


@respx.mock
def test_test_connection_forbidden_gives_hint(auth_client, app):
    _create(auth_client)
    conn = _conn(app)
    respx.post(TOKEN_URL).respond(json={"access_token": "tok", "expires_in": 3600})
    respx.get(f"{API}/companies").respond(
        403, json={"error": {"code": "Authorization_InsufficientPermissions", "message": "no"}}
    )
    r = auth_client.post(f"/connections/{conn.id}/test")
    assert "Authorization_InsufficientPermissions" in r.text and "D365 AUTOMATION" in r.text


@respx.mock
def test_test_connection_missing_company(auth_client, app):
    _create(auth_client, company="Inesistente")
    conn = _conn(app)
    respx.post(TOKEN_URL).respond(json={"access_token": "tok", "expires_in": 3600})
    respx.get(f"{API}/companies").respond(json={"value": [{"id": "c1", "name": "Altra"}]})
    r = auth_client.post(f"/connections/{conn.id}/test")
    assert "non trovata" in r.text


def test_network_error_is_reported(auth_client, app, monkeypatch):
    _create(auth_client)
    conn = _conn(app)

    def boom(request):
        raise httpx.ConnectError("down")

    app.state.bc_transport = httpx.MockTransport(boom)
    r = auth_client.post(f"/connections/{conn.id}/test")
    assert "non raggiungibile" in r.text


# ---- elenchi di environment e company nel form

ADMIN_ENVS = "https://api.businesscentral.dynamics.com/admin/v2.29/applications/BusinessCentral/environments"
CREDS = {"tenant_id": TENANT, "client_id": CLIENT, "client_secret": SECRET}


@respx.mock
def test_lookup_environments_gives_a_dropdown(auth_client):
    respx.post(TOKEN_URL).respond(json={"access_token": "tok", "expires_in": 3600})
    respx.get(ADMIN_ENVS).respond(json={"value": [
        {"name": "Sandbox", "type": "Sandbox", "status": "Active"},
        {"name": "Production", "type": "Production", "status": "Active"},
    ]})
    r = auth_client.post("/connections/lookup/environments", data={**CREDS, "environment": "Sandbox"})
    assert '<select name="environment"' in r.text
    # produzione per prima, tipo letto da BC per spuntare "produzione"
    assert r.text.index('value="Production"') < r.text.index('value="Sandbox"')
    assert 'value="Production" data-production="true"' in r.text
    assert 'value="Sandbox" data-production="false" selected' in r.text
    assert SECRET not in r.text


@respx.mock
def test_lookup_environments_without_admin_permission_keeps_text_field(auth_client):
    respx.post(TOKEN_URL).respond(json={"access_token": "tok", "expires_in": 3600})
    respx.get(ADMIN_ENVS).respond(403, json={"error": {"code": "Forbidden", "message": "no"}})
    r = auth_client.post("/connections/lookup/environments", data={**CREDS, "environment": "UAT"})
    assert '<input name="environment" value="UAT"' in r.text
    assert "AdminCenter.ReadWrite.All" in r.text and SECRET not in r.text


def test_lookup_needs_credentials(auth_client):
    r = auth_client.post("/connections/lookup/environments", data={"tenant_id": "", "client_id": ""})
    assert "Compila prima Tenant ID e Client ID" in r.text
    r = auth_client.post("/connections/lookup/environments", data={**CREDS, "client_secret": ""})
    assert "Compila prima il client secret" in r.text


@respx.mock
def test_lookup_uses_saved_secret_when_editing(auth_client, app):
    _create(auth_client)
    conn = _conn(app)
    token = respx.post(TOKEN_URL).respond(json={"access_token": "tok", "expires_in": 3600})
    respx.get(f"{API}/companies").respond(json={"value": [
        {"id": "c1", "name": "CRONUS IT", "displayName": "CRONUS IT"},
        {"id": "c2", "name": "MY CO", "displayName": "My Company"},
    ]})
    r = auth_client.post("/connections/lookup/companies", data={
        **CREDS, "client_secret": "", "conn_id": conn.id, "environment": "Sandbox", "company": "MY CO"})
    assert '<select name="company">' in r.text and 'value="MY CO" selected' in r.text and "My Company" in r.text
    sent = parse_qs(token.calls.last.request.content.decode())
    assert sent["client_secret"] == [SECRET]  # quello salvato, decifrato lato server
    assert SECRET not in r.text


def test_edit_page_loads_lists_automatically(auth_client, app):
    _create(auth_client)
    conn = _conn(app)
    page = auth_client.get(f"/connections/{conn.id}/edit").text
    assert 'hx-post="/connections/lookup/environments" hx-trigger="load"' in page
    assert 'hx-post="/connections/lookup/companies" hx-trigger="load"' in page
    assert f'name="conn_id" value="{conn.id}"' in page
    # nuova connessione: niente caricamento automatico, servono prima le credenziali
    assert 'hx-trigger="load"' not in auth_client.get("/connections/new").text


def test_card_opens_edit_and_has_delete(auth_client, app):
    _create(auth_client)
    conn = _conn(app)
    page = auth_client.get("/connections").text
    assert f'class="conn-link conn-env" href="/connections/{conn.id}/edit">' in page
    assert f'hx-post="/connections/{conn.id}/delete"' in page and ">Modifica<" not in page
