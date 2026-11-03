"""Rendering dei template Jinja2."""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

RUN_STATUS = {
    "queued": "In coda",
    "running": "In esecuzione",
    "succeeded": "Completata",
    "failed": "Fallita",
    "canceled": "Annullata",
    "interrupted": "Interrotta",
}
ITEM_STATUS = {
    "queued": "In coda",
    "uploading": "Upload",
    "in_progress": "In corso",
    "succeeded": "Completata",
    "scheduled": "Programmata",
    "already_installed": "Già installata",
    "failed": "Fallita",
    "skipped": "Saltata",
}
TRIGGERS = {"manual": "Manuale", "scheduled": "Schedulata", "rerun": "Rilancio"}
templates.env.globals.update(RUN_STATUS=RUN_STATUS, ITEM_STATUS=ITEM_STATUS, TRIGGERS=TRIGGERS)

# Messaggi mostrabili via ?msg=codice (mai testo arbitrario dall'URL)
FLASH = {
    "conn_saved": "Connessione salvata.",
    "conn_deleted": "Connessione eliminata.",
    "job_created": "Deploy programmato.",
    "smtp_saved": "Impostazioni email salvate.",
    "smtp_deleted": "Impostazioni email rimosse.",
    "job_deleted": "Job eliminato.",
    "logged_out": "Sei uscito.",
    "session_expired": "Sessione scaduta: accedi di nuovo.",
}


def _fmt_dt(request: Request):
    tz = ZoneInfo(request.app.state.settings.timezone)

    def fmt(dt: datetime | None, with_seconds: bool = False) -> str:
        if dt is None:
            return "—"
        return dt.astimezone(tz).strftime("%d/%m/%Y %H:%M:%S" if with_seconds else "%d/%m/%Y %H:%M")

    return fmt


def render(request: Request, name: str, status_code: int = 200, headers: dict | None = None, **ctx):
    sess = getattr(request.state, "user_session", None)
    ctx.setdefault("flash", FLASH.get(request.query_params.get("msg", "")))
    return templates.TemplateResponse(
        request,
        name,
        {
            "csrf_token": sess.csrf_token if sess else "",
            "user": sess.username if sess else None,
            "fmt_dt": _fmt_dt(request),
            "settings": request.app.state.settings,
            **ctx,
        },
        status_code=status_code,
        headers=headers,
    )
