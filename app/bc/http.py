"""Client HTTP verso Entra ID e Business Central: token con cache, retry su 429/5xx con Retry-After.

Rifattorizzato da BCClient._auth/request di bc_deploy.py. Differenze:
- il token è in cache per (tenant, client) e condiviso fra i thread;
- un 401 invalida il token e riprova una volta;
- retry anche su 500 ed errori di rete, Retry-After in secondi o come data HTTP;
- il corpo del PATCH viene riaperto a ogni tentativo (prima un retry inviava un body vuoto);
- i messaggi di errore riportano il testo di BC ma mai token o secret.
"""
import hashlib
import logging
import random
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import IO, Any
from urllib.parse import quote

import httpx

AUTH_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
SCOPE = "https://api.businesscentral.dynamics.com/.default"
API_BASE = (
    "https://api.businesscentral.dynamics.com/v2.0/{tenant}/{env}"
    "/api/microsoft/automation/v2.0"
)
# Admin Center API: elenco degli environment del tenant. Richiede il permesso AdminCenter.ReadWrite.All
# e l'app autorizzata nella pagina "Authorized Microsoft Entra apps" del BC admin center.
ADMIN_ENVIRONMENTS_URL = "https://api.businesscentral.dynamics.com/admin/v2.29/applications/BusinessCentral/environments"
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_WAIT = 120

log = logging.getLogger(__name__)


class BCError(Exception):
    """Errore mostrabile all'utente: non contiene mai segreti."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class BCAuthError(BCError):
    pass


# ---------------------------------------------------------------- token
class _TokenCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._tokens: dict[str, tuple[str, float]] = {}

    @staticmethod
    def key(tenant: str, client_id: str, secret: str) -> str:
        # Il secret entra nella chiave (hash) così cambiarlo invalida il token in cache
        return hashlib.sha256(f"{tenant}|{client_id}|{secret}".encode()).hexdigest()

    def get(self, key: str) -> str | None:
        with self._lock:
            item = self._tokens.get(key)
        if item and time.time() < item[1] - 120:
            return item[0]
        return None

    def put(self, key: str, token: str, expires_in: int) -> None:
        with self._lock:
            self._tokens[key] = (token, time.time() + expires_in)

    def drop(self, key: str) -> None:
        with self._lock:
            self._tokens.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._tokens.clear()


token_cache = _TokenCache()


def _entra_error(r: httpx.Response) -> str:
    try:
        j = r.json()
        desc = (j.get("error_description") or "").splitlines()[0]
        return f"{j.get('error', 'errore')}: {desc}"[:400]
    except ValueError:
        return f"HTTP {r.status_code}"


def bc_error_message(r: httpx.Response) -> str:
    """Estrae il messaggio di errore OData restituito da BC."""
    try:
        err = r.json().get("error") or {}
        code, msg = err.get("code"), err.get("message")
        if msg:
            return f"{code}: {msg}" if code else msg
    except ValueError:
        pass
    text = r.text.strip()
    return text[:1000] if text else r.reason_phrase


def retry_after_seconds(r: httpx.Response) -> float | None:
    value = r.headers.get("Retry-After")
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError):
            return None
    return max(0.0, min(seconds, MAX_WAIT))


def backoff_seconds(attempt: int) -> float:
    return min(60.0, 2.0 * 2**attempt) + random.uniform(0, 1)


# ---------------------------------------------------------------- client
class BCHttp:
    def __init__(
        self,
        tenant: str,
        client_id: str,
        client_secret: str,
        environment: str,
        *,
        timeout: float = 60,
        upload_timeout: float = 900,
        max_retries: int = 5,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        on_log: Callable[[str], None] | None = None,
    ):
        self.tenant = tenant
        self.client_id = client_id
        self._secret = client_secret
        self.base = API_BASE.format(tenant=quote(tenant, safe=""), env=quote(environment, safe=""))
        self.timeout = timeout
        self.upload_timeout = upload_timeout
        self.max_retries = max_retries
        self._sleep = sleep
        self._log = on_log or (lambda m: log.info(m))
        self._cache_key = token_cache.key(tenant, client_id, client_secret)
        self._http = httpx.Client(transport=transport, timeout=timeout, follow_redirects=False)

    def __repr__(self) -> str:
        return f"BCHttp(tenant={self.tenant!r}, client_id={self.client_id!r}, base={self.base!r})"

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- token
    def token(self) -> str:
        cached = token_cache.get(self._cache_key)
        if cached:
            return cached
        url = AUTH_URL.format(tenant=quote(self.tenant, safe=""))
        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self._secret,
            "scope": SCOPE,
        }
        for attempt in range(self.max_retries + 1):
            try:
                r = self._http.post(url, data=data, timeout=30)
            except httpx.TransportError as e:
                if attempt < self.max_retries:
                    self._wait(backoff_seconds(attempt), f"errore di rete verso Entra ({type(e).__name__})")
                    continue
                raise BCAuthError(f"Entra ID non raggiungibile: {type(e).__name__}") from None
            if r.status_code in RETRY_STATUS and attempt < self.max_retries:
                self._wait(retry_after_seconds(r) or backoff_seconds(attempt), f"Entra HTTP {r.status_code}")
                continue
            if r.status_code != 200:
                raise BCAuthError(f"Autenticazione Entra fallita ({_entra_error(r)})", r.status_code)
            j = r.json()
            token_cache.put(self._cache_key, j["access_token"], int(j.get("expires_in", 3600)))
            return j["access_token"]
        raise BCAuthError("Autenticazione Entra fallita: troppi tentativi")

    def _wait(self, seconds: float, reason: str) -> None:
        self._log(f"{reason}, nuovo tentativo tra {seconds:.0f}s")
        self._sleep(seconds)

    # -- richieste
    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict | None = None,
        headers: dict | None = None,
        content_factory: Callable[[], IO[bytes]] | None = None,
        timeout: float | None = None,
        idempotent: bool = True,
    ) -> httpx.Response:
        """Esegue una richiesta all'Automation API (path relativo) o a un URL completo di BC.

        content_factory restituisce un file aperto da inviare come body: viene
        richiamata a ogni tentativo, così un retry rimanda il file dall'inizio.
        Con idempotent=False si ritenta solo quando BC sicuramente non ha eseguito
        la richiesta (429, connessione non stabilita).
        """
        url = path if path.startswith("https://") else f"{self.base}{path}"
        refreshed = False
        attempt = 0
        while True:
            h = {"Authorization": f"Bearer {self.token()}", "Accept": "application/json", **(headers or {})}
            body = content_factory() if content_factory else None
            try:
                r = self._http.request(
                    method, url, json=json, params=params, headers=h, content=body,
                    timeout=timeout or self.timeout,
                )
            except httpx.TransportError as e:
                safe_to_retry = idempotent or isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout))
                if attempt < self.max_retries and safe_to_retry:
                    self._wait(backoff_seconds(attempt), f"{method} {path}: errore di rete ({type(e).__name__})")
                    attempt += 1
                    continue
                raise BCError(f"{method} {path}: Business Central non raggiungibile ({type(e).__name__})") from None
            finally:
                if body is not None:
                    body.close()

            if r.status_code == 401 and not refreshed:
                token_cache.drop(self._cache_key)
                refreshed = True
                continue
            retryable = RETRY_STATUS if idempotent else {429}
            if r.status_code in retryable and attempt < self.max_retries:
                wait = retry_after_seconds(r)
                self._wait(wait if wait is not None else backoff_seconds(attempt), f"{method} {path}: HTTP {r.status_code}")
                attempt += 1
                continue
            if r.is_error:
                raise BCError(f"{method} {path} → HTTP {r.status_code}: {bc_error_message(r)}", r.status_code)
            return r
