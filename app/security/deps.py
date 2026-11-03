"""Dipendenze FastAPI: DB, utente autenticato, protezione CSRF."""
import hmac
from collections.abc import Iterator
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..models import UserSession
from .auth import load_session

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class NotAuthenticated(Exception):
    pass


def get_db(request: Request) -> Iterator[Session]:
    db = request.app.state.sessionmaker()
    try:
        yield db
    finally:
        db.close()


def require_user(request: Request, db: Session = Depends(get_db)) -> UserSession:
    settings = request.app.state.settings
    sess = load_session(db, settings, request.cookies.get(settings.session_cookie))
    if sess is None:
        raise NotAuthenticated()
    request.state.user_session = sess
    return sess


def check_origin(request: Request) -> None:
    """Rifiuta richieste non sicure che arrivano da un altro sito."""
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Richiesta cross-site rifiutata")
    origin = request.headers.get("origin")
    if origin and origin != "null" and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "Origine della richiesta non valida")


async def csrf_protect(request: Request, sess: UserSession = Depends(require_user)) -> UserSession:
    if request.method in SAFE_METHODS:
        return sess
    check_origin(request)
    token = request.headers.get("x-csrf-token")
    ctype = request.headers.get("content-type", "")
    if not token and ctype.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        token = (await request.form()).get("csrf_token")
    if not isinstance(token, str) or not hmac.compare_digest(token, sess.csrf_token):
        raise HTTPException(403, "Token CSRF non valido: ricarica la pagina")
    return sess


def client_ip(request: Request) -> str:
    # Dietro proxy, uvicorn --proxy-headers imposta client.host dall'X-Forwarded-For
    return request.client.host if request.client else "unknown"
