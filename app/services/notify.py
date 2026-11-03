"""Notifiche a fine deploy, configurate per connessione: webhook (Teams, Slack o JSON generico) ed email.

- Teams: webhook di un flusso "Workflows" (Power Automate) o di un vecchio connettore in arrivo:
  entrambi accettano un messaggio con Adaptive Card.
- Slack: Incoming Webhook ({"text": ...}).
- generic: JSON con i dati della run, per integrazioni proprie.
- email: ai destinatari della connessione, col server SMTP delle Impostazioni.
L'URL del webhook contiene un token: è cifrato a riposo e non compare mai in log o pagine.
"""
import html
import logging
from urllib.parse import urlsplit

import httpx
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..models import Connection, Run, RunItem, RunLog, utcnow
from ..security.crypto import SecretBox, SecretBoxError
from .mail import get_smtp, send_email

log = logging.getLogger(__name__)

KINDS = {"teams": "Microsoft Teams", "slack": "Slack", "generic": "JSON generico"}
NOTIFY_ON = {"all": "Ogni esecuzione", "failure": "Solo esecuzioni non riuscite"}
STATUS_TEXT = {
    "succeeded": "✅ Completato",
    "failed": "❌ Fallito",
    "canceled": "⏹ Annullato",
    "interrupted": "⚠️ Interrotto",
}
ITEM_TEXT = {
    "succeeded": "completata", "scheduled": "programmata", "already_installed": "già installata",
    "failed": "FALLITA", "skipped": "saltata", "queued": "non eseguita", "uploading": "interrotta",
    "in_progress": "esito sconosciuto",
}


def validate_webhook_url(url: str) -> str | None:
    """Messaggio d'errore, oppure None se l'URL è accettabile."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return "Deve essere un URL https://"
    if parts.username or parts.password:
        return "L'URL non deve contenere credenziali"
    if len(url) > 2000:
        return "URL troppo lungo"
    return None


def build_payload(kind: str, run: Run, link: str | None) -> dict:
    title = f"BC Deployer · {STATUS_TEXT.get(run.status, run.status)} · {run.connection_name} ({run.environment})"
    lines = [f"{it.name} {it.version}: {ITEM_TEXT.get(it.status, it.status)}" for it in run.items]
    if kind == "slack":
        text = f"*{title}*\n" + "\n".join(f"• {line}" for line in lines)
        if run.error:
            text += f"\n> {run.error[:1500]}"
        if link:
            text += f"\n<{link}|Apri l'esecuzione #{run.id}>"
        return {"text": text}
    if kind == "teams":
        body = [
            {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True},
            {"type": "TextBlock", "text": f"Esecuzione #{run.id} · avviata da {run.triggered_by}", "isSubtle": True,
             "wrap": True},
            {"type": "TextBlock", "text": "\n".join(f"- {line}" for line in lines) or "-", "wrap": True},
        ]
        if run.error:
            body.append({"type": "TextBlock", "text": run.error[:1500], "color": "Attention", "wrap": True})
        card = {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard", "version": "1.4", "body": body,
        }
        if link:
            card["actions"] = [{"type": "Action.OpenUrl", "title": "Apri l'esecuzione", "url": link}]
        return {
            "type": "message",
            "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}],
        }
    return {
        "event": "deploy.finished",
        "run_id": run.id, "status": run.status, "error": run.error, "connection": run.connection_name,
        "environment": run.environment, "trigger": run.trigger, "triggered_by": run.triggered_by,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "apps": [{"name": it.name, "version": it.version, "status": it.status, "error": it.error_message}
                 for it in run.items],
        "url": link,
    }


def build_email(run: Run, link: str | None) -> tuple[str, str, str]:
    """Oggetto, testo semplice e HTML dell'email di fine deploy."""
    status = STATUS_TEXT.get(run.status, run.status)
    subject = f"BC Deployer · {status} · {run.connection_name} ({run.environment})"
    lines = [(it.name, it.version, ITEM_TEXT.get(it.status, it.status), it.error_message) for it in run.items]
    text = [f"{status} · {run.connection_name} · environment {run.environment}",
            f"Esecuzione #{run.id}, avviata da {run.triggered_by}", ""]
    text += [f"- {n} {v}: {s}" + (f"\n  {err}" if err else "") for n, v, s, err in lines]
    if run.error:
        text += ["", run.error]
    if link:
        text += ["", f"Dettagli: {link}"]

    e = html.escape
    color = {"succeeded": "#157f3b", "failed": "#b42318"}.get(run.status, "#5d6778")
    rows = "".join(
        f'<tr><td style="padding:6px 12px 6px 0"><strong>{e(n)}</strong> {e(v)}</td>'
        f'<td style="padding:6px 0">{e(s)}</td></tr>'
        + (f'<tr><td colspan="2" style="padding:0 0 6px;color:#b42318">{e(err)}</td></tr>' if err else "")
        for n, v, s, err in lines
    )
    body = (
        f'<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#1c2330">'
        f'<h2 style="margin:0 0 4px;color:{color}">{e(status)}</h2>'
        f'<p style="margin:0 0 12px">{e(run.connection_name)} · environment <strong>{e(run.environment)}</strong><br>'
        f'<span style="color:#5d6778">Esecuzione #{run.id}, avviata da {e(run.triggered_by)}</span></p>'
        f'<table style="border-collapse:collapse">{rows}</table>'
        + (f'<p style="color:#b42318">{e(run.error)}</p>' if run.error else "")
        + (f'<p><a href="{e(link)}">Apri l\'esecuzione</a></p>' if link else "")
        + "</div>"
    )
    return subject, "\n".join(text), body


def send(url: str, payload: dict, *, transport: httpx.BaseTransport | None = None) -> None:
    """Invia la notifica. Solleva RuntimeError con un messaggio che non contiene l'URL."""
    last = ""
    with httpx.Client(transport=transport, timeout=10, follow_redirects=False) as client:
        for _ in range(2):
            try:
                r = client.post(url, json=payload)
            except httpx.TransportError as e:
                last = f"errore di rete ({type(e).__name__})"
                continue
            if r.is_success:
                return
            last = f"HTTP {r.status_code}"
            if r.status_code < 500 and r.status_code != 429:
                break
    raise RuntimeError(last)


def notify_run(
    factory: sessionmaker[Session], settings: Settings, box: SecretBox, run_id: int,
    *, transport: httpx.BaseTransport | None = None,
) -> None:
    with factory() as db:
        run = db.get(Run, run_id)
        conn = db.get(Connection, run.connection_id) if run and run.connection_id else None
        if conn is None or not conn.has_notifications:
            return
        if conn.notify_on == "failure" and run.status == "succeeded":
            return
        link = f"{settings.public_base_url.rstrip('/')}/runs/{run.id}" if settings.public_base_url else None
        results = []
        if conn.webhook_url_enc and conn.webhook_kind:
            label = KINDS.get(conn.webhook_kind, "")
            try:
                send(box.decrypt(conn.webhook_url_enc), build_payload(conn.webhook_kind, run, link),
                     transport=transport)
                results.append((f"Notifica {label} inviata", "info"))
            except (RuntimeError, SecretBoxError) as e:
                results.append((f"Notifica {label} non inviata: {e}", "warn"))
        if conn.email_list:
            results.append(_email(db, box, conn.email_list, run, link))
        for message, level in results:
            if level == "warn":
                log.warning("Run %s: %s", run_id, message)
            db.add(RunLog(run_id=run_id, level=level, message=message))
        db.commit()


def _email(db: Session, box: SecretBox, to: list[str], run: Run, link: str | None) -> tuple[str, str]:
    smtp = get_smtp(db)
    if smtp is None:
        return "Email non inviata: server SMTP non configurato (vedi Impostazioni)", "warn"
    try:
        send_email(smtp, box, to, *build_email(run, link))
        return f"Email inviata a {', '.join(to)}", "info"
    except (RuntimeError, SecretBoxError) as e:
        return f"Email non inviata: {e}", "warn"


def sample_run(conn: Connection) -> Run:
    """Esecuzione fittizia per i messaggi di prova."""
    now = utcnow()
    return Run(
        id=0, connection_name=conn.name, environment=conn.environment, trigger="manual", triggered_by="test",
        status="succeeded", started_at=now, finished_at=now,
        items=[RunItem(position=0, app_id="-", name="App di prova", publisher="-", version="1.0.0.0",
                       status="succeeded")],
    )
