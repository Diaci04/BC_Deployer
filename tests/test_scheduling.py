"""Deploy programmati, storico con filtri e ciclo di vita dei file."""
import os
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models import AppFile, DeployJob, Run, RunItem, utcnow
from app.services.retention import cleanup

from .apps import APP_A, APP_B  # noqa: F401
from .fakebc import FakeBC
from .test_deploy_flow import A, B, get_run, make_conn, upload_ok

ROME = ZoneInfo("Europe/Rome")


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


def future(hours=2) -> str:
    return (datetime.now(ROME) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")


def schedule(client, conn, ids, **extra):
    data = {"connection_id": conn, "file_ids": ids, "schema_sync": "Add", "schedule": "Current version", **extra}
    return client.post("/deploy", data=data)


def job_of(app, job_id) -> DeployJob:
    with app.state.sessionmaker() as db:
        job = db.get(DeployJob, job_id)
        job.files, job.connection  # carica le relazioni
        for jf in job.files:
            jf.file
        db.expunge_all()
        return job


def created_job_id(r) -> int:
    assert r.status_code == 204, r.text
    return int(r.headers["hx-redirect"].split("/jobs/")[1].split("?")[0])


# ---------------------------------------------------------------- creazione
def test_one_shot_job_is_scheduled_in_rome_time(client, app):
    conn = make_conn(client, app)
    ids = [upload_ok(client, **A), upload_ok(client, **B)]
    when = future()
    job_id = created_job_id(schedule(client, conn, ids, when="once", run_at=when, delete_files_after="true"))
    job = job_of(app, job_id)
    assert job.kind == "once" and job.enabled and job.delete_files_after
    expected = datetime.strptime(when, "%Y-%m-%dT%H:%M").replace(tzinfo=ROME)
    assert app.state.scheduler.next_run_time(job_id) == expected
    assert {jf.file.status for jf in job.files} == {"bound"}
    assert all(jf.file.expires_at is None for jf in job.files)
    page = client.get(f"/jobs/{job_id}")
    assert page.status_code == 200 and "App A" in page.text and "una volta" in page.text


def test_one_shot_job_in_the_past_is_rejected(client, app):
    conn = make_conn(client, app)
    r = schedule(client, conn, [upload_ok(client, **B)], when="once", run_at=future(-1))
    assert r.status_code == 200 and "futuro" in r.text


def test_cron_job_validation_and_preview(client, app):
    conn = make_conn(client, app)
    ids = [upload_ok(client, **B)]
    assert "cron non valida" in schedule(client, conn, ids, when="cron", cron_expr="ogni sabato").text
    assert "5 campi" in schedule(client, conn, ids, when="cron", cron_expr="0 2 * *").text
    job_id = created_job_id(schedule(client, conn, ids, when="cron", cron_expr="0  2 * * sat", job_name="Settimanale"))
    job = job_of(app, job_id)
    assert job.name == "Settimanale" and job.cron_expr == "0 2 * * sat" and not job.delete_files_after
    nxt = app.state.scheduler.next_run_time(job_id)
    assert nxt.astimezone(ROME).weekday() == 5 and nxt.astimezone(ROME).hour == 2

    preview = client.get("/jobs/cron-preview", params={"cron_expr": "30 6 * * 1-5"})
    assert "Prossime esecuzioni" in preview.text and "06:30" in preview.text
    assert "error" in client.get("/jobs/cron-preview", params={"cron_expr": "99 * * * *"}).text


def test_jobs_survive_restart(client, app, settings):
    conn = make_conn(client, app)
    job_id = created_job_id(schedule(client, conn, [upload_ok(client, **B)], when="cron", cron_expr="0 3 * * *"))
    before = app.state.scheduler.next_run_time(job_id)
    app.state.scheduler.shutdown()

    app2 = create_app(settings)
    with TestClient(app2, base_url="https://testserver"):
        assert app2.state.scheduler.next_run_time(job_id) == before


# ---------------------------------------------------------------- esecuzione
def test_fired_one_shot_job_runs_and_deletes_files_on_success(client, app, bc, settings):
    conn = make_conn(client, app)
    FakeBC(bc)
    job_id = created_job_id(
        schedule(client, conn, [upload_ok(client, **B)], when="once", run_at=future(), delete_files_after="true")
    )
    run_id = app.state.scheduler.fire(job_id)
    run = get_run(app, run_id)
    assert run.status == "succeeded" and run.trigger == "scheduled" and run.job_id == job_id
    job = job_of(app, job_id)
    assert not job.enabled and job.last_run_id == run_id
    assert job.files[0].file.status == "deleted"
    assert list(settings.upload_dir.glob("*.app")) == []


def test_failed_one_shot_job_keeps_files_for_rerun(client, app, bc, settings):
    conn = make_conn(client, app)
    FakeBC(bc, outcomes={"App B": "Failed"})
    job_id = created_job_id(
        schedule(client, conn, [upload_ok(client, **B)], when="once", run_at=future(), delete_files_after="true")
    )
    run_id = app.state.scheduler.fire(job_id)
    assert get_run(app, run_id).status == "failed"
    f = job_of(app, job_id).files[0].file
    assert f.status == "bound" and f.expires_at > utcnow() + timedelta(days=settings.upload_retention_days - 1)
    assert 'hx-post="/runs/%d/rerun"' % run_id in client.get(f"/runs/{run_id}").text


def test_cron_job_keeps_files_and_stays_enabled(client, app, bc):
    conn = make_conn(client, app)
    FakeBC(bc)
    job_id = created_job_id(schedule(client, conn, [upload_ok(client, **B)], when="cron", cron_expr="0 3 * * *"))
    app.state.scheduler.fire(job_id)
    job = job_of(app, job_id)
    assert job.enabled and job.files[0].file.status == "bound" and job.files[0].file.expires_at is None


def test_job_with_missing_files_records_failed_run(client, app):
    conn = make_conn(client, app)
    job_id = created_job_id(schedule(client, conn, [upload_ok(client, **B)], when="cron", cron_expr="0 3 * * *"))
    with app.state.sessionmaker() as db:
        for f in db.scalars(select(AppFile)):
            f.status = "deleted"
        db.commit()
    run = get_run(app, app.state.scheduler.fire(job_id))
    assert run.status == "failed" and "eliminati" in run.error
    assert "non avviata" in job_of(app, job_id).last_message


def test_pause_resume_run_now_delete(client, app, bc):
    conn = make_conn(client, app, prod=True, env="Production")
    FakeBC(bc, env="Production")
    job_id = created_job_id(
        schedule(client, conn, [upload_ok(client, **B)], when="cron", cron_expr="0 3 * * *", confirm_env="Production")
    )
    sched = app.state.scheduler
    assert client.post(f"/jobs/{job_id}/pause").status_code == 204
    assert not job_of(app, job_id).enabled and sched.next_run_time(job_id) is None
    assert client.post(f"/jobs/{job_id}/resume").status_code == 204
    assert job_of(app, job_id).enabled and sched.next_run_time(job_id) is not None

    assert client.post(f"/jobs/{job_id}/run-now").status_code == 400  # produzione senza conferma
    r = client.post(f"/jobs/{job_id}/run-now", data={"confirm_env": "Production"})
    assert r.status_code == 204 and get_run(app).status == "succeeded"

    # Connessione con job: non eliminabile
    assert client.post(f"/connections/{conn}/delete").status_code == 409

    assert client.post(f"/jobs/{job_id}/delete").status_code == 204
    assert sched.next_run_time(job_id) is None
    with app.state.sessionmaker() as db:
        assert db.get(DeployJob, job_id) is None
        f = db.scalars(select(AppFile)).one()
        assert f.expires_at is not None  # torna alla retention normale
    assert client.post(f"/connections/{conn}/delete").status_code == 204


def test_production_job_requires_confirmation(client, app):
    conn = make_conn(client, app, prod=True, env="Production")
    r = schedule(client, conn, [upload_ok(client, **B)], when="cron", cron_expr="0 3 * * *")
    assert "digita esattamente" in r.text


def test_missed_execution_is_recorded(client, app):
    conn = make_conn(client, app)
    job_id = created_job_id(schedule(client, conn, [upload_ok(client, **B)], when="once", run_at=future()))
    event = SimpleNamespace(job_id=f"deploy-job-{job_id}", scheduled_run_time=datetime.now(ROME))
    app.state.scheduler._on_missed(event)
    job = job_of(app, job_id)
    assert "saltata" in job.last_message and not job.enabled


# ---------------------------------------------------------------- storico
def _run(db, conn_name, status, trigger="manual", apps=("App A",), created=None):
    r = Run(connection_name=conn_name, environment="Sandbox", tenant_id="t", trigger=trigger, triggered_by="admin",
            schema_sync="Add", schedule="Current version", status=status, created_at=created or utcnow())
    r.items = [RunItem(position=i, app_id=APP_A if i == 0 else APP_B, name=n, publisher="p", version="1.0.0.0")
               for i, n in enumerate(apps)]
    db.add(r)
    return r


def test_history_filters(client, app):
    with app.state.sessionmaker() as db:
        _run(db, "Uno", "succeeded", apps=("Vendite",))
        _run(db, "Due", "failed", trigger="scheduled", apps=("Acquisti",))
        _run(db, "Tre", "failed", apps=("Magazzino",), created=utcnow() - timedelta(days=10))
        db.commit()

    def shown(**params):
        text = client.get("/runs", params=params, headers={"hx-request": "true"}).text
        return {n for n in ("Uno", "Due", "Tre") if f"{n} · Sandbox" in text}

    assert shown() == {"Uno", "Due", "Tre"}
    assert shown(status="failed") == {"Due", "Tre"}
    assert shown(trigger="scheduled") == {"Due"}
    assert shown(app="vend") == {"Uno"}
    today = datetime.now(ROME).date()
    assert shown(date_from=(today - timedelta(days=1)).isoformat()) == {"Uno", "Due"}
    assert shown(date_to=(today - timedelta(days=5)).isoformat()) == {"Tre"}
    assert shown(date_from="non-una-data") == {"Uno", "Due", "Tre"}

    full = client.get("/runs?status=failed")
    assert "<h1>Storico esecuzioni</h1>" in full.text and 'value="failed" selected' in full.text


def test_history_pagination(client, app):
    with app.state.sessionmaker() as db:
        for _ in range(55):
            _run(db, "X", "succeeded")
        db.commit()
    p1 = client.get("/runs", params={"status": "succeeded"}).text
    assert "Meno recenti" in p1 and "status=succeeded&page=2" in p1
    p2 = client.get("/runs", params={"status": "succeeded", "page": 2}).text
    assert p2.count('class="card row-link"') == 5


# ---------------------------------------------------------------- retention
def test_manual_run_files_get_retention(client, app, bc, settings):
    conn = make_conn(client, app)
    FakeBC(bc)
    client.post("/deploy", data={"connection_id": conn, "file_ids": [upload_ok(client, **B)]})
    with app.state.sessionmaker() as db:
        f = db.scalars(select(AppFile)).one()
        assert f.status == "bound"
        days = (f.expires_at - utcnow()).total_seconds() / 86400
        assert settings.upload_retention_days - 0.1 < days <= settings.upload_retention_days


def test_cleanup_removes_expired_and_keeps_protected(client, app, settings):
    conn = make_conn(client, app)
    old = upload_ok(client, **A)
    kept = upload_ok(client, **B)
    created_job_id(schedule(client, conn, [kept], when="cron", cron_expr="0 3 * * *"))
    stale_part = settings.upload_dir / "abandoned.app.part"
    stale_part.write_bytes(b"x")
    os.utime(stale_part, (time.time() - 2 * 86400,) * 2)
    with app.state.sessionmaker() as db:
        for f in db.scalars(select(AppFile)):
            f.expires_at = utcnow() - timedelta(minutes=1)  # anche quello del job: deve restare
        db.commit()
    assert cleanup(app.state.sessionmaker, settings) == 1
    with app.state.sessionmaker() as db:
        assert db.get(AppFile, old).status == "deleted"
        assert db.get(AppFile, kept).status == "bound" and db.get(AppFile, kept).expires_at is None
    assert not stale_part.exists()


def test_health_reports_scheduler(client):
    assert client.get("/health").json() == {"status": "ok", "database": "ok", "scheduler": "ok"}


def test_pages_render(client, app):
    for path in ("/", "/runs", "/jobs"):
        assert client.get(path).status_code == 200

