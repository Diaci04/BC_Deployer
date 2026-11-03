"""Deploy programmati: elenco, dettaglio, pausa/ripresa, esecuzione immediata, eliminazione."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import DeployJob, Run
from ..security.deps import csrf_protect, get_db
from ..services.scheduler import next_fire_times, parse_cron
from ..web import render

router = APIRouter(prefix="/jobs", dependencies=[Depends(csrf_protect)])


def _get(db: Session, job_id: int) -> DeployJob:
    job = db.get(DeployJob, job_id)
    if job is None:
        raise HTTPException(404, "Job non trovato")
    return job


@router.get("")
def list_jobs(request: Request, db: Session = Depends(get_db)):
    jobs = db.scalars(select(DeployJob).order_by(DeployJob.enabled.desc(), DeployJob.id.desc())).all()
    sched = request.app.state.scheduler
    return render(request, "jobs/list.html", jobs=jobs, next_run={j.id: sched.next_run_time(j.id) for j in jobs})


@router.get("/cron-preview")
def cron_preview(request: Request, cron_expr: str = ""):
    tz = request.app.state.settings.timezone
    if not cron_expr.strip():
        return Response(status_code=204)
    try:
        times, error = next_fire_times(parse_cron(cron_expr, tz)), None
    except ValueError as e:
        times, error = [], str(e)
    return render(request, "jobs/_cron_preview.html", times=times, error=error)


@router.get("/{job_id}")
def job_detail(request: Request, job_id: int, db: Session = Depends(get_db)):
    job = _get(db, job_id)
    runs = db.scalars(select(Run).where(Run.job_id == job_id).order_by(Run.id.desc()).limit(20)).all()
    return render(
        request, "jobs/detail.html", job=job, runs=runs, next_run=request.app.state.scheduler.next_run_time(job_id)
    )


@router.post("/{job_id}/pause")
def pause_job(request: Request, job_id: int, db: Session = Depends(get_db)):
    job = _get(db, job_id)
    job.enabled = False
    db.commit()
    request.app.state.scheduler.pause(job_id)
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.post("/{job_id}/resume")
def resume_job(request: Request, job_id: int, db: Session = Depends(get_db)):
    job = _get(db, job_id)
    if job.kind == "once" and request.app.state.scheduler.next_run_time(job_id) is None:
        raise HTTPException(409, "La data di esecuzione è passata: usa 'Esegui ora' o crea un nuovo job")
    job.enabled = True
    db.commit()
    request.app.state.scheduler.resume(job)
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.post("/{job_id}/run-now")
def run_now(request: Request, job_id: int, confirm_env: str = Form(""), db: Session = Depends(get_db)):
    job = _get(db, job_id)
    if job.connection.is_production and confirm_env.strip() != job.connection.environment:
        raise HTTPException(400, f"Per un environment di produzione digita esattamente '{job.connection.environment}'")
    run_id = request.app.state.scheduler.fire(job_id, manual=True)
    return Response(status_code=204, headers={"HX-Redirect": f"/runs/{run_id}"})


@router.post("/{job_id}/delete")
def delete_job(request: Request, job_id: int, db: Session = Depends(get_db)):
    job = _get(db, job_id)
    request.app.state.scheduler.delete_job(db, job)
    db.commit()
    return Response(status_code=204, headers={"HX-Redirect": "/jobs?msg=job_deleted"})
