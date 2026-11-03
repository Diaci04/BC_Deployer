"""Dettaglio di un'esecuzione, aggiornamenti live via SSE, annullamento e rilancio."""
import asyncio
import json
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import exists, select
from sqlalchemy.orm import Session, sessionmaker

from ..bc.base import Schedule, SchemaSync
from ..bc.ordering import OrderingError
from ..models import Connection, Run, RunItem, RunLog, UserSession
from ..security.deps import client_ip, csrf_protect, get_db
from ..services.deployer import FINAL_RUN_STATES, RunError, create_run
from ..web import render

router = APIRouter(prefix="/runs", dependencies=[Depends(csrf_protect)])

HEARTBEAT_SECONDS = 15
PAGE_SIZE = 50


def _get(db: Session, run_id: int) -> Run:
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(404, "Esecuzione non trovata")
    return run


def rerunnable(run: Run) -> str | None:
    """None se la run si può rilanciare, altrimenti il motivo."""
    if run.status not in ("failed", "canceled", "interrupted"):
        return "Si possono rilanciare solo esecuzioni fallite, annullate o interrotte"
    if run.connection_id is None:
        return "La connessione è stata eliminata"
    if any(it.file is None or it.file.status == "deleted" for it in run.items):
        return "I pacchetti di questa esecuzione sono stati eliminati dal server"
    return None


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


@router.get("")
def list_runs(
    request: Request,
    connection_id: str = "",
    status: str = "",
    trigger: str = "",
    app: str = "",
    date_from: str = "",
    date_to: str = "",
    page: int = 1,
    db: Session = Depends(get_db),
):
    tz = ZoneInfo(request.app.state.settings.timezone)
    q = select(Run)
    if connection_id.isdigit():
        q = q.where(Run.connection_id == int(connection_id))
    if status:
        q = q.where(Run.status == status)
    if trigger:
        q = q.where(Run.trigger == trigger)
    if app.strip():
        pattern = f"%{app.strip()[:100]}%"
        q = q.where(exists().where(RunItem.run_id == Run.id, RunItem.name.ilike(pattern)))
    if d := _parse_date(date_from):
        q = q.where(Run.created_at >= datetime.combine(d, time.min, tz))
    if d := _parse_date(date_to):
        q = q.where(Run.created_at < datetime.combine(d + timedelta(days=1), time.min, tz))
    page = max(page, 1)
    rows = db.scalars(q.order_by(Run.id.desc()).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE + 1)).all()
    filters = {
        "connection_id": connection_id, "status": status, "trigger": trigger, "app": app,
        "date_from": date_from, "date_to": date_to,
    }
    ctx = dict(
        runs=rows[:PAGE_SIZE], has_next=len(rows) > PAGE_SIZE, page=page, filters=filters,
        connections=db.scalars(select(Connection).order_by(Connection.name)).all(),
    )
    if "hx-request" in request.headers:
        return render(request, "runs/_rows.html", **ctx)
    return render(request, "runs/list.html", **ctx)


@router.get("/{run_id}")
def run_detail(request: Request, run_id: int, db: Session = Depends(get_db)):
    run = _get(db, run_id)
    logs = db.scalars(select(RunLog).where(RunLog.run_id == run_id).order_by(RunLog.id)).all()
    conn = db.get(Connection, run.connection_id) if run.connection_id else None
    return render(
        request, "runs/detail.html", run=run, logs=logs, conn=conn,
        final=run.status in FINAL_RUN_STATES, rerun_block=rerunnable(run),
    )


def _snapshot(factory: sessionmaker, run_id: int, after_log_id: int):
    with factory() as db:
        run = db.get(Run, run_id)
        if run is None:
            return None
        logs = db.scalars(
            select(RunLog).where(RunLog.run_id == run_id, RunLog.id > after_log_id).order_by(RunLog.id)
        ).all()
        return (
            {"type": "run", "status": run.status, "error": run.error},
            [
                {
                    "type": "item", "id": it.id, "status": it.status, "error_message": it.error_message,
                    "installed_version": it.installed_version, "bc_status": it.bc_status,
                }
                for it in run.items
            ],
            [
                {"type": "log", "id": l.id, "ts": l.ts.isoformat(), "level": l.level,
                 "message": l.message, "item_id": l.item_id}
                for l in logs
            ],
        )


def _sse(event: dict) -> str:
    head = f"id: {event['id']}\n" if event["type"] == "log" else ""
    return f"{head}event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.get("/{run_id}/events")
async def run_events(request: Request, run_id: int):
    state = request.app.state
    try:
        last_id = int(request.headers.get("last-event-id") or request.query_params.get("after") or 0)
    except ValueError:
        last_id = 0
    queue = state.bus.subscribe(run_id)  # prima dello snapshot: nessun evento perso nel mezzo
    snap = await run_in_threadpool(_snapshot, state.sessionmaker, run_id, last_id)
    if snap is None:
        state.bus.unsubscribe(run_id, queue)
        raise HTTPException(404, "Esecuzione non trovata")

    async def stream():
        try:
            run_ev, items, logs = snap
            seen = last_id
            for ev in logs:
                seen = ev["id"]
                yield _sse(ev)
            for ev in items:
                yield _sse(ev)
            yield _sse(run_ev)
            if run_ev["status"] in FINAL_RUN_STATES:
                return
            while not await request.is_disconnected():
                try:
                    ev = await asyncio.wait_for(queue.get(), HEARTBEAT_SECONDS)
                except TimeoutError:
                    yield ": ping\n\n"  # tiene aperta la connessione attraverso i proxy
                    continue
                if ev["type"] == "log":
                    if ev["id"] <= seen:
                        continue
                    seen = ev["id"]
                yield _sse(ev)
                if ev["type"] == "run" and ev["status"] in FINAL_RUN_STATES:
                    return
        finally:
            state.bus.unsubscribe(run_id, queue)

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"}
    )


@router.post("/{run_id}/cancel")
def cancel_run(request: Request, run_id: int, db: Session = Depends(get_db)):
    run = _get(db, run_id)
    if run.status not in FINAL_RUN_STATES:
        request.app.state.deploy.cancel(run_id)
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.post("/{run_id}/rerun")
def rerun(
    request: Request,
    run_id: int,
    confirm_env: str = Form(""),
    db: Session = Depends(get_db),
    sess: UserSession = Depends(csrf_protect),
):
    run = _get(db, run_id)
    block = rerunnable(run)
    if block:
        raise HTTPException(409, block)
    conn = db.get(Connection, run.connection_id)
    if conn.is_production and confirm_env.strip() != conn.environment:
        raise HTTPException(400, f"Per un environment di produzione digita esattamente '{conn.environment}'")
    try:
        new = create_run(
            db, conn, [it.file for it in run.items],
            schema_sync=SchemaSync(run.schema_sync), schedule=Schedule(run.schedule),
            trigger="rerun", triggered_by=sess.username, client_ip=client_ip(request), parent_run_id=run.id,
        )
    except (RunError, OrderingError) as e:
        raise HTTPException(409, str(e)) from None
    db.commit()
    request.app.state.deploy.submit(new.id)
    return Response(status_code=204, headers={"HX-Redirect": f"/runs/{new.id}"})
