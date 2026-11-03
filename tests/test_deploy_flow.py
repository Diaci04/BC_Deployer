"""Flusso completo di deploy manuale contro un finto Business Central."""
import re
import threading
import time

import pytest
import respx
from sqlalchemy import select

from app.models import AppFile, Connection, Run, RunItem, RunLog

from .apps import APP_A, APP_B, app_bytes
from .fakebc import CLIENT, TENANT, FakeBC

SECRET = "Segreto-Che-Non-Deve-Comparire"
EXT = "eeeeeeee-0000-0000-0000-00000000000e"

A = dict(app_id=APP_A, name="App A", version="1.0.0.0", deps=[(APP_B, "App B", "Contoso", "1.0.0.0")])
B = dict(app_id=APP_B, name="App B", version="1.0.0.0")


@pytest.fixture
def client(auth_client, app):
    svc = app.state.deploy
    svc._sleep = lambda s: None
    svc.submit = svc._execute  # esecuzione sincrona nei test
    return auth_client


@pytest.fixture
def bc():
    with respx.mock(assert_all_called=False) as m:
        yield m


def make_conn(client, app, *, prod=False, env="Sandbox") -> int:
    data = {
        "name": f"Conn {env}", "tenant_id": TENANT, "client_id": CLIENT, "client_secret": SECRET,
        "environment": env, "company": "",
    }
    if prod:
        data["is_production"] = "true"
    assert client.post("/connections", data=data, follow_redirects=False).status_code == 303
    with app.state.sessionmaker() as db:
        return db.scalars(select(Connection).where(Connection.environment == env)).one().id


def upload(client, name, data):
    return client.put(f"/uploads?name={name}", content=data, headers={"content-type": "application/octet-stream"})


def upload_ok(client, **manifest) -> int:
    r = upload(client, f"{manifest['name'].replace(' ', '_')}.app", app_bytes(**manifest))
    assert r.status_code == 200, r.text
    return r.json()["id"]


def start(client, conn_id, file_ids, **extra):
    return client.post("/deploy", data={"connection_id": conn_id, "file_ids": file_ids, **extra})


def get_run(app, run_id=None) -> Run:
    with app.state.sessionmaker() as db:
        q = select(Run).order_by(Run.id.desc())
        run = db.get(Run, run_id) if run_id else db.scalars(q).first()
        run.items  # carica gli item
        db.expunge_all()
        return run


def logs(app, run_id) -> list[str]:
    with app.state.sessionmaker() as db:
        return [l.message for l in db.scalars(select(RunLog).where(RunLog.run_id == run_id).order_by(RunLog.id))]


# ---------------------------------------------------------------- upload
def test_upload_rejects_non_app_extension(client):
    r = upload(client, "virus.exe", b"NAVX")
    assert r.status_code == 422 and ".app" in r.text


def test_upload_rejects_invalid_package_and_cleans_up(client, settings):
    r = upload(client, "finto.app", b"not a package" * 100)
    assert r.status_code == 422 and "NAVX" in r.text
    assert list(settings.upload_dir.iterdir()) == []


def test_upload_size_limit_while_streaming(client, app, settings):
    app.state.settings.max_app_size_mb = 1
    r = upload(client, "grande.app", app_bytes(**A) + b"\x00" * (1024 * 1024 + 10))
    assert r.status_code == 413
    assert list(settings.upload_dir.iterdir()) == []


def test_upload_sanitizes_name_and_dedupes(client, app, settings):
    data = app_bytes(**B)
    r1 = upload(client, "..%2F..%2Fetc%2Fevil<script>.app", data)
    assert r1.status_code == 200
    f = r1.json()
    assert f["file"] == "evil_script_.app" and f["name"] == "App B" and f["version"] == "1.0.0.0"
    assert upload(client, "copia.app", data).json()["id"] == f["id"]
    stored = list(settings.upload_dir.iterdir())
    assert len(stored) == 1 and re.fullmatch(r"[0-9a-f]{32}\.app", stored[0].name)


def test_staged_upload_can_be_removed(client, app, settings):
    fid = upload_ok(client, **B)
    assert client.delete(f"/uploads/{fid}").status_code == 204
    with app.state.sessionmaker() as db:
        assert db.get(AppFile, fid).status == "deleted"
    assert list(settings.upload_dir.iterdir()) == []


# ---------------------------------------------------------------- anteprima e verifica
def test_preview_shows_dependency_order(client, app):
    conn = make_conn(client, app)
    ids = [upload_ok(client, **A), upload_ok(client, **B)]
    r = client.post("/deploy/preview", data={"connection_id": conn, "file_ids": ids})
    assert r.status_code == 200
    assert r.text.index("App B") < r.text.index("App A")
    assert "Dipende da: App B" in r.text and 'hx-post="/deploy/precheck"' in r.text


def test_preview_reports_duplicate_app_id(client, app):
    ids = [upload_ok(client, **B), upload_ok(client, **{**B, "version": "2.0.0.0"})]
    r = client.post("/deploy/preview", data={"file_ids": ids})
    assert "stesso app id" in r.text


def test_precheck_against_environment(client, app, bc):
    conn = make_conn(client, app)
    FakeBC(bc, installed=[{"id": APP_B, "name": "App B", "version": "1.0.0.0"}])
    ids = [upload_ok(client, **A), upload_ok(client, **B)]
    r = client.post("/deploy/precheck", data={"connection_id": conn, "file_ids": ids})
    assert "Già installata: verrà saltata" in r.text and "Nuova installazione" in r.text
    assert "Verifiche superate" in r.text


# ---------------------------------------------------------------- esecuzione
def test_successful_deploy(client, app, bc):
    conn = make_conn(client, app)
    fake = FakeBC(bc)
    ids = [upload_ok(client, **A), upload_ok(client, **B)]
    r = start(client, conn, ids, schema_sync="Add", schedule="Current version")
    assert r.status_code == 204
    run = get_run(app)
    assert r.headers["hx-redirect"] == f"/runs/{run.id}"
    assert run.status == "succeeded" and run.trigger == "manual" and run.triggered_by == "admin"
    assert [(i.name, i.status) for i in run.items] == [("App B", "succeeded"), ("App A", "succeeded")]
    assert [c for c in fake.calls if c.startswith("install")] == ["install:App B", "install:App A"]
    all_logs = " ".join(logs(app, run.id))
    assert "Stato in BC: InProgress" in all_logs and "Deploy completato" in all_logs

    page = client.get(f"/runs/{run.id}")
    assert page.status_code == 200 and "Completata" in page.text
    assert SECRET not in page.text and SECRET not in all_logs

    # SSE di una run conclusa: snapshot completo e chiusura dello stream
    with client.stream("GET", f"/runs/{run.id}/events") as s:
        body = "".join(s.iter_text())
    assert s.headers["content-type"].startswith("text/event-stream")
    assert "event: log" in body and '"status": "succeeded"' in body
    assert body.count("event: item") == 2


def test_sse_resumes_after_last_event_id(client, app, bc):
    conn = make_conn(client, app)
    FakeBC(bc)
    start(client, conn, [upload_ok(client, **B)])
    run = get_run(app)
    with app.state.sessionmaker() as db:
        ids = [l.id for l in db.scalars(select(RunLog).where(RunLog.run_id == run.id).order_by(RunLog.id))]
    with client.stream("GET", f"/runs/{run.id}/events", headers={"Last-Event-ID": str(ids[-2])}) as s:
        body = "".join(s.iter_text())
    assert body.count("event: log") == 1


def test_failure_stops_and_skips_following_apps_then_rerun(client, app, bc):
    conn = make_conn(client, app)
    fake = FakeBC(bc, outcomes={"App B": "Failed"})
    ids = [upload_ok(client, **A), upload_ok(client, **B)]
    start(client, conn, ids)
    run = get_run(app)
    assert run.status == "failed"
    b, a = run.items
    assert b.status == "failed" and "Extension Deployment Status" in b.error_message
    assert a.status == "skipped"
    assert "install:App A" not in fake.calls
    assert 'hx-post="/runs/%d/rerun"' % run.id in client.get(f"/runs/{run.id}").text

    fake.outcomes.clear()
    r = client.post(f"/runs/{run.id}/rerun")
    assert r.status_code == 204
    new = get_run(app)
    assert new.id != run.id and new.parent_run_id == run.id and new.trigger == "rerun"
    assert new.status == "succeeded"


def test_same_version_is_skipped(client, app, bc):
    conn = make_conn(client, app)
    fake = FakeBC(bc, installed=[{"id": APP_B, "name": "App B", "version": "1.0.0.0"}])
    start(client, conn, [upload_ok(client, **A), upload_ok(client, **B)])
    run = get_run(app)
    assert [(i.name, i.status) for i in run.items] == [("App B", "already_installed"), ("App A", "succeeded")]
    assert fake.calls.count("create") == 1


def test_precheck_failure_blocks_before_any_upload(client, app, bc):
    conn = make_conn(client, app)
    fake = FakeBC(bc)
    start(client, conn, [upload_ok(client, **{**B, "deps": [(EXT, "Esterna", "Contoso", "1.0.0.0")]})])
    run = get_run(app)
    assert run.status == "failed" and "Esterna" in run.error
    assert [i.status for i in run.items] == ["skipped"]
    assert "create" not in fake.calls


def test_scheduled_for_next_update_is_not_polled(client, app, bc):
    conn = make_conn(client, app)
    fake = FakeBC(bc)
    start(client, conn, [upload_ok(client, **B)], schedule="Next minor version")
    run = get_run(app)
    assert run.status == "succeeded" and run.items[0].status == "scheduled"
    assert fake.uploads[next(iter(fake.uploads))]["body"]["schedule"] == "Next minor version"
    assert fake.polls == {}


def test_bc_error_message_is_shown(client, app, bc):
    conn = make_conn(client, app)
    fake = FakeBC(bc)
    fake.install_error = 400
    start(client, conn, [upload_ok(client, **B)])
    run = get_run(app)
    assert run.status == "failed" and "HTTP 400: Internal: boom" in run.items[0].error_message


def test_production_requires_typed_environment(client, app, bc):
    conn = make_conn(client, app, prod=True, env="Production")
    FakeBC(bc, env="Production")
    ids = [upload_ok(client, **B)]
    preview = client.post("/deploy/preview", data={"connection_id": conn, "file_ids": ids})
    assert 'name="confirm_env"' in preview.text
    for wrong in ("", "production", "Sandbox"):
        r = start(client, conn, ids, confirm_env=wrong)
        assert r.status_code == 200 and "digita esattamente" in r.text
    assert start(client, conn, ids, confirm_env="Production").status_code == 204
    assert get_run(app).status == "succeeded"


def test_invalid_options_are_rejected(client, app):
    conn = make_conn(client, app)
    r = start(client, conn, [upload_ok(client, **B)], schema_sync="Drop Everything")
    assert "non valide" in r.text


def test_cancel_marks_remaining_apps_skipped(client, app, bc):
    conn = make_conn(client, app)
    FakeBC(bc)
    svc = app.state.deploy
    svc.submit = lambda run_id: None  # creiamo la run senza eseguirla
    start(client, conn, [upload_ok(client, **A), upload_ok(client, **B)])
    run = get_run(app)
    assert client.post(f"/runs/{run.id}/cancel").status_code == 204
    svc._execute(run.id)
    run = get_run(app, run.id)
    assert run.status == "canceled" and {i.status for i in run.items} == {"skipped"}


# ---------------------------------------------------------------- lock e ripristino
def test_one_deploy_at_a_time_per_environment(client, app, bc):
    conn = make_conn(client, app)
    FakeBC(bc)
    svc = app.state.deploy
    svc.submit = lambda run_id: None
    start(client, conn, [upload_ok(client, **B)])
    run = get_run(app)

    lock = svc._lock_for(svc._env_key(TENANT, "sandbox"))  # chiave indipendente dalle maiuscole
    lock.acquire()
    t = threading.Thread(target=svc._execute, args=(run.id,))
    t.start()
    deadline = time.time() + 5
    while time.time() < deadline and not any("in coda" in m for m in logs(app, run.id)):
        time.sleep(0.05)
    assert get_run(app, run.id).status == "queued"
    assert svc.is_busy(TENANT, "Sandbox")
    lock.release()
    t.join(10)
    assert get_run(app, run.id).status == "succeeded"


def test_recover_interrupted_runs(app, client):
    with app.state.sessionmaker() as db:
        run = Run(connection_name="x", environment="e", tenant_id="t", trigger="manual", triggered_by="admin",
                  schema_sync="Add", schedule="Current version", status="running")
        run.items = [
            RunItem(position=0, app_id="a", name="A", publisher="p", version="1.0.0.0", status="in_progress"),
            RunItem(position=1, app_id="b", name="B", publisher="p", version="1.0.0.0", status="queued"),
        ]
        db.add(run)
        db.commit()
        run_id = run.id
    assert app.state.deploy.recover_interrupted() == 1
    run = get_run(app, run_id)
    assert run.status == "interrupted"
    assert [i.status for i in run.items] == ["failed", "skipped"]


def test_deploy_page_renders(client, app):
    assert "Prima crea una" in client.get("/deploy").text
    conn = make_conn(client, app, prod=True, env="Production")
    page = client.get(f"/deploy?connection_id={conn}")
    assert page.status_code == 200
    assert "(PRODUZIONE)" in page.text and 'id="dropzone"' in page.text and "/static/deploy.js" in page.text
