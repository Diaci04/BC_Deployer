"""Impostazioni: server SMTP per le notifiche email. La password non viene mai restituita."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..models import SmtpSettings
from ..security.crypto import SecretBoxError
from ..security.deps import csrf_protect, get_db
from ..services.connections import CheckResult
from ..services.mail import SECURITY, SmtpInput, get_smtp, parse_emails, send_email
from ..web import render

router = APIRouter(prefix="/settings", dependencies=[Depends(csrf_protect)])


def _render(request: Request, smtp: SmtpSettings | None, data: SmtpInput, errors: dict, status_code=200):
    data.password = ""
    return render(request, "settings.html", status_code, smtp=smtp, data=data, errors=errors, security=SECURITY)


@router.get("")
def settings_page(request: Request, db: Session = Depends(get_db)):
    smtp = get_smtp(db)
    data = SmtpInput()
    if smtp:
        data = SmtpInput(smtp.host, str(smtp.port), smtp.security, smtp.username or "", "", smtp.from_addr)
    return _render(request, smtp, data, {})


@router.post("/smtp")
def save_smtp(
    request: Request,
    host: str = Form(""),
    port: str = Form("587"),
    security: str = Form("starttls"),
    username: str = Form(""),
    password: str = Form(""),
    from_addr: str = Form(""),
    db: Session = Depends(get_db),
):
    smtp = get_smtp(db)
    data = SmtpInput(host, port, security, username, password, from_addr)
    errors = data.validate()
    if data.username and not data.password and not (smtp and smtp.password_enc):
        errors["password"] = "Indica la password dell'account"
    if errors:
        return _render(request, smtp, data, errors, 422)
    if smtp is None:
        smtp = SmtpSettings(id=1)
        db.add(smtp)
    data.apply(smtp, request.app.state.secret_box)
    db.commit()
    return RedirectResponse("/settings?msg=smtp_saved", status_code=303)


@router.post("/smtp/delete")
def delete_smtp(db: Session = Depends(get_db)):
    smtp = get_smtp(db)
    if smtp:
        db.delete(smtp)
        db.commit()
    return RedirectResponse("/settings?msg=smtp_deleted", status_code=303)


@router.post("/smtp/test")
async def test_smtp(request: Request, to: str = Form(""), db: Session = Depends(get_db)):
    smtp = await run_in_threadpool(get_smtp, db)
    emails, err = parse_emails(to)
    if smtp is None:
        result = CheckResult(False, [(False, "Salva prima le impostazioni del server")])
    elif err or not emails:
        result = CheckResult(False, [(False, err or "Indica un destinatario")])
    else:
        text = "Questa è un'email di prova di BC Deployer: il server SMTP è configurato correttamente."
        try:
            await run_in_threadpool(
                send_email, smtp, request.app.state.secret_box, emails, "[Prova] BC Deployer", text
            )
            result = CheckResult(True, [(True, f"Email di prova inviata a {', '.join(emails)}")])
        except (RuntimeError, SecretBoxError) as e:
            result = CheckResult(False, [(False, f"Invio non riuscito: {e}")])
    return render(request, "connections/_test_result.html", result=result)
