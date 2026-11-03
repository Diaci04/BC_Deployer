"""Invio email tramite SMTP, per le notifiche a fine deploy.

La configurazione del server sta nel DB (pagina Impostazioni), con la password cifrata.
I messaggi d'errore non contengono mai la password.
"""
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr

from sqlalchemy.orm import Session

from ..models import SmtpSettings
from ..security.crypto import SecretBox

EMAIL_RE = re.compile(r"^[^@\s,;<>\"']+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")
HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,255}$")
SECURITY = {"starttls": "STARTTLS (porta 587)", "ssl": "SSL/TLS (porta 465)", "none": "Nessuna (sconsigliato)"}
MAX_RECIPIENTS = 20
TIMEOUT = 20


def parse_emails(raw: str) -> tuple[list[str], str | None]:
    """Indirizzi separati da virgola, punto e virgola, spazi o a capo. Restituisce (indirizzi, errore)."""
    emails = []
    for e in re.split(r"[\s,;]+", raw or ""):
        if not e:
            continue
        if not EMAIL_RE.match(e) or len(e) > 254:
            return [], f"Indirizzo non valido: {e[:60]}"
        if e.lower() not in (x.lower() for x in emails):
            emails.append(e)
    if len(emails) > MAX_RECIPIENTS:
        return [], f"Massimo {MAX_RECIPIENTS} destinatari"
    return emails, None


@dataclass
class SmtpInput:
    host: str = ""
    port: str = "587"
    security: str = "starttls"
    username: str = ""
    password: str = ""
    from_addr: str = ""

    def __post_init__(self):
        for f in ("host", "port", "security", "username", "from_addr"):
            setattr(self, f, (getattr(self, f) or "").strip())

    def validate(self) -> dict[str, str]:
        errors = {}
        if not HOST_RE.match(self.host):
            errors["host"] = "Nome del server non valido (es. smtp.office365.com)"
        if not self.port.isdigit() or not 1 <= int(self.port) <= 65535:
            errors["port"] = "Porta non valida"
        if self.security not in SECURITY:
            errors["security"] = "Valore non valido"
        if len(self.username) > 255:
            errors["username"] = "Troppo lungo"
        if len(self.password) > 500:
            errors["password"] = "Troppo lunga"
        if not EMAIL_RE.match(self.from_addr):
            errors["from_addr"] = "Indirizzo email non valido"
        return errors

    def apply(self, s: SmtpSettings, box: SecretBox) -> None:
        s.host, s.port, s.security = self.host, int(self.port), self.security
        s.username = self.username or None
        s.from_addr = self.from_addr
        if self.password:  # vuota = resta quella salvata
            s.password_enc = box.encrypt(self.password)
        if not s.username:
            s.password_enc = None


def get_smtp(db: Session) -> SmtpSettings | None:
    return db.get(SmtpSettings, 1)


def send_email(
    smtp: SmtpSettings, box: SecretBox, to: list[str], subject: str, text: str, html: str | None = None
) -> None:
    """Invia un'email. Solleva RuntimeError con un messaggio senza credenziali."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("BC Deployer", smtp.from_addr))
    msg["To"] = ", ".join(to)
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    ctx = ssl.create_default_context()
    try:
        password = box.decrypt(smtp.password_enc) if smtp.password_enc else ""
        if smtp.security == "ssl":
            server = smtplib.SMTP_SSL(smtp.host, smtp.port, timeout=TIMEOUT, context=ctx)
        else:
            server = smtplib.SMTP(smtp.host, smtp.port, timeout=TIMEOUT)
        with server:
            if smtp.security == "starttls":
                server.starttls(context=ctx)
            if smtp.username:
                server.login(smtp.username, password)
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        raise RuntimeError(f"autenticazione rifiutata dal server ({_reply(e)})") from None
    except smtplib.SMTPResponseException as e:
        raise RuntimeError(f"il server ha risposto {_reply(e)}") from None
    except smtplib.SMTPRecipientsRefused:
        raise RuntimeError("destinatari rifiutati dal server") from None
    except smtplib.SMTPException as e:
        raise RuntimeError(f"errore SMTP ({type(e).__name__})") from None
    except ssl.SSLError:
        raise RuntimeError("errore TLS: prova a cambiare la sicurezza o la porta") from None
    except OSError as e:
        raise RuntimeError(f"server {smtp.host}:{smtp.port} non raggiungibile ({type(e).__name__})") from None


def _reply(e: smtplib.SMTPResponseException) -> str:
    detail = e.smtp_error.decode(errors="replace") if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
    return f"{e.smtp_code} {detail[:200]}".strip()
