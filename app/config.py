"""Configurazione dell'applicazione: tutto da variabili d'ambiente (o file .env)."""
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # "production" attiva HTTPS obbligatorio e HSTS
    app_env: str = Field("production", pattern="^(production|development)$")

    data_dir: Path = Path("./data")
    # Default: SQLite nel volume dati. Per Postgres:
    # postgresql+psycopg://user:pass@host:5432/bcdeployer
    database_url: str = ""
    # SQLite: WAL è il più veloce ma non funziona su file system di rete (Azure Files / /home di App Service):
    # lì usa DELETE
    sqlite_journal_mode: str = Field("WAL", pattern="^(WAL|DELETE|TRUNCATE)$")
    # URL pubblico dell'app, usato per i link nelle notifiche (es. https://bcdeployer.azurewebsites.net)
    public_base_url: str = ""

    # Login: un solo utente, password come hash argon2 (python -m app.cli hash-password)
    admin_username: str = "admin"
    admin_password_hash: str = Field(pattern=r"^\$argon2(id|i|d)\$")
    session_idle_minutes: int = 60
    session_max_hours: int = 12
    login_max_attempts: int = 5
    login_window_minutes: int = 15
    cookie_secure: bool = True

    # Chiavi Fernet separate da virgola: la prima cifra, tutte decifrano (rotazione)
    fernet_keys: str

    max_app_size_mb: int = 100
    upload_retention_days: int = 7
    timezone: str = "Europe/Rome"

    # Timeout HTTP verso BC (secondi)
    bc_timeout: float = 60
    bc_upload_timeout: float = 900
    # Tempo massimo di attesa dell'esito di installazione per ciascuna app
    app_timeout_minutes: int = 60
    # Se il servizio era spento all'ora prevista, il job parte comunque entro questo margine
    scheduler_misfire_grace_minutes: int = 60

    @field_validator("data_dir")
    @classmethod
    def _abs(cls, v: Path) -> Path:
        return v.resolve()

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{(self.data_dir / 'bcdeployer.db').as_posix()}"

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def session_cookie(self) -> str:
        # Il prefisso __Host- obbliga Secure, Path=/ e nessun Domain
        return "__Host-bcd_session" if self.cookie_secure else "bcd_session"

    @property
    def login_csrf_cookie(self) -> str:
        return "__Host-bcd_login" if self.cookie_secure else "bcd_login"


@lru_cache
def get_settings() -> Settings:
    return Settings()
