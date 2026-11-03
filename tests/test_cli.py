"""CLI bc_deploy.py, comandi di servizio e controlli di configurazione."""
import sqlite3
import sys
from pathlib import Path

import pytest
import respx
from pydantic import ValidationError

import bc_deploy
from app import cli
from app.config import Settings
from app.main import create_app

from .apps import APP_A, APP_B, write_app
from .fakebc import CLIENT, TENANT, FakeBC


@pytest.fixture
def apps_dir(tmp_path):
    d = tmp_path / "apps"
    d.mkdir()
    write_app(d / "a.app", app_id=APP_A, name="App A", deps=[(APP_B, "App B", "Contoso", "1.0.0.0")])
    write_app(d / "b.app", app_id=APP_B, name="App B")
    return d


def run_cli(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["bc_deploy.py", *args])
    return bc_deploy.main()


def test_cli_dry_run_prints_order(monkeypatch, apps_dir, capsys):
    assert run_cli(monkeypatch, "--environment", "Sandbox", "--apps", str(apps_dir), "--dry-run") == 0
    out = capsys.readouterr().out
    assert out.index("App B 1.0.0.0") < out.index("App A 1.0.0.0")


def test_cli_deploys_with_fake_bc(monkeypatch, apps_dir, capsys):
    monkeypatch.setenv("BC_TENANT_ID", TENANT)
    monkeypatch.setenv("BC_CLIENT_ID", CLIENT)
    monkeypatch.setenv("BC_CLIENT_SECRET", "s")
    monkeypatch.setattr(bc_deploy.time, "sleep", lambda s: None)
    with respx.mock(assert_all_called=False) as m:
        bc = FakeBC(m)
        code = run_cli(monkeypatch, "--environment", "Sandbox", "--apps", str(apps_dir), "--schedule", "Current Version")
    assert code == 0
    assert [c for c in bc.calls if c.startswith("install")] == ["install:App B", "install:App A"]
    assert "Deploy completato." in capsys.readouterr().out


def test_cli_stops_on_failure(monkeypatch, apps_dir, capsys):
    for k in ("BC_TENANT_ID", "BC_CLIENT_ID", "BC_CLIENT_SECRET"):
        monkeypatch.setenv(k, TENANT if k == "BC_TENANT_ID" else CLIENT)
    monkeypatch.setattr(bc_deploy.time, "sleep", lambda s: None)
    with respx.mock(assert_all_called=False) as m:
        bc = FakeBC(m, outcomes={"App B": "Failed"})
        assert run_cli(monkeypatch, "--environment", "Sandbox", "--apps", str(apps_dir)) == 1
    assert "install:App A" not in bc.calls
    assert "ERRORE: App B" in capsys.readouterr().out


def test_cli_missing_credentials(monkeypatch, apps_dir):
    for k in ("BC_TENANT_ID", "BC_CLIENT_ID", "BC_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    assert run_cli(monkeypatch, "--environment", "Sandbox", "--apps", str(apps_dir)) == 1


def test_backup_command(client, app, settings, monkeypatch):
    import app.config as config

    monkeypatch.setattr(config, "get_settings", lambda: settings)
    target = cli.backup(str(settings.data_dir / "copia.db"))
    with sqlite3.connect(target) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"connections", "runs", "deploy_jobs", "alembic_version"} <= tables


def _answers(monkeypatch, inputs, passwords=("password-lunga-123", "password-lunga-123")):
    it, pw = iter(inputs), iter(passwords)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(pw))


def test_setup_writes_env_from_example(tmp_path, monkeypatch):
    from dotenv import dotenv_values

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    example = Path(__file__).parents[1] / ".env.example"
    (tmp_path / ".env").write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    _answers(monkeypatch, [""])  # Invio: chiave nuova
    cli.setup()
    env = dotenv_values(tmp_path / ".env")
    assert cli.ARGON2_HASH.match(env["ADMIN_PASSWORD_HASH"])
    assert cli._key_ok(env["FERNET_KEYS"], [])
    assert env["APP_ENV"] == "development" and env["COOKIE_SECURE"] == "false"
    assert env["DATA_DIR"] == str(tmp_path / "local" / "bcdeployer-data")
    s = Settings(_env_file=tmp_path / ".env")
    assert s.data_dir == (tmp_path / "local" / "bcdeployer-data").resolve()


def test_setup_keeps_existing_config_and_requires_right_key(tmp_path, monkeypatch, settings):
    from dotenv import dotenv_values

    monkeypatch.chdir(tmp_path)
    data = tmp_path / "dati"
    data.mkdir()
    key = cli.SecretBox.generate_key()
    with sqlite3.connect(data / "bcdeployer.db") as db:
        db.execute("CREATE TABLE connections (client_secret_enc TEXT)")
        db.execute("INSERT INTO connections VALUES (?)", (cli.SecretBox(key).encrypt("s"),))
    other = cli.SecretBox.generate_key()
    (tmp_path / ".env").write_text(f"DATA_DIR={data}\nFERNET_KEYS={other}\nMAX_APP_SIZE_MB=50\n", encoding="utf-8")
    # chiave sbagliata, poi quella giusta; password nuova
    _answers(monkeypatch, [other, key])
    cli.setup()
    env = dotenv_values(tmp_path / ".env")
    assert env["FERNET_KEYS"] == key and env["MAX_APP_SIZE_MB"] == "50" and env["DATA_DIR"] == str(data)
    # seconda esecuzione: tutto valido, nessuna domanda tranne "cambiare password?"
    _answers(monkeypatch, ["n"], passwords=())
    cli.setup()
    assert dotenv_values(tmp_path / ".env") == env


def test_password_hash_must_be_argon2(settings):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, admin_password_hash="password-in-chiaro", fernet_keys=settings.fernet_keys)


def test_production_requires_secure_cookies(settings):
    with pytest.raises(RuntimeError, match="COOKIE_SECURE"):
        create_app(settings.model_copy(update={"app_env": "production", "cookie_secure": False}))
