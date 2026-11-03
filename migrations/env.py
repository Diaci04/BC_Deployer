"""Ambiente Alembic. L'app passa la connessione in config.attributes; da CLI usa DATABASE_URL."""
from alembic import context

from app import models  # noqa: F401  (registra le tabelle)
from app.db import Base, make_engine

target_metadata = Base.metadata


def run() -> None:
    connection = context.config.attributes.get("connection")
    if connection is not None:
        _configure_and_run(connection)
        return
    from app.config import get_settings

    settings = get_settings()
    engine = make_engine(settings.db_url, settings.sqlite_journal_mode)
    with engine.begin() as conn:
        _configure_and_run(conn)


def _include_object(obj, name, type_, reflected, compare_to) -> bool:
    # La tabella dei job di APScheduler è gestita da APScheduler stesso
    return not (type_ == "table" and name == "apscheduler_jobs")


def _configure_and_run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=_include_object,
        render_as_batch=connection.dialect.name == "sqlite",  # ALTER TABLE su SQLite
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


run()
