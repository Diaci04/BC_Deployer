"""Dashboard e health check."""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..models import Connection, DeployJob, Run, utcnow
from ..security.deps import csrf_protect, get_db
from ..web import render

router = APIRouter()

# Da quando gli environment possono passare a BC v30, che non ha più l'upload via Automation API
V30_FROM = date(2027, 4, 1)


@router.get("/", dependencies=[Depends(csrf_protect)])
def dashboard(request: Request, db: Session = Depends(get_db)):
    n_conn = db.scalar(select(func.count()).select_from(Connection))
    week_ago = utcnow() - timedelta(days=7)
    week = dict(db.execute(
        select(Run.status, func.count()).where(Run.created_at >= week_ago).group_by(Run.status)
    ).all())
    recent = db.scalars(select(Run).order_by(Run.id.desc()).limit(8)).all()
    active = db.scalars(select(Run).where(Run.status.in_(("queued", "running"))).order_by(Run.id)).all()
    sched = request.app.state.scheduler
    jobs = db.scalars(select(DeployJob).where(DeployJob.enabled.is_(True))).all()
    upcoming = sorted(
        ((j, sched.next_run_time(j.id)) for j in jobs if sched.next_run_time(j.id)), key=lambda x: x[1]
    )[:5]
    return render(
        request, "dashboard.html", n_connections=n_conn, recent=recent, active=active, upcoming=upcoming,
        n_jobs=len(jobs), week_ok=week.get("succeeded", 0),
        week_ko=sum(week.get(s, 0) for s in ("failed", "interrupted")),
        days_to_v30=max((V30_FROM - date.today()).days, 0),
    )


@router.get("/favicon.ico", include_in_schema=False)
def favicon():
    # Alcuni browser la chiedono comunque, ignorando il <link rel="icon">
    return RedirectResponse("/static/favicon.svg", status_code=301)


@router.get("/health", include_in_schema=False)
def health(request: Request, db: Session = Depends(get_db)):
    checks = {}
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "error"
    checks["scheduler"] = "ok" if request.app.state.scheduler.running else "error"
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse({"status": "ok" if ok else "error", **checks}, status_code=200 if ok else 503)
