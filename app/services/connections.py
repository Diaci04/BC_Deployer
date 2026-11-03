"""Validazione delle connessioni, creazione del client BC e test di connessione."""
import re
from dataclasses import dataclass, field

import httpx

from ..bc.automation import AutomationClient
from ..bc.http import BCError, BCHttp
from ..config import Settings
from ..models import Connection
from ..security.crypto import SecretBox, SecretBoxError
from .mail import parse_emails
from .notify import KINDS, NOTIFY_ON, validate_webhook_url

GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
DOMAIN_RE = re.compile(r"^(?=.{4,253}$)[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")
ENV_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,49}$")


@dataclass
class ConnectionInput:
    name: str = ""
    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""
    environment: str = ""
    company: str = ""
    is_production: bool = False
    webhook_kind: str = ""
    webhook_url: str = ""
    notify_on: str = "all"
    remove_webhook: bool = False
    customer: str = ""
    notify_emails: str = ""

    def __post_init__(self):
        for f in ("name", "tenant_id", "client_id", "environment", "company", "webhook_kind", "webhook_url",
                  "customer", "notify_emails"):
            setattr(self, f, (getattr(self, f) or "").strip())

    @classmethod
    def from_connection(cls, conn: Connection) -> "ConnectionInput":
        """Dati del form a partire da una connessione salvata, senza segreti."""
        return cls(
            name=conn.name, tenant_id=conn.tenant_id, client_id=conn.client_id, environment=conn.environment,
            company=conn.company or "", is_production=conn.is_production, webhook_kind=conn.webhook_kind or "",
            notify_on=conn.notify_on, customer=conn.customer or "", notify_emails=", ".join(conn.email_list),
        )

    def validate(self, *, require_secret: bool, has_webhook: bool = False) -> dict[str, str]:
        errors = {}
        if len(self.customer) > 100:
            errors["customer"] = "Massimo 100 caratteri"
        _, err = parse_emails(self.notify_emails)
        if err:
            errors["notify_emails"] = err
        if self.webhook_kind and self.webhook_kind not in KINDS:
            errors["webhook_kind"] = "Tipo non valido"
        if self.notify_on not in NOTIFY_ON:
            errors["notify_on"] = "Valore non valido"
        if self.webhook_url:
            if err := validate_webhook_url(self.webhook_url):
                errors["webhook_url"] = err
        elif self.webhook_kind and not self.remove_webhook and not has_webhook:
            errors["webhook_url"] = "Indica l'URL del webhook"
        if not 1 <= len(self.name) <= 100:
            errors["name"] = "Obbligatorio, massimo 100 caratteri"
        if not (GUID_RE.match(self.tenant_id) or DOMAIN_RE.match(self.tenant_id)):
            errors["tenant_id"] = "Deve essere un GUID o un dominio (es. contoso.onmicrosoft.com)"
        if not GUID_RE.match(self.client_id):
            errors["client_id"] = "Deve essere un GUID"
        if require_secret and not self.client_secret:
            errors["client_secret"] = "Obbligatorio"
        if len(self.client_secret) > 500:
            errors["client_secret"] = "Troppo lungo"
        if not ENV_RE.match(self.environment):
            errors["environment"] = "Lettere, numeri, '-' e '_' (massimo 50)"
        if len(self.company) > 100:
            errors["company"] = "Massimo 100 caratteri"
        return errors

    def apply(self, conn: Connection, box: SecretBox) -> None:
        conn.name = self.name
        conn.customer = self.customer or None
        conn.notify_emails = ",".join(parse_emails(self.notify_emails)[0]) or None
        conn.tenant_id = self.tenant_id
        conn.client_id = self.client_id.lower()
        conn.environment = self.environment
        conn.company = self.company or None
        conn.is_production = self.is_production
        if self.client_secret:
            conn.client_secret_enc = box.encrypt(self.client_secret)
        conn.notify_on = self.notify_on
        if self.remove_webhook or not self.webhook_kind:
            conn.webhook_kind = conn.webhook_url_enc = None
        else:
            conn.webhook_kind = self.webhook_kind
            if self.webhook_url:  # vuoto = resta quello salvato
                conn.webhook_url_enc = box.encrypt(self.webhook_url)


def make_http(
    conn: Connection, box: SecretBox, settings: Settings, *, transport: httpx.BaseTransport | None = None, **kw
) -> BCHttp:
    return BCHttp(
        conn.tenant_id,
        conn.client_id,
        box.decrypt(conn.client_secret_enc),
        conn.environment,
        timeout=settings.bc_timeout,
        upload_timeout=settings.bc_upload_timeout,
        transport=transport,
        **kw,
    )


@dataclass
class CheckResult:
    ok: bool
    steps: list[tuple[bool, str]] = field(default_factory=list)


def check_connection(
    conn: Connection, box: SecretBox, settings: Settings, *, transport: httpx.BaseTransport | None = None
) -> CheckResult:
    """Prova token Entra + GET /companies (+ company configurata). Nessun retry lungo: è interattivo."""
    res = CheckResult(ok=False)
    try:
        http = make_http(conn, box, settings, transport=transport, max_retries=1, sleep=lambda s: None)
    except SecretBoxError as e:
        res.steps.append((False, str(e)))
        return res
    with http:
        try:
            http.token()
            res.steps.append((True, "Token Entra ID ottenuto"))
            client = AutomationClient(http)
            companies = client.list_companies()
            names = ", ".join(c.get("name", "?") for c in companies[:5])
            more = "…" if len(companies) > 5 else ""
            res.steps.append((True, f"Automation API raggiungibile: {len(companies)} company ({names}{more})"))
            if not companies:
                res.steps.append((False, "Nessuna company nell'environment"))
                return res
            if conn.company:
                client.resolve_company(conn.company)
                res.steps.append((True, f"Company '{conn.company}' trovata"))
            res.ok = True
        except BCError as e:
            res.steps.append((False, _hint(e)))
    return res


def _hint(e: BCError) -> str:
    msg = str(e)
    if e.status_code == 401:
        msg += " — verifica che l'app sia registrata e abilitata nella pagina 'Microsoft Entra Applications' di BC"
    elif e.status_code == 403:
        msg += " — mancano permessi: API permission Automation.ReadWrite.All e permission set D365 AUTOMATION"
    elif e.status_code == 404:
        msg += " — environment inesistente o nome errato"
    return msg
