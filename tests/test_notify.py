"""Notifiche webhook a fine deploy."""
import json

import httpx
import pytest
import respx
from sqlalchemy import select

from app.models import Connection, RunLog
from app.services.notify import build_payload, validate_webhook_url

from .fakebc import CLIENT, TENANT, FakeBC
from .test_deploy_flow import B, get_run, upload_ok

HOOK = "https://prod-01.westeurope.logic.azure.com/workflows/abc/triggers/manual/paths/invoke?sig=SEGRETO123"
SLACK = "https://hooks.slack.com/services/T000/B000/SEGRETOSLACK"


@pytest.fixture
def client(auth_client, app):
    svc = app.state.deploy
    svc._sleep = lambda s: None
    svc.submit = svc._execute
    return auth_client


@pytest.fixture
def bc():
    with respx.mock(assert_all_called=False) as m:
        yield m


def make_conn(client, app, **extra) -> int:
    data = {
        "name": "Notificata", "tenant_id": TENANT, "client_id": CLIENT, "client_secret": "s",
        "environment": "Sandbox", "company": "", **extra,
    }
    r = client.post("/connections", data=data, follow_redirects=False)
    assert r.status_code == 303, r.text
    with app.state.sessionmaker() as db:
        return db.scalars(select(Connection)).one().id


def run_logs(app, run_id):
    with app.state.sessionmaker() as db:
        return [l.message for l in db.scalars(select(RunLog).where(RunLog.run_id == run_id))]


def test_webhook_url_is_encrypted_and_never_shown(client, app):
    conn_id = make_conn(client, app, webhook_kind="teams", webhook_url=HOOK)
    with app.state.sessionmaker() as db:
        conn = db.get(Connection, conn_id)
        assert "SEGRETO123" not in conn.webhook_url_enc
        assert app.state.secret_box.decrypt(conn.webhook_url_enc) == HOOK
    for page in (client.get("/connections"), client.get(f"/connections/{conn_id}/edit")):
        assert "SEGRETO123" not in page.text and "logic.azure.com" not in page.text
    assert "configurato" in client.get(f"/connections/{conn_id}/edit").text


def test_webhook_validation(client, app):
    base = {"name": "X", "tenant_id": TENANT, "client_id": CLIENT, "client_secret": "s", "environment": "Sandbox"}
    r = client.post("/connections", data={**base, "webhook_kind": "slack", "webhook_url": "http://hooks.slack.com/x"})
    assert r.status_code == 422 and "https://" in r.text
    r = client.post("/connections", data={**base, "webhook_kind": "slack"})
    assert r.status_code == 422 and "URL del webhook" in r.text
    r = client.post("/connections", data={**base, "webhook_kind": "slack", "webhook_url": "https://hooks.slack.com/x/" + "SEGRETO" + "x" * 2000})
    assert r.status_code == 422 and "SEGRETO" not in r.text
    assert validate_webhook_url("https://user:pw@example.com/x") is not None


def test_update_keeps_or_removes_webhook(client, app):
    conn_id = make_conn(client, app, webhook_kind="slack", webhook_url=SLACK)
    base = {"name": "Notificata", "tenant_id": TENANT, "client_id": CLIENT, "environment": "Sandbox"}
    client.post(f"/connections/{conn_id}", data={**base, "webhook_kind": "slack", "notify_on": "failure"})
    with app.state.sessionmaker() as db:
        conn = db.get(Connection, conn_id)
        assert app.state.secret_box.decrypt(conn.webhook_url_enc) == SLACK and conn.notify_on == "failure"
    client.post(f"/connections/{conn_id}", data={**base, "webhook_kind": "slack", "remove_webhook": "true"})
    with app.state.sessionmaker() as db:
        conn = db.get(Connection, conn_id)
        assert conn.webhook_url_enc is None and conn.webhook_kind is None


def test_teams_notification_after_deploy(client, app, bc, settings):
    app.state.settings.public_base_url = "https://bcd.example.com/"
    conn_id = make_conn(client, app, webhook_kind="teams", webhook_url=HOOK)
    FakeBC(bc)
    hook = bc.post(HOOK).respond(202)
    client.post("/deploy", data={"connection_id": conn_id, "file_ids": [upload_ok(client, **B)]})
    run = get_run(app)
    payload = json.loads(hook.calls.last.request.content)
    card = payload["attachments"][0]["content"]
    assert payload["type"] == "message" and card["type"] == "AdaptiveCard"
    assert "Completato" in card["body"][0]["text"] and "Notificata (Sandbox)" in card["body"][0]["text"]
    assert card["actions"][0]["url"] == f"https://bcd.example.com/runs/{run.id}"
    assert "Notifica Microsoft Teams inviata" in run_logs(app, run.id)


def test_notify_only_on_failure(client, app, bc):
    conn_id = make_conn(client, app, webhook_kind="slack", webhook_url=SLACK, notify_on="failure")
    fake = FakeBC(bc)
    hook = bc.post(SLACK).respond(200)
    client.post("/deploy", data={"connection_id": conn_id, "file_ids": [upload_ok(client, **B)]})
    assert hook.call_count == 0
    fake.outcomes["App B"] = "Failed"
    client.post("/deploy", data={"connection_id": conn_id, "file_ids": [upload_ok(client, **{**B, "version": "2.0.0.0"})]})
    assert hook.call_count == 1
    text = json.loads(hook.calls.last.request.content)["text"]
    assert "Fallito" in text and "App B 2.0.0.0: FALLITA" in text


def test_failed_notification_is_logged_without_url(client, app, bc):
    conn_id = make_conn(client, app, webhook_kind="slack", webhook_url=SLACK)
    FakeBC(bc)
    bc.post(SLACK).respond(404)
    client.post("/deploy", data={"connection_id": conn_id, "file_ids": [upload_ok(client, **B)]})
    run = get_run(app)
    logs = " ".join(run_logs(app, run.id))
    assert "non inviata: HTTP 404" in logs and "SEGRETOSLACK" not in logs and "hooks.slack.com" not in logs
    assert run.status == "succeeded"  # la notifica non cambia l'esito del deploy


def test_test_webhook_button(client, app, bc):
    conn_id = make_conn(client, app, webhook_kind="slack", webhook_url=SLACK)
    route = bc.post(SLACK)
    route.side_effect = [httpx.Response(200), httpx.ConnectError("x"), httpx.ConnectError("x")]
    r = client.post(f"/connections/{conn_id}/test-webhook")
    assert "Messaggio di prova inviato a Slack" in r.text
    r = client.post(f"/connections/{conn_id}/test-webhook")
    assert "Invio non riuscito: errore di rete" in r.text and "SEGRETOSLACK" not in r.text


def test_generic_payload():
    from app.models import Run, RunItem

    run = Run(id=7, connection_name="C", environment="E", trigger="manual", triggered_by="admin", status="failed",
              error="boom", items=[RunItem(name="A", version="1.0.0.0", status="failed", error_message="x")])
    p = build_payload("generic", run, None)
    assert p["event"] == "deploy.finished" and p["run_id"] == 7 and p["apps"][0]["error"] == "x"
