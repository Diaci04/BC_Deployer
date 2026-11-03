"""CRUD delle connessioni a Business Central. Il client secret non viene mai restituito."""
from itertools import groupby

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import Connection, DeployJob
from ..security.crypto import SecretBoxError
from ..security.deps import csrf_protect, get_db
from ..bc.http import BCError
from ..services.connections import (
    ENV_RE, CheckResult, ConnectionInput, check_connection, list_companies, list_environments, lookup_hint,
)
from ..services.mail import get_smtp, send_email
from ..services.notify import KINDS, NOTIFY_ON, build_email, build_payload, sample_run, send
from ..web import render

router = APIRouter(prefix="/connections", dependencies=[Depends(csrf_protect)])


def _get(db: Session, conn_id: int) -> Connection:
    conn = db.get(Connection, conn_id)
    if conn is None:
        raise HTTPException(404, "Connessione non trovata")
    return conn


def _form(
    name: str = Form(""),
    customer: str = Form(""),
    tenant_id: str = Form(""),
    client_id: str = Form(""),
    client_secret: str = Form(""),
    environment: str = Form(""),
    company: str = Form(""),
    is_production: bool = Form(False),
    webhook_kind: str = Form(""),
    webhook_url: str = Form(""),
    notify_on: str = Form("all"),
    remove_webhook: bool = Form(False),
    notify_emails: str = Form(""),
) -> ConnectionInput:
    return ConnectionInput(
        name=name, tenant_id=tenant_id, client_id=client_id, client_secret=client_secret, environment=environment,
        company=company, is_production=is_production, webhook_kind=webhook_kind, webhook_url=webhook_url,
        notify_on=notify_on, remove_webhook=remove_webhook, customer=customer, notify_emails=notify_emails,
    )


def _render_form(
    request: Request, db: Session, data: ConnectionInput, errors: dict, conn: Connection | None,
    status_code=200, source: Connection | None = None,
):
    # Segreti mai rimandati al browser, neanche dopo un errore di validazione
    data.client_secret = data.webhook_url = ""
    customers = db.scalars(
        select(Connection.customer).where(Connection.customer.is_not(None)).distinct().order_by(Connection.customer)
    ).all()
    return render(
        request, "connections/form.html", status_code, data=data, errors=errors, conn=conn, source=source,
        kinds=KINDS, notify_on=NOTIFY_ON, customers=customers, smtp_ready=get_smtp(db) is not None,
    )


def _save(request: Request, db: Session, conn: Connection, data: ConnectionInput, source: Connection | None = None):
    data.apply(conn, request.app.state.secret_box)  # secret vuoto = resta quello salvato
    if source is not None:  # copia: secret e webhook vuoti = quelli della connessione di origine
        conn.client_secret_enc = conn.client_secret_enc or source.client_secret_enc
        if conn.webhook_kind and not conn.webhook_url_enc:
            conn.webhook_url_enc = source.webhook_url_enc
    if conn.id is None:
        db.add(conn)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return {"name": "Esiste già una connessione con questo nome"}
    return None


@router.get("")
def list_connections(request: Request, db: Session = Depends(get_db)):
    conns = db.scalars(select(Connection)).all()
    # Raggruppate per cliente (quelle senza cliente in fondo), poi per nome
    conns = sorted(conns, key=lambda c: (c.customer is None, (c.customer or "").lower(), c.name.lower()))
    groups = [(k, list(g)) for k, g in groupby(conns, key=lambda c: c.customer)]
    return render(request, "connections/list.html", groups=groups, n=len(conns))


# ---- elenchi di environment e company per il form (il secret non torna mai al browser)

def _lookup_credentials(db: Session, box, tenant_id: str, client_id: str, client_secret: str,
                        conn_id: int | None, copy_from: int | None) -> tuple[str, str, str] | str:
    """Credenziali da usare: quelle del form, con il secret salvato se il campo è vuoto. Oppure un messaggio."""
    data = ConnectionInput(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret, name="x",
                           environment="x")
    errors = data.validate(require_secret=False)
    if errors.get("tenant_id") or errors.get("client_id"):
        return "Compila prima Tenant ID e Client ID"
    secret = data.client_secret
    if not secret:
        saved = db.get(Connection, conn_id or copy_from) if (conn_id or copy_from) else None
        if saved is None:
            return "Compila prima il client secret"
        try:
            secret = box.decrypt(saved.client_secret_enc)
        except SecretBoxError as e:
            return str(e)
    return data.tenant_id, data.client_id, secret


@router.post("/lookup/environments")
async def lookup_environments(
    request: Request,
    tenant_id: str = Form(""), client_id: str = Form(""), client_secret: str = Form(""),
    environment: str = Form(""), conn_id: int | None = Form(None), copy_from: int | None = Form(None),
    db: Session = Depends(get_db),
):
    state = request.app.state
    creds = await run_in_threadpool(
        _lookup_credentials, db, state.secret_box, tenant_id, client_id, client_secret, conn_id, copy_from
    )
    ctx = {"value": environment.strip(), "envs": None, "message": None, "auto": False}
    if isinstance(creds, str):
        ctx["message"] = creds
    else:
        try:
            ctx["envs"] = await run_in_threadpool(list_environments, *creds, state.settings, transport=state.bc_transport)
            if not ctx["envs"]:
                ctx["envs"], ctx["message"] = None, "Nessun environment trovato nel tenant: scrivi il nome"
        except BCError as e:
            ctx["message"] = (f"Elenco non disponibile ({lookup_hint(e)}): scrivi il nome. Per l'elenco serve il "
                              "permesso AdminCenter.ReadWrite.All e l'app autorizzata nel BC admin center.")
    return render(request, "connections/_env_field.html", **ctx)


@router.post("/lookup/companies")
async def lookup_companies(
    request: Request,
    tenant_id: str = Form(""), client_id: str = Form(""), client_secret: str = Form(""),
    environment: str = Form(""), company: str = Form(""),
    conn_id: int | None = Form(None), copy_from: int | None = Form(None),
    db: Session = Depends(get_db),
):
    state = request.app.state
    ctx = {"value": company.strip(), "companies": None, "message": None, "auto": False}
    creds = await run_in_threadpool(
        _lookup_credentials, db, state.secret_box, tenant_id, client_id, client_secret, conn_id, copy_from
    )
    if isinstance(creds, str):
        ctx["message"] = creds
    elif not ENV_RE.match(environment.strip()):
        ctx["message"] = "Scegli prima l'environment"
    else:
        try:
            ctx["companies"] = await run_in_threadpool(
                list_companies, *creds, environment.strip(), state.settings, transport=state.bc_transport
            )
        except BCError as e:
            ctx["message"] = f"Elenco non disponibile ({lookup_hint(e)}): scrivi il nome o lascia vuoto"
    return render(request, "connections/_company_field.html", **ctx)


@router.get("/new")
def new_connection(request: Request, db: Session = Depends(get_db)):
    return _render_form(request, db, ConnectionInput(), {}, None)


@router.get("/{conn_id}/copy")
def copy_connection(request: Request, conn_id: int, db: Session = Depends(get_db)):
    source = _get(db, conn_id)
    data = ConnectionInput.from_connection(source)
    data.name, data.environment, data.is_production = f"{source.name} (copia)", "", False
    return _render_form(request, db, data, {}, None, source=source)


@router.post("")
def create_connection(
    request: Request, data: ConnectionInput = Depends(_form), copy_from: int | None = Form(None),
    db: Session = Depends(get_db),
):
    source = _get(db, copy_from) if copy_from else None
    errors = data.validate(require_secret=source is None, has_webhook=bool(source and source.webhook_url_enc))
    if not errors:
        errors = _save(request, db, Connection(), data, source)
    if errors:
        return _render_form(request, db, data, errors, None, 422, source=source)
    return RedirectResponse("/connections?msg=conn_saved", status_code=303)


@router.get("/{conn_id}/edit")
def edit_connection(request: Request, conn_id: int, db: Session = Depends(get_db)):
    conn = _get(db, conn_id)
    return _render_form(request, db, ConnectionInput.from_connection(conn), {}, conn)


@router.post("/{conn_id}")
def update_connection(
    request: Request, conn_id: int, data: ConnectionInput = Depends(_form), db: Session = Depends(get_db)
):
    conn = _get(db, conn_id)
    errors = data.validate(require_secret=False, has_webhook=bool(conn.webhook_url_enc))
    if not errors:
        errors = _save(request, db, conn, data)
    if errors:
        return _render_form(request, db, data, errors, conn, 422)
    return RedirectResponse("/connections?msg=conn_saved", status_code=303)


@router.post("/{conn_id}/delete")
def delete_connection(request: Request, conn_id: int, db: Session = Depends(get_db)):
    conn = _get(db, conn_id)
    if db.scalars(select(DeployJob).where(DeployJob.connection_id == conn_id)).first():
        raise HTTPException(409, "La connessione ha deploy programmati: eliminali prima di eliminarla")
    db.delete(conn)
    db.commit()
    return Response(status_code=204, headers={"HX-Redirect": "/connections?msg=conn_deleted"})


@router.post("/{conn_id}/test")
async def test_connection(request: Request, conn_id: int, db: Session = Depends(get_db)):
    conn = await run_in_threadpool(_get, db, conn_id)
    state = request.app.state
    result = await run_in_threadpool(
        check_connection, conn, state.secret_box, state.settings, transport=state.bc_transport
    )
    return render(request, "connections/_test_result.html", result=result)


@router.post("/{conn_id}/test-webhook")
async def test_webhook(request: Request, conn_id: int, db: Session = Depends(get_db)):
    conn = await run_in_threadpool(_get, db, conn_id)
    if not conn.webhook_url_enc or not conn.webhook_kind:
        return render(request, "connections/_test_result.html", result=_result(False, "Nessun webhook configurato"))
    state = request.app.state
    try:
        url = state.secret_box.decrypt(conn.webhook_url_enc)
        payload = build_payload(conn.webhook_kind, sample_run(conn), None)
        await run_in_threadpool(send, url, payload, transport=state.bc_transport)
        result = _result(True, f"Messaggio di prova inviato a {KINDS[conn.webhook_kind]}")
    except (RuntimeError, SecretBoxError) as e:  # il messaggio non contiene l'URL
        result = _result(False, f"Invio non riuscito: {e}")
    return render(request, "connections/_test_result.html", result=result)


@router.post("/{conn_id}/test-email")
async def test_email(request: Request, conn_id: int, db: Session = Depends(get_db)):
    conn = await run_in_threadpool(_get, db, conn_id)
    smtp = await run_in_threadpool(get_smtp, db)
    if not conn.email_list:
        result = _result(False, "Nessun destinatario email configurato")
    elif smtp is None:
        result = _result(False, "Server SMTP non configurato: vai in Impostazioni")
    else:
        subject, text, html = build_email(sample_run(conn), None)
        try:
            await run_in_threadpool(
                send_email, smtp, request.app.state.secret_box, conn.email_list, "[Prova] " + subject, text, html
            )
            result = _result(True, f"Email di prova inviata a {', '.join(conn.email_list)}")
        except (RuntimeError, SecretBoxError) as e:
            result = _result(False, f"Invio non riuscito: {e}")
    return render(request, "connections/_test_result.html", result=result)


def _result(ok: bool, text: str) -> CheckResult:
    return CheckResult(ok=ok, steps=[(ok, text)])
