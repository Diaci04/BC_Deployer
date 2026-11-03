"""Pagina di deploy: upload dei pacchetti, anteprima dell'ordine, verifica sull'environment, avvio o programmazione."""
from datetime import datetime, timedelta
from itertools import groupby
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..bc.automation import AutomationBackend
from ..bc.base import Schedule, SchemaSync
from ..bc.http import BCError
from ..bc.ordering import OrderingError, order_by_dependencies
from ..bc.precheck import precheck
from ..models import AppFile, Connection, DeployJob, JobFile, UserSession, utcnow
from ..security.crypto import SecretBoxError
from ..security.deps import client_ip, csrf_protect, get_db
from ..services.connections import make_http
from ..services.deployer import RunError, create_run
from ..services.scheduler import parse_cron
from ..services.uploads import UploadError, delete_file, manifest_of, receive_upload
from ..web import render

router = APIRouter(dependencies=[Depends(csrf_protect)])

SCHEDULE_LABELS = {
    Schedule.CURRENT: "Versione corrente (subito)",
    Schedule.NEXT_MINOR: "Prossima versione minor",
    Schedule.NEXT_MAJOR: "Prossima versione major",
}
SYNC_LABELS = {SchemaSync.ADD: "Add", SchemaSync.FORCE: "Force Sync"}


def file_json(f: AppFile) -> dict:
    return {
        "id": f.id, "name": f.name, "publisher": f.publisher, "version": f.version,
        "file": f.original_name, "size_mb": round(f.size / 1024 / 1024, 2),
    }


def selected_files(db: Session, file_ids: list[int]) -> list[AppFile]:
    if not file_ids:
        return []
    files = db.scalars(select(AppFile).where(AppFile.id.in_(file_ids), AppFile.status != "deleted")).all()
    if len(files) != len(set(file_ids)):
        raise HTTPException(409, "Uno o più file non sono più disponibili: ricaricali")
    return list(files)


def _connection(db: Session, connection_id: int | None) -> Connection | None:
    return db.get(Connection, connection_id) if connection_id else None


@router.get("/deploy")
def deploy_page(request: Request, connection_id: int | None = None, db: Session = Depends(get_db)):
    conns = db.scalars(select(Connection)).all()
    # Raggruppate per cliente nel menu di scelta (quelle senza cliente in fondo)
    conns = sorted(conns, key=lambda c: (c.customer is None, (c.customer or "").lower(), c.name.lower()))
    groups = [(k, list(g)) for k, g in groupby(conns, key=lambda c: c.customer)]
    return render(
        request, "deploy/page.html", connections=conns, groups=groups, selected_id=connection_id,
        schedules=SCHEDULE_LABELS, syncs=SYNC_LABELS,
    )


@router.put("/uploads")
async def upload(request: Request, name: str = Query(..., max_length=300), db: Session = Depends(get_db)):
    cl = request.headers.get("content-length")
    try:
        f = await receive_upload(
            db, request.app.state.settings, name, request.stream(), int(cl) if cl and cl.isdigit() else None
        )
    except UploadError as e:
        return PlainTextResponse(str(e), status_code=e.status_code)
    return JSONResponse(file_json(f))


@router.delete("/uploads/{file_id}")
def remove_upload(request: Request, file_id: int, db: Session = Depends(get_db)):
    f = db.get(AppFile, file_id)
    if f is not None and f.status == "staged":  # i file già legati a run o job restano
        delete_file(db, request.app.state.settings, f)
        db.commit()
    return Response(status_code=204)


@router.post("/deploy/preview")
def preview(
    request: Request,
    connection_id: int | None = Form(None),
    file_ids: list[int] = Form([]),
    when: str = Form("now"),
    db: Session = Depends(get_db),
):
    conn = _connection(db, connection_id)
    files = selected_files(db, file_ids)
    ordered, error = [], None
    by_app = {f.app_id: f for f in files}
    try:
        ordered = [(m, by_app[m.id]) for m in order_by_dependencies([manifest_of(f) for f in files])]
    except OrderingError as e:
        error = str(e)
    busy = conn is not None and request.app.state.deploy.is_busy(conn.tenant_id, conn.environment)
    return render(
        request, "deploy/_preview.html", conn=conn, ordered=ordered, error=error, busy=busy, files=files,
        scheduled=when != "now",
    )


def _env_check(request: Request, conn: Connection, files: list[AppFile]):
    state = request.app.state
    with make_http(conn, state.secret_box, state.settings, transport=state.bc_transport, max_retries=1) as http:
        backend = AutomationBackend(http, conn.company)
        ordered = order_by_dependencies([manifest_of(f) for f in files])
        return precheck(ordered, backend.list_apps())


@router.post("/deploy/precheck")
async def env_check(
    request: Request,
    connection_id: int | None = Form(None),
    file_ids: list[int] = Form([]),
    db: Session = Depends(get_db),
):
    conn = await run_in_threadpool(_connection, db, connection_id)
    files = await run_in_threadpool(selected_files, db, file_ids)
    if conn is None or not files:
        return Response(status_code=204)
    try:
        result, error = await run_in_threadpool(_env_check, request, conn, files), None
    except (BCError, SecretBoxError, OrderingError) as e:
        result, error = None, str(e)
    return render(request, "deploy/_precheck.html", result=result, error=error, conn=conn)


def _start_error(request: Request, message: str):
    return render(request, "deploy/_start_error.html", message=message)


@router.post("/deploy")
def start_deploy(
    request: Request,
    connection_id: int | None = Form(None),
    file_ids: list[int] = Form([]),
    schema_sync: str = Form(SchemaSync.ADD.value),
    schedule: str = Form(Schedule.CURRENT.value),
    confirm_env: str = Form(""),
    when: str = Form("now"),
    run_at: str = Form(""),
    cron_expr: str = Form(""),
    job_name: str = Form(""),
    delete_files_after: bool = Form(False),
    db: Session = Depends(get_db),
    sess: UserSession = Depends(csrf_protect),
):
    conn = _connection(db, connection_id)
    if conn is None:
        return _start_error(request, "Scegli una connessione")
    try:
        sync, sched = SchemaSync(schema_sync), Schedule(schedule)
    except ValueError:
        return _start_error(request, "Opzioni di deploy non valide")
    if when not in ("now", "once", "cron"):
        return _start_error(request, "Scegli quando eseguire il deploy")
    if conn.is_production and confirm_env.strip() != conn.environment:
        return _start_error(request, f"Per un environment di produzione digita esattamente '{conn.environment}'")
    try:
        files = selected_files(db, file_ids)
    except HTTPException as e:
        return _start_error(request, e.detail)

    if when != "now":
        return _create_job(
            request, db, sess, conn, files, sync, sched, when, run_at, cron_expr, job_name, delete_files_after
        )
    try:
        run = create_run(
            db, conn, files, schema_sync=sync, schedule=sched, trigger="manual",
            triggered_by=sess.username, client_ip=client_ip(request),
        )
    except (RunError, OrderingError) as e:
        return _start_error(request, str(e))
    db.commit()
    request.app.state.deploy.submit(run.id)
    return Response(status_code=204, headers={"HX-Redirect": f"/runs/{run.id}"})


def _create_job(request, db, sess, conn, files, sync, sched, when, run_at, cron_expr, job_name, delete_files_after):
    settings = request.app.state.settings
    tz = ZoneInfo(settings.timezone)
    if not files:
        return _start_error(request, "Nessun pacchetto selezionato")
    try:
        order_by_dependencies([manifest_of(f) for f in files])
    except OrderingError as e:
        return _start_error(request, str(e))

    job = DeployJob(
        connection_id=conn.id, kind=when, timezone=settings.timezone, schema_sync=sync.value,
        schedule=sched.value, created_by=sess.username, enabled=True,
        delete_files_after=delete_files_after if when == "once" else False,
    )
    if when == "once":
        try:
            local = datetime.strptime(run_at.strip(), "%Y-%m-%dT%H:%M")
        except ValueError:
            return _start_error(request, "Indica data e ora di esecuzione")
        job.run_at = local.replace(tzinfo=tz)
        if job.run_at < utcnow() + timedelta(minutes=1):
            return _start_error(request, "La data di esecuzione deve essere nel futuro")
        default_name = f"{conn.name} · {local:%d/%m/%Y %H:%M}"
    else:
        try:
            parse_cron(cron_expr, settings.timezone)
        except ValueError as e:
            return _start_error(request, f"Espressione cron non valida: {e}")
        job.cron_expr = " ".join(cron_expr.split())
        default_name = f"{conn.name} · cron {job.cron_expr}"
    job.name = (job_name.strip() or default_name)[:100]
    for pos, f in enumerate(files):
        job.files.append(JobFile(file_id=f.id, position=pos))
        f.status = "bound"
        f.expires_at = None
    db.add(job)
    db.commit()
    request.app.state.scheduler.schedule(job)
    return Response(status_code=204, headers={"HX-Redirect": f"/jobs/{job.id}?msg=job_created"})
