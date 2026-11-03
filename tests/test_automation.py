"""Client Automation API con chiamate mockate: forma esatta delle richieste e lettura degli esiti."""
from datetime import UTC, datetime, timedelta

import pytest
import respx

from app.bc.automation import AutomationBackend, parse_bc_datetime
from app.bc.base import DeployState, Schedule, SchemaSync, UploadHandle
from app.bc.http import BCError, BCHttp
from app.bc.manifest import read_manifest

from .apps import APP_A, write_app
from .fakebc import CLIENT, COMPANY_ID, TENANT, FakeBC, api_base


@pytest.fixture
def backend():
    http = BCHttp(TENANT, CLIENT, "secret", "Sandbox", sleep=lambda s: None, max_retries=2)
    yield AutomationBackend(http, "CRONUS IT")
    http.close()


def test_upload_sequence_matches_microsoft_docs(backend, tmp_path):
    p = write_app(tmp_path / "a.app", app_id=APP_A, name="App A", version="1.2.3.4")
    with respx.mock(assert_all_called=False) as m:
        bc = FakeBC(m)
        handle = backend.upload_and_install(read_manifest(p), p, SchemaSync.FORCE, Schedule.CURRENT)
    assert bc.calls == ["create", "content", "install:App A"]
    assert bc.uploads[handle.upload_id]["body"] == {"schedule": "Current version", "schemaSyncMode": "Force Sync"}
    assert bc.uploads[handle.upload_id]["content"] == p.read_bytes()


def test_existing_upload_record_is_reused(backend, tmp_path):
    # Errore visto su BC reale: POST con record già presente → 400 Internal_EntityWithSameKeyExists (Id='0')
    p = write_app(tmp_path / "a.app", app_id=APP_A, name="App A", version="1.2.3.4")
    with respx.mock(assert_all_called=False) as m:
        bc = FakeBC(m)
        uid = bc.add_existing_upload()
        handle = backend.upload_and_install(read_manifest(p), p, SchemaSync.ADD, Schedule.NEXT_MINOR)
    assert handle.upload_id == uid
    assert bc.calls == ["update", "content", "install:App A"]
    assert bc.uploads[uid]["body"] == {"schedule": "Next minor version", "schemaSyncMode": "Add"}
    assert bc.uploads[uid]["content"] == p.read_bytes()


def test_deploy_status_matches_name_version_and_time(backend, tmp_path):
    p = write_app(tmp_path / "a.app", app_id=APP_A, name="App A", version="1.2.3.4")
    manifest = read_manifest(p)
    now = datetime.now(UTC)
    ops = [
        # tentativo vecchio della stessa versione: va ignorato
        {"operationID": "old", "name": "App A", "appVersion": "1.2.3.4", "status": "Failed",
         "startedOn": (now - timedelta(days=1)).isoformat()},
        {"operationID": "other", "name": "App A", "appVersion": "1.0.0.0", "status": "Completed",
         "startedOn": now.isoformat()},
        {"operationID": "new", "name": "app a", "publisher": "Contoso", "appVersion": "1.2.3.4",
         "status": "Completed", "startedOn": now.strftime("%Y-%m-%dT%H:%M:%S.1234567Z")},
    ]
    with respx.mock(assert_all_called=False) as m:
        FakeBC(m)
        m.get(f"{api_base()}/companies({COMPANY_ID})/extensionDeploymentStatus").respond(json={"value": ops})
        st = backend.deploy_status(UploadHandle(manifest, now))
        assert (st.state, st.operation_id, st.raw_status) == (DeployState.COMPLETED, "new", "Completed")
        m.get(f"{api_base()}/companies({COMPANY_ID})/extensionDeploymentStatus").respond(json={"value": ops[:2]})
        assert backend.deploy_status(UploadHandle(manifest, now)).state == DeployState.PENDING


def test_list_apps_maps_versions(backend):
    with respx.mock(assert_all_called=False) as m:
        FakeBC(m, installed=[{"id": APP_A.upper(), "name": "App A", "version": "1.10.0.2", "isInstalled": False}])
        apps = backend.list_apps()
    assert apps[0].id == APP_A and str(apps[0].version) == "1.10.0.2" and not apps[0].is_installed


def test_install_trigger_is_not_retried_on_server_error(backend, tmp_path):
    """Microsoft.NAV.upload non è idempotente: un 500 non deve avviare una seconda installazione."""
    p = write_app(tmp_path / "a.app", app_id=APP_A, name="App A")
    with respx.mock(assert_all_called=False) as m:
        bc = FakeBC(m)
        bc.install_error = 500
        with pytest.raises(BCError, match="boom"):
            backend.upload_and_install(read_manifest(p), p, SchemaSync.ADD, Schedule.CURRENT)
        assert bc.install_attempts == 1


def test_upload_without_id_in_response_is_an_error(backend, tmp_path):
    p = write_app(tmp_path / "a.app", app_id=APP_A, name="App A")
    with respx.mock(assert_all_called=False) as m:
        FakeBC(m)
        m.post(f"{api_base()}/companies({COMPANY_ID})/extensionUpload").respond(201, json={})
        with pytest.raises(BCError, match="id del record"):
            backend.upload_and_install(read_manifest(p), p, SchemaSync.ADD, Schedule.CURRENT)


def test_company_not_found(tmp_path):
    with BCHttp(TENANT, CLIENT, "s", "Sandbox", sleep=lambda s: None) as http, respx.mock(assert_all_called=False) as m:
        FakeBC(m)
        with pytest.raises(BCError, match="non trovata"):
            AutomationBackend(http, "Altra").company_id


def test_parse_bc_datetime():
    assert parse_bc_datetime("2026-11-02T10:00:00.1234567Z") == datetime(2026, 11, 2, 10, 0, 0, 123456, UTC)
    assert parse_bc_datetime("2026-11-02T10:00:00") == datetime(2026, 11, 2, 10, tzinfo=UTC)
    assert parse_bc_datetime("") is None and parse_bc_datetime("boh") is None


def test_recent_failed_attempt_is_not_mistaken_for_new_one(backend, tmp_path):
    """Rilancio subito dopo un errore: l'operazione fallita di prima non è quella nuova."""
    p = write_app(tmp_path / "a.app", app_id=APP_A, name="App A", version="1.0.0.0")
    with respx.mock(assert_all_called=False) as m:
        bc = FakeBC(m, outcomes={"App A": "Failed"})
        first = backend.upload_and_install(read_manifest(p), p, SchemaSync.ADD, Schedule.CURRENT)
        while backend.deploy_status(first).state == DeployState.PENDING:
            pass
        bc.outcomes.clear()
        second = backend.upload_and_install(read_manifest(p), p, SchemaSync.ADD, Schedule.CURRENT)
        assert backend.deploy_status(second).state == DeployState.PENDING  # non "Failed" del tentativo prima
        assert backend.deploy_status(second).state == DeployState.COMPLETED
