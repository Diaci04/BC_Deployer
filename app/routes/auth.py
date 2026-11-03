"""Login e logout."""
import hmac
import secrets

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..security.auth import create_session, delete_session, is_rate_limited, record_attempt, verify_credentials
from ..security.deps import check_origin, client_ip, csrf_protect, get_db
from ..web import render

router = APIRouter()


def _safe_next(url: str | None) -> str:
    # Solo percorsi locali: niente open redirect verso altri siti
    if url and url.startswith("/") and not url.startswith(("//", "/\\")):
        return url
    return "/"


def _login_page(request: Request, error: str | None = None, status_code: int = 200, next_url: str = "/"):
    settings = request.app.state.settings
    token = secrets.token_urlsafe(32)
    resp = render(
        request, "login.html", status_code=status_code, login_csrf=token, error=error, next_url=_safe_next(next_url)
    )
    resp.set_cookie(
        settings.login_csrf_cookie, token, max_age=1800, path="/",
        httponly=True, secure=settings.cookie_secure, samesite="strict",
    )
    return resp


@router.get("/login")
def login_form(request: Request, next: str = "/"):
    return _login_page(request, next_url=next)


@router.post("/login")
def login(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    csrf_token: str = Form(""),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    settings = request.app.state.settings
    check_origin(request)
    cookie = request.cookies.get(settings.login_csrf_cookie, "")
    if not cookie or not hmac.compare_digest(cookie, csrf_token):
        return _login_page(request, "Modulo scaduto: riprova.", 400, next)

    ip = client_ip(request)
    if is_rate_limited(db, settings, ip):
        return _login_page(
            request, f"Troppi tentativi falliti. Riprova tra {settings.login_window_minutes} minuti.", 429, next
        )

    ok = verify_credentials(settings, username, password)
    record_attempt(db, ip, ok)
    if not ok:
        db.commit()
        return _login_page(request, "Credenziali non valide.", 401, next)

    token = create_session(db, settings, settings.admin_username, ip, request.headers.get("user-agent"))
    db.commit()
    resp = RedirectResponse(_safe_next(next), status_code=303)
    resp.set_cookie(
        settings.session_cookie, token, max_age=settings.session_max_hours * 3600, path="/",
        httponly=True, secure=settings.cookie_secure, samesite="strict",
    )
    resp.delete_cookie(settings.login_csrf_cookie, path="/", secure=settings.cookie_secure, httponly=True)
    return resp


@router.post("/logout", dependencies=[Depends(csrf_protect)])
def logout(request: Request, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    delete_session(db, request.cookies.get(settings.session_cookie))
    db.commit()
    resp = RedirectResponse("/login?msg=logged_out", status_code=303)
    resp.delete_cookie(settings.session_cookie, path="/", secure=settings.cookie_secure, httponly=True)
    return resp
