"""Modelli del database."""
from datetime import UTC, datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, TypeDecorator
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """Salva in UTC e restituisce sempre datetime con fuso (SQLite li perde)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None:
            if value.tzinfo is None:
                raise ValueError("datetime senza fuso orario")
            value = value.astimezone(UTC)
            if dialect.name == "sqlite":
                value = value.replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value


class Connection(Base):
    __tablename__ = "connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    # Facoltativo: raggruppa gli environment dello stesso cliente
    customer: Mapped[str | None] = mapped_column(String(100))
    tenant_id: Mapped[str] = mapped_column(String(100))
    client_id: Mapped[str] = mapped_column(String(36))
    client_secret_enc: Mapped[str] = mapped_column(Text)
    environment: Mapped[str] = mapped_column(String(50))
    company: Mapped[str | None] = mapped_column(String(100))
    is_production: Mapped[bool] = mapped_column(Boolean, default=False)
    webhook_kind: Mapped[str | None] = mapped_column(String(20))
    webhook_url_enc: Mapped[str | None] = mapped_column(Text)
    notify_on: Mapped[str] = mapped_column(String(10), default="all", server_default="all")  # all | failure
    # Destinatari email separati da virgola (inviate col server SMTP delle Impostazioni)
    notify_emails: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    @property
    def email_list(self) -> list[str]:
        return [e for e in (self.notify_emails or "").split(",") if e]

    @property
    def has_notifications(self) -> bool:
        return bool(self.webhook_url_enc and self.webhook_kind) or bool(self.email_list)


class SmtpSettings(Base):
    """Server SMTP per le notifiche email: una sola riga (id=1). La password è cifrata come i client secret."""

    __tablename__ = "smtp_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer)
    security: Mapped[str] = mapped_column(String(10))  # starttls | ssl | none
    username: Mapped[str | None] = mapped_column(String(255))
    password_enc: Mapped[str | None] = mapped_column(Text)
    from_addr: Mapped[str] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class UserSession(Base):
    __tablename__ = "sessions"

    # sha256 del token nel cookie: un dump del DB non permette di impersonare l'utente
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(100))
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))


class LoginAttempt(Base):
    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ip: Mapped[str] = mapped_column(String(64), index=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    success: Mapped[bool] = mapped_column(Boolean)


class AppFile(Base):
    """Pacchetto .app caricato. Su disco si chiama <stored_name>, mai col nome originale."""

    __tablename__ = "app_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    original_name: Mapped[str] = mapped_column(String(200))
    stored_name: Mapped[str] = mapped_column(String(60), unique=True)
    size: Mapped[int] = mapped_column(BigInteger)
    app_id: Mapped[str] = mapped_column(String(36))
    name: Mapped[str] = mapped_column(String(200))
    publisher: Mapped[str] = mapped_column(String(200))
    version: Mapped[str] = mapped_column(String(30))
    manifest_json: Mapped[str] = mapped_column(Text)
    # staged = caricato e non ancora usato; bound = legato a run/job; deleted = rimosso dal disco
    status: Mapped[str] = mapped_column(String(20), default="staged", index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[int | None] = mapped_column(ForeignKey("connections.id", ondelete="SET NULL"), index=True)
    # Copie al momento della run: lo storico resta leggibile anche se la connessione cambia
    connection_name: Mapped[str] = mapped_column(String(100))
    environment: Mapped[str] = mapped_column(String(50))
    tenant_id: Mapped[str] = mapped_column(String(100))
    trigger: Mapped[str] = mapped_column(String(20))  # manual | scheduled | rerun
    parent_run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    job_id: Mapped[int | None] = mapped_column(ForeignKey("deploy_jobs.id", ondelete="SET NULL"), index=True)
    triggered_by: Mapped[str] = mapped_column(String(100))
    client_ip: Mapped[str | None] = mapped_column(String(64))
    schema_sync: Mapped[str] = mapped_column(String(20))
    schedule: Mapped[str] = mapped_column(String(30))
    # queued | running | succeeded | failed | canceled | interrupted
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    items: Mapped[list["RunItem"]] = relationship(
        back_populates="run", order_by="RunItem.position", cascade="all, delete-orphan"
    )


class RunItem(Base):
    __tablename__ = "run_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    file_id: Mapped[int | None] = mapped_column(ForeignKey("app_files.id", ondelete="SET NULL"))
    app_id: Mapped[str] = mapped_column(String(36))
    name: Mapped[str] = mapped_column(String(200))
    publisher: Mapped[str] = mapped_column(String(200))
    version: Mapped[str] = mapped_column(String(30))
    # queued | uploading | in_progress | succeeded | scheduled | already_installed | failed | skipped
    status: Mapped[str] = mapped_column(String(20), default="queued")
    installed_version: Mapped[str | None] = mapped_column(String(30))
    operation_id: Mapped[str | None] = mapped_column(String(60))
    bc_status: Mapped[str | None] = mapped_column(String(40))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    run: Mapped[Run] = relationship(back_populates="items")
    file: Mapped[AppFile | None] = relationship()


class RunLog(Base):
    __tablename__ = "run_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("run_items.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    level: Mapped[str] = mapped_column(String(10), default="info")
    message: Mapped[str] = mapped_column(Text)


class DeployJob(Base):
    """Deploy programmato: una volta (run_at) o ricorrente (cron, nel fuso indicato)."""

    __tablename__ = "deploy_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id"), index=True)
    kind: Mapped[str] = mapped_column(String(10))  # once | cron
    run_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cron_expr: Mapped[str | None] = mapped_column(String(100))
    timezone: Mapped[str] = mapped_column(String(50))
    schema_sync: Mapped[str] = mapped_column(String(20))
    schedule: Mapped[str] = mapped_column(String(30))
    # Solo per i job singoli: elimina i .app dopo un'esecuzione riuscita
    delete_files_after: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Messaggio sull'ultima attivazione (es. esecuzione saltata perché il servizio era spento)
    last_message: Mapped[str | None] = mapped_column(Text)
    last_run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL", use_alter=True))
    created_by: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    files: Mapped[list["JobFile"]] = relationship(
        order_by="JobFile.position", cascade="all, delete-orphan", back_populates="job"
    )
    connection: Mapped[Connection] = relationship()


class JobFile(Base):
    __tablename__ = "job_files"

    job_id: Mapped[int] = mapped_column(ForeignKey("deploy_jobs.id", ondelete="CASCADE"), primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("app_files.id"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer)

    job: Mapped[DeployJob] = relationship(back_populates="files")
    file: Mapped[AppFile] = relationship()
