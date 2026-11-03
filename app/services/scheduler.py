"""Deploy programmati con APScheduler, con i job salvati nel DB (sopravvivono ai riavvii).

APScheduler gira dentro il processo web: va avviato un solo processo (uvicorn --workers 1),
altrimenti ogni processo eseguirebbe gli stessi job.
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.events import EVENT_JOB_MISSED, JobExecutionEvent
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..bc.base import Schedule, SchemaSync
from ..bc.ordering import OrderingError
from ..config import Settings
from ..models import DeployJob, Run, utcnow
from .deployer import DeployService, RunError, create_run
from .retention import cleanup, release_job_files

log = logging.getLogger(__name__)

# APScheduler salva nel DB un riferimento alla funzione: deve essere a livello di modulo
_active: "JobScheduler | None" = None


def fire_job(job_id: int) -> None:
    if _active is not None:
        _active.fire(job_id)


def run_cleanup() -> None:
    if _active is not None:
        cleanup(_active.factory, _active.settings)


def aps_id(job_id: int) -> str:
    return f"deploy-job-{job_id}"


def parse_cron(expr: str, tz: str) -> CronTrigger:
    """Cron a 5 campi (minuto ora giorno mese giorno-settimana). Solleva ValueError se non valido."""
    expr = " ".join(expr.split())
    if len(expr.split(" ")) != 5:
        raise ValueError("Servono 5 campi: minuto ora giorno mese giorno-della-settimana")
    return CronTrigger.from_crontab(expr, timezone=ZoneInfo(tz))


def next_fire_times(trigger, count: int = 3) -> list[datetime]:
    times, prev, now = [], None, datetime.now(trigger.timezone)
    for _ in range(count):
        nxt = trigger.get_next_fire_time(prev, prev or now)
        if nxt is None:
            break
        times.append(nxt)
        prev = nxt
    return times


class JobScheduler:
    def __init__(self, settings: Settings, engine: Engine, factory: sessionmaker[Session], deploy: DeployService):
        self.settings = settings
        self.factory = factory
        self.deploy = deploy
        self.tz = ZoneInfo(settings.timezone)
        self.scheduler = BackgroundScheduler(
            jobstores={"default": SQLAlchemyJobStore(engine=engine, tablename="apscheduler_jobs")},
            timezone=self.tz,
            job_defaults={
                "coalesce": True,  # dopo un fermo, più esecuzioni perse diventano una sola
                "max_instances": 1,
                "misfire_grace_time": settings.scheduler_misfire_grace_minutes * 60,
            },
        )
        self.scheduler.add_listener(self._on_missed, EVENT_JOB_MISSED)

    @property
    def running(self) -> bool:
        return self.scheduler.running

    def start(self) -> None:
        global _active
        _active = self
        self.scheduler.start()
        self.scheduler.add_job(
            run_cleanup, "interval", hours=1, id="maintenance-cleanup", replace_existing=True,
            next_run_time=datetime.now(self.tz),
        )

    def shutdown(self) -> None:
        global _active
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        if _active is self:
            _active = None

    # -- gestione job
    def trigger_for(self, job: DeployJob):
        if job.kind == "once":
            return DateTrigger(run_date=job.run_at, timezone=ZoneInfo(job.timezone))
        return parse_cron(job.cron_expr, job.timezone)

    def schedule(self, job: DeployJob) -> None:
        self.scheduler.add_job(
            fire_job, self.trigger_for(job), args=[job.id], id=aps_id(job.id), replace_existing=True,
            name=job.name,
        )

    def unschedule(self, job_id: int) -> None:
        if self.scheduler.get_job(aps_id(job_id)):
            self.scheduler.remove_job(aps_id(job_id))

    def pause(self, job_id: int) -> None:
        if self.scheduler.get_job(aps_id(job_id)):
            self.scheduler.pause_job(aps_id(job_id))

    def resume(self, job: DeployJob) -> None:
        if self.scheduler.get_job(aps_id(job.id)):
            self.scheduler.resume_job(aps_id(job.id))
        else:
            self.schedule(job)

    def next_run_time(self, job_id: int) -> datetime | None:
        j = self.scheduler.get_job(aps_id(job_id))
        return j.next_run_time if j else None

    # -- esecuzione
    def fire(self, job_id: int, *, manual: bool = False) -> int | None:
        """Crea ed esegue la run di un job. Restituisce l'id della run (None se il job non esiste)."""
        with self.factory() as db:
            job = db.get(DeployJob, job_id)
            if job is None or (not job.enabled and not manual):
                return None
            conn = job.connection
            files = [jf.file for jf in job.files]
            try:
                if any(f.status == "deleted" for f in files):
                    raise RunError("I pacchetti del job sono stati eliminati dal server")
                run = create_run(
                    db, conn, files, schema_sync=SchemaSync(job.schema_sync), schedule=Schedule(job.schedule),
                    trigger="scheduled", triggered_by=f"job #{job.id} ({job.name})",
                )
            except (RunError, OrderingError) as e:
                db.rollback()
                job = db.get(DeployJob, job_id)
                run = Run(
                    connection_id=job.connection.id, connection_name=job.connection.name,
                    environment=job.connection.environment, tenant_id=job.connection.tenant_id,
                    trigger="scheduled", triggered_by=f"job #{job.id} ({job.name})",
                    schema_sync=job.schema_sync, schedule=job.schedule, status="failed",
                    error=str(e), finished_at=utcnow(),
                )
                db.add(run)
                db.flush()
                job.last_run_id = run.id
                job.last_message = f"Esecuzione non avviata: {e}"
                db.commit()
                return run.id
            run.job_id = job.id
            job.last_run_id = run.id
            job.last_message = None
            if job.kind == "once" and not manual:
                job.enabled = False  # eseguito: resta nello storico come concluso
            db.commit()
            run_id = run.id
        log.info("Job %s: avviata la run %s", job_id, run_id)
        self.deploy.submit(run_id)
        return run_id

    def delete_job(self, db: Session, job: DeployJob) -> None:
        self.unschedule(job.id)
        job.enabled = False
        db.flush()
        release_job_files(db, self.settings, job)
        db.delete(job)

    def _on_missed(self, event: JobExecutionEvent) -> None:
        if not event.job_id.startswith("deploy-job-"):
            return
        job_id = int(event.job_id.rsplit("-", 1)[1])
        when = event.scheduled_run_time.astimezone(self.tz).strftime("%d/%m/%Y %H:%M")
        with self.factory() as db:
            job = db.get(DeployJob, job_id)
            if job:
                job.last_message = f"Esecuzione delle {when} saltata: il servizio non era attivo"
                if job.kind == "once":
                    job.enabled = False
                db.commit()
        log.warning("Job %s: esecuzione delle %s saltata (misfire)", job_id, when)
