"""Comandi di servizio.

  python -m app.cli setup           # configurazione guidata per l'uso in locale: scrive password e chiave in .env
  python -m app.cli hash-password   # genera ADMIN_PASSWORD_HASH
  python -m app.cli gen-key         # genera una chiave per FERNET_KEYS
  python -m app.cli rotate-keys     # ricifra i segreti con la prima chiave di FERNET_KEYS
  python -m app.cli backup [file]   # copia coerente del DB SQLite, anche con l'app in esecuzione
"""
import getpass
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

from .security.crypto import SecretBox, SecretBoxError

ARGON2_HASH = re.compile(r"^\$argon2(id|i|d)\$v=\d+\$m=\d+,t=\d+,p=\d+\$[A-Za-z0-9+/]+\$[A-Za-z0-9+/]+$")


def _ask_password() -> str:
    from .security.auth import hash_password as _hash

    pw = getpass.getpass("Password admin: ")
    if len(pw) < 12:
        sys.exit("La password deve avere almeno 12 caratteri")
    if pw != getpass.getpass("Ripeti: "):
        sys.exit("Le password non coincidono")
    return _hash(pw)


def hash_password() -> None:
    print(_ask_password())


def _saved_secrets(db: Path) -> list[str]:
    """Client secret cifrati già salvati, per verificare che la chiave sia quella giusta."""
    if not db.is_file():
        return []
    try:
        with sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True) as conn:
            return [r[0] for r in conn.execute("SELECT client_secret_enc FROM connections")]
    except sqlite3.Error:
        return []


def _key_ok(key: str, secrets: list[str]) -> bool:
    try:
        box = SecretBox(key)
        if secrets:
            box.decrypt(secrets[0])
    except SecretBoxError:
        return False
    return True


def _ask_key(secrets: list[str]) -> str:
    while True:
        if secrets:
            print(f"Ci sono {len(secrets)} connessioni salvate: serve la chiave usata finora.")
            key = input("Incolla la chiave (FERNET_KEYS) e premi Invio: ").strip()
        else:
            key = input("Se hai già una chiave incollala, altrimenti premi Invio per crearne una nuova: ").strip()
        if not key:
            if secrets and input(
                "Con una chiave nuova le connessioni salvate vanno reinserite. Continuare? [s/N] "
            ).strip().lower() != "s":
                continue
            print("Creata una chiave nuova. Tienine una copia nel password manager.")
            return SecretBox.generate_key()
        if _key_ok(key, secrets):
            return key
        print("Chiave non valida, oppure non è quella usata per le connessioni salvate. Riprova.")


def _write_env(path: Path, values: dict[str, str]) -> None:
    lines = ["# Creato da Installa.bat (python -m app.cli setup). Contiene password e chiave: non passarlo a nessuno."]
    for k, v in values.items():
        lines.append(f"{k}='{v}'" if "'" not in v else f'{k}="{v}"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def setup() -> None:
    """Configurazione per l'uso in locale: tutto in .env, così le volte successive basta avviare."""
    from dotenv import dotenv_values

    env = Path(".env")
    values = {k: v for k, v in (dotenv_values(env) if env.is_file() else {}).items() if v}
    default_dir = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "bcdeployer-data"
    data_dir = values.get("DATA_DIR", "")
    if data_dir in ("", "/data"):  # "/data" è il valore per il container in .env.example
        data_dir = str(default_dir)
    secrets = [] if values.get("DATABASE_URL") else _saved_secrets(Path(data_dir) / "bcdeployer.db")

    key = values.get("FERNET_KEYS", "")
    if not _key_ok(key, secrets):
        if key:
            print("La chiave in .env non è valida o non apre le connessioni salvate.")
        key = _ask_key(secrets)

    pw_hash = values.get("ADMIN_PASSWORD_HASH", "")
    if not ARGON2_HASH.match(pw_hash):
        print("Scegli la password per entrare nell'app (almeno 12 caratteri).")
        pw_hash = _ask_password()
    elif input("Password già impostata. Vuoi cambiarla? [s/N] ").strip().lower() == "s":
        pw_hash = _ask_password()

    managed = {
        "ADMIN_PASSWORD_HASH": pw_hash,
        "FERNET_KEYS": key,
        "APP_ENV": "development",  # in locale si usa http://localhost: niente redirect HTTPS
        "COOKIE_SECURE": "false",
        "DATA_DIR": data_dir,
    }
    _write_env(env, managed | {k: v for k, v in values.items() if k not in managed})
    print(f"Configurazione salvata in {env.resolve()}")
    print(f"Database e pacchetti in {data_dir}")


def rotate_keys() -> None:
    from sqlalchemy import select

    from .config import get_settings
    from .db import make_engine, make_sessionmaker, session_scope
    from .models import Connection

    settings = get_settings()
    box = SecretBox(settings.fernet_keys)
    factory = make_sessionmaker(make_engine(settings.db_url, settings.sqlite_journal_mode))
    with session_scope(factory) as db:
        n = 0
        for conn in db.scalars(select(Connection)):
            conn.client_secret_enc = box.rotate(conn.client_secret_enc)
            if conn.webhook_url_enc:
                conn.webhook_url_enc = box.rotate(conn.webhook_url_enc)
            n += 1
    print(f"Ricifrate {n} connessioni con la chiave corrente")


def backup(dest: str | None = None) -> Path:
    from sqlalchemy.engine import make_url

    from .config import get_settings

    settings = get_settings()
    url = make_url(settings.db_url)
    if url.get_backend_name() != "sqlite":
        sys.exit("DATABASE_URL non è SQLite: usa pg_dump (vedi docs/deploy-azure.md)")
    target = Path(dest) if dest else settings.data_dir / "backups" / f"bcdeployer-{time.strftime('%Y%m%d-%H%M%S')}.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(url.database)
    dst = sqlite3.connect(target)
    try:
        with dst:
            src.backup(dst)  # API di backup online: copia coerente anche con scritture in corso
    finally:
        src.close()
        dst.close()
    print(f"Backup scritto in {target}")
    return target


def main() -> None:
    cmds = {
        "setup": setup,
        "hash-password": hash_password,
        "gen-key": lambda: print(SecretBox.generate_key()),
        "rotate-keys": rotate_keys,
        "backup": backup,
    }
    if len(sys.argv) < 2 or sys.argv[1] not in cmds or (len(sys.argv) > 2 and sys.argv[1] != "backup"):
        sys.exit(__doc__)
    cmds[sys.argv[1]](*sys.argv[2:3])


if __name__ == "__main__":
    main()
