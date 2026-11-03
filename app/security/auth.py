"""Autenticazione dell'unico utente admin, sessioni lato server e limite ai tentativi di login."""
import hashlib
import hmac
import logging
import secrets
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import LoginAttempt, UserSession, utcnow

log = logging.getLogger(__name__)
_ph = PasswordHasher()


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_credentials(settings: Settings, username: str, password: str) -> bool:
    # L'hash viene verificato sempre, anche con username sbagliato: tempi di risposta uguali
    user_ok = hmac.compare_digest(username.encode(), settings.admin_username.encode())
    try:
        pw_ok = _ph.verify(settings.admin_password_hash, password)
    except (VerifyMismatchError, VerificationError):
        pw_ok = False
    except InvalidHashError:
        log.error("ADMIN_PASSWORD_HASH non è un hash argon2 valido")
        pw_ok = False
    return user_ok and pw_ok


# ---------------------------------------------------------------- rate limit
def is_rate_limited(db: Session, settings: Settings, ip: str) -> bool:
    since = utcnow() - timedelta(minutes=settings.login_window_minutes)
    failures = select(func.count()).select_from(LoginAttempt).where(
        LoginAttempt.success.is_(False), LoginAttempt.ts >= since
    )
    per_ip = db.scalar(failures.where(LoginAttempt.ip == ip))
    if per_ip >= settings.login_max_attempts:
        return True
    # Limite globale contro attacchi distribuiti su molti IP
    return db.scalar(failures) >= settings.login_max_attempts * 10


def record_attempt(db: Session, ip: str, success: bool) -> None:
    db.add(LoginAttempt(ip=ip, success=success))
    if success:
        db.execute(delete(LoginAttempt).where(LoginAttempt.ip == ip, LoginAttempt.success.is_(False)))
    db.execute(delete(LoginAttempt).where(LoginAttempt.ts < utcnow() - timedelta(days=1)))


# ---------------------------------------------------------------- sessioni
def _session_id(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(
    db: Session, settings: Settings, username: str, ip: str | None, user_agent: str | None
) -> str:
    token = secrets.token_urlsafe(32)
    now = utcnow()
    db.add(
        UserSession(
            id=_session_id(token),
            username=username,
            csrf_token=secrets.token_urlsafe(32),
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(hours=settings.session_max_hours),
            ip=ip,
            user_agent=(user_agent or "")[:300],
        )
    )
    db.execute(delete(UserSession).where(UserSession.expires_at < now))
    return token


def load_session(db: Session, settings: Settings, token: str | None) -> UserSession | None:
    if not token:
        return None
    sess = db.get(UserSession, _session_id(token))
    if sess is None:
        return None
    now = utcnow()
    idle_limit = sess.last_seen_at + timedelta(minutes=settings.session_idle_minutes)
    if now >= sess.expires_at or now >= idle_limit:
        db.delete(sess)
        db.commit()
        return None
    if (now - sess.last_seen_at).total_seconds() > 60:
        sess.last_seen_at = now
        db.commit()
    return sess


def delete_session(db: Session, token: str | None) -> None:
    if token:
        db.execute(delete(UserSession).where(UserSession.id == _session_id(token)))
