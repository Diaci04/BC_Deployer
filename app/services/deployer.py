"""Esecuzione dei deploy: una run alla volta per environment, stato per app, log su DB + eventi live.

Logica ripresa dal main() di bc_deploy.py, con in più:
- verifiche preliminari sulle app installate (stessa versione → saltata, downgrade e
  dipendenze mancanti → stop prima di iniziare);
- polling con backoff (5 s → 30 s) e timeout per app;
- alla prima app fallita le successive risultano "saltate";
- lock per environment: una seconda run sullo stesso environment resta in coda.
"""
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..bc.automation import AutomationBackend
from ..bc.base import DeployBackend, DeployStatus, Schedule, SchemaSync
from ..bc.http import BCError
from ..bc.ordering import order_by_dependencies
from ..bc.polling import wait_for_deploy
from ..bc.precheck import Action, precheck
from ..config import Settings
from ..models import AppFile, Connection, Run, RunItem, RunLog, utcnow
from ..security.crypto import SecretBox, SecretBoxError
from .connections import make_http
from .events import EventBus
from .uploads import file_path, manifest_of

log = logging.getLogger(__name__)

FINAL_RUN_STATES = {"succeeded", "failed", "canceled", "interrupted"}
ACTIVE_ITEM_STATES = {"uploading", "in_progress"}


class RunError(Exception):
    pass


def create_run(
    db: Session,
    conn: Connection,
    files: list[AppFile],
    *,
    schema_sync: SchemaSync,
    schedule: Schedule,
    trigger: str,
    triggered_by: str,
    client_ip: str | None = None,
    parent_run_id: int | None = None,
) -> Run:
    """Crea la run con le app già in ordine di installazione. Solleva OrderingError."""
    if not files:
        raise RunError("Nessun pacchetto selezionato")
    by_app = {f.app_id: f for f in files}
    ordered = order_by_dependencies([manifest_of(f) for f in files])
    run = Run(
        connection_id=conn.id,
        connection_name=conn.name,
        environment=conn.environment,
        tenant_id=conn.tenant_id,
        trigger=trigger,
        parent_run_id=parent_run_id,
        triggered_by=triggered_by,
        client_ip=client_ip,
        schema_sync=schema_sync.value,
        schedule=schedule.value,
        status="queued",
    )
    for pos, m in enumerate(ordered):
        f = by_app[m.id]
        run.items.append(
            RunItem(position=pos, file_id=f.id, app_id=m.id, name=m.name, publisher=m.publisher, version=str(m.version))
        )
        f.status = "bound"
        f.expires_at = None
    db.add(run)
    db.flush()
    return run


class RunRecorder:
    """Scrive stato e log della run nel DB e li pubblica sul bus."""

    def __init__(self, factory: sessionmaker[Session], bus: EventBus, run_id: int):
        self.factory = factory
        self.bus = bus
        self.run_id = run_id

    def log(self, message: str, level: str = "info", item_id: int | None = None) -> None:
        with self.factory() as db:
            entry = RunLog(run_id=self.run_id, item_id=item_id, level=level, message=message[:4000])
            db.add(entry)
            db.commit()
            event = {
                "type": "log", "id": entry.id, "ts": entry.ts.isoformat(),
                "level": level, "message": entry.message, "item_id": item_id,
            }
        self.bus.publish(self.run_id, event)

    def item(self, item_id: int, **fields) -> None:
        with self.factory() as db:
            it = db.get(RunItem, item_id)
            for k, v in fields.items():
                setattr(it, k, v)
            db.commit()
            event = {
                "type": "item", "id": it.id, "status": it.status, "error_message": it.error_message,
                "installed_version": it.installed_version, "bc_status": it.bc_status,
            }
        self.bus.publish(self.run_id, event)

    def run(self, **fields) -> None:
        with self.factory() as db:
            r = db.get(Run, self.run_id)
            for k, v in fields.items():
                setattr(r, k, v)
            db.commit()
            event = {"type": "run", "status": r.status, "error": r.error}
        self.bus.publish(self.run_id, event)

    def skip_remaining(self, items: list[RunItem], after: int) -> None:
        for it in items[after:]:
            self.item(it.id, status="skipped")


class DeployService:
    def __init__(
        self,
        settings: Settings,
        factory: sessionmaker[Session],
        secret_box: SecretBox,
        bus: EventBus,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_workers: int = 4,
    ):
        self.settings = settings
        self.factory = factory
        self.box = secret_box
        self.bus = bus
        self.transport = transport
        self._sleep = sleep
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="deploy")
        self._env_locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._cancel: set[int] = set()
        # Callback chiamate a fine run (notifiche, pulizia file): ricevono l'id della run
        self.on_finished: list[Callable[[int], None]] = []

    # -- API pubblica
    def submit(self, run_id: int) -> Future:
        return self._executor.submit(self._execute, run_id)

    def cancel(self, run_id: int) -> None:
        with self._guard:
            self._cancel.add(run_id)

    def is_busy(self, tenant_id: str, environment: str) -> bool:
        lock = self._env_locks.get(self._env_key(tenant_id, environment))
        return bool(lock and lock.locked())

    def recover_interrupted(self) -> int:
        """All'avvio: le run rimaste aperte da un processo precedente vengono chiuse come interrotte."""
        with self.factory() as db:
            runs = db.scalars(select(Run).where(Run.status.in_(("queued", "running")))).all()
            for r in runs:
                r.status = "interrupted"
                r.error = "Interrotta dal riavvio del servizio: verifica lo stato delle app in BC"
                r.finished_at = utcnow()
                for it in r.items:
                    if it.status in ACTIVE_ITEM_STATES:
                        it.status = "failed"
                        it.error_message = "Esito sconosciuto: servizio riavviato durante il deploy"
                    elif it.status == "queued":
                        it.status = "skipped"
            db.commit()
            return len(runs)

    def shutdown(self, wait: bool = False) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=True)

    # -- esecuzione
    @staticmethod
    def _env_key(tenant_id: str, environment: str) -> str:
        return f"{tenant_id.lower()}|{environment.lower()}"

    def _lock_for(self, key: str) -> threading.Lock:
        with self._guard:
            return self._env_locks.setdefault(key, threading.Lock())

    def _cancel_requested(self, run_id: int) -> bool:
        with self._guard:
            return run_id in self._cancel

    def _execute(self, run_id: int) -> None:
        rec = RunRecorder(self.factory, self.bus, run_id)
        try:
            with self.factory() as db:
                run = db.get(Run, run_id)
                conn = db.get(Connection, run.connection_id) if run.connection_id else None
                items = list(run.items)
                files = {it.id: it.file for it in items}
                db.expunge_all()
            if conn is None:
                rec.run(status="failed", error="Connessione eliminata", finished_at=utcnow())
                return

            lock = self._lock_for(self._env_key(conn.tenant_id, conn.environment))
            if not lock.acquire(blocking=False):
                rec.log("Un altro deploy è in corso su questo environment: la run resta in coda")
                lock.acquire()
            try:
                if self._cancel_requested(run_id):
                    rec.skip_remaining(items, 0)
                    rec.run(status="canceled", finished_at=utcnow())
                    return
                self._run_locked(rec, run, conn, items, files)
            finally:
                lock.release()
        except Exception:
            log.exception("Errore interno nella run %s", run_id)
            rec.run(status="failed", error="Errore interno del servizio (vedi log del server)", finished_at=utcnow())
        finally:
            with self._guard:
                self._cancel.discard(run_id)
            for cb in self.on_finished:
                try:
                    cb(run_id)
                except Exception:
                    log.exception("Callback di fine run fallita (run %s)", run_id)

    def _make_backend(self, conn: Connection, rec: RunRecorder) -> tuple[DeployBackend, object]:
        http = make_http(
            conn, self.box, self.settings, transport=self.transport, sleep=self._sleep,
            on_log=lambda m: rec.log(m, "warn"),
        )
        return AutomationBackend(http, conn.company, on_log=rec.log), http

    def _run_locked(self, rec: RunRecorder, run: Run, conn: Connection, items: list[RunItem], files: dict) -> None:
        rec.run(status="running", started_at=utcnow(), error=None)
        rec.log(
            f"Deploy su '{conn.name}' (environment {conn.environment}): {len(items)} app, "
            f"schemaSyncMode={run.schema_sync}, schedule={run.schedule}"
        )
        schedule = Schedule(run.schedule)
        schema_sync = SchemaSync(run.schema_sync)
        current: RunItem | None = None
        try:
            backend, http = self._make_backend(conn, rec)
        except SecretBoxError as e:
            rec.skip_remaining(items, 0)
            rec.run(status="failed", error=str(e), finished_at=utcnow())
            return

        with http:
            try:
                backend.company_id  # risolve e registra la company
                rec.log("Lettura delle app presenti nell'environment…")
                manifests = []
                for it in items:
                    f = files.get(it.id)
                    if f is None or f.status == "deleted" or not file_path(self.settings, f).exists():
                        raise RunError(f"Il pacchetto di {it.name} {it.version} non è più disponibile sul server")
                    manifests.append(manifest_of(f))
                check = precheck(manifests, backend.list_apps())
                for it, plan in zip(items, check.plans):
                    rec.item(it.id, installed_version=str(plan.installed_version) if plan.installed_version else None)
                if not check.ok:
                    for p in check.problems:
                        rec.log(p, "error")
                    rec.skip_remaining(items, 0)
                    rec.run(
                        status="failed",
                        error="Verifiche preliminari fallite: " + "; ".join(check.problems),
                        finished_at=utcnow(),
                    )
                    return

                for idx, (it, plan) in enumerate(zip(items, check.plans)):
                    if self._cancel_requested(run.id):
                        rec.log("Run annullata dall'utente", "warn")
                        rec.skip_remaining(items, idx)
                        rec.run(status="canceled", finished_at=utcnow())
                        return
                    current = it
                    if plan.action == Action.SAME_VERSION:
                        rec.item(it.id, status="already_installed", finished_at=utcnow())
                        rec.log(f"{it.name} {it.version} già installata: saltata", item_id=it.id)
                        continue

                    rec.item(it.id, status="uploading", started_at=utcnow())
                    verb = "Aggiornamento" if plan.action == Action.UPGRADE else "Installazione"
                    rec.log(f"{verb} di {it.name} {it.version}", item_id=it.id)
                    handle = backend.upload_and_install(
                        plan.manifest, file_path(self.settings, files[it.id]), schema_sync, schedule
                    )
                    if schedule != Schedule.CURRENT:
                        rec.item(it.id, status="scheduled", finished_at=utcnow())
                        rec.log(f"{it.name} programmata ({run.schedule})", item_id=it.id)
                        continue

                    rec.item(it.id, status="in_progress")
                    ok, message = self._wait_for_deploy(rec, backend, handle, it)
                    if not ok:
                        rec.item(it.id, status="failed", error_message=message, finished_at=utcnow())
                        rec.log(message, "error", item_id=it.id)
                        rec.skip_remaining(items, idx + 1)
                        rec.run(status="failed", error=f"{it.name}: {message}", finished_at=utcnow())
                        return
                    rec.item(it.id, status="succeeded", finished_at=utcnow())
                    rec.log(f"{it.name} {it.version} installata", item_id=it.id)
                    current = None

                rec.log("Deploy completato")
                rec.run(status="succeeded", finished_at=utcnow())
            except (BCError, RunError) as e:
                self._fail(rec, items, current, str(e))

    def _fail(self, rec: RunRecorder, items: list[RunItem], current: RunItem | None, message: str) -> None:
        rec.log(message, "error", item_id=current.id if current else None)
        start = 0
        if current is not None:
            rec.item(current.id, status="failed", error_message=message, finished_at=utcnow())
            start = items.index(current) + 1
        rec.skip_remaining(items, start)
        rec.run(status="failed", error=message, finished_at=utcnow())

    def _wait_for_deploy(self, rec: RunRecorder, backend: DeployBackend, handle, it: RunItem) -> tuple[bool, str]:
        def on_change(st: DeployStatus) -> None:
            shown = st.raw_status or "in attesa che BC registri l'operazione"
            rec.log(f"Stato in BC: {shown}", item_id=it.id)
            rec.item(it.id, bc_status=st.raw_status, operation_id=st.operation_id)

        return wait_for_deploy(
            backend, handle, timeout_minutes=self.settings.app_timeout_minutes, sleep=self._sleep, on_change=on_change
        )
