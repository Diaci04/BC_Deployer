"""Ciclo di vita dei pacchetti caricati.

- caricati ma mai usati: eliminati dopo 24 ore;
- usati da una run manuale: conservati UPLOAD_RETENTION_DAYS giorni (per poter rilanciare);
- legati a un job attivo: conservati finché il job esiste;
- job singolo con delete_files_after: eliminati subito dopo un'esecuzione riuscita
  (se fallisce restano per i giorni di retention, così si può rilanciare).
"""
import logging
import time
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..models import AppFile, DeployJob, JobFile, Run, utcnow
from .uploads import delete_file

log = logging.getLogger(__name__)


def _files_in_active_jobs(db: Session) -> set[int]:
    rows = db.execute(
        select(JobFile.file_id).join(DeployJob, DeployJob.id == JobFile.job_id).where(DeployJob.enabled.is_(True))
    )
    return {r[0] for r in rows}


def after_run(factory: sessionmaker[Session], settings: Settings, run_id: int) -> None:
    with factory() as db:
        run = db.get(Run, run_id)
        if run is None:
            return
        protected = _files_in_active_jobs(db)
        job = db.get(DeployJob, run.job_id) if run.job_id else None
        delete_now = job is not None and job.kind == "once" and job.delete_files_after and run.status == "succeeded"
        expires = utcnow() + timedelta(days=settings.upload_retention_days)
        for it in run.items:
            f = it.file
            if f is None or f.status == "deleted" or f.id in protected:
                continue
            if delete_now:
                delete_file(db, settings, f)
            else:
                f.expires_at = expires
        db.commit()


def release_job_files(db: Session, settings: Settings, job: DeployJob) -> None:
    """Quando un job viene eliminato o termina: i suoi file seguono la retention normale."""
    protected = _files_in_active_jobs(db)
    expires = utcnow() + timedelta(days=settings.upload_retention_days)
    for jf in job.files:
        if jf.file.status != "deleted" and jf.file_id not in protected:
            jf.file.expires_at = expires


def cleanup(factory: sessionmaker[Session], settings: Settings) -> int:
    """Elimina i file scaduti e i residui di upload interrotti. Restituisce quanti file ha rimosso."""
    removed = 0
    with factory() as db:
        protected = _files_in_active_jobs(db)
        expired = db.scalars(
            select(AppFile).where(AppFile.status != "deleted", AppFile.expires_at < utcnow())
        ).all()
        for f in expired:
            if f.id in protected:
                f.expires_at = None
                continue
            delete_file(db, settings, f)
            removed += 1
        db.commit()
    cutoff = time.time() - 86400
    for part in settings.upload_dir.glob("*.part"):
        try:
            if part.stat().st_mtime < cutoff:
                part.unlink()
        except OSError:
            pass
    if removed:
        log.info("Pulizia: eliminati %d pacchetti scaduti", removed)
    return removed
