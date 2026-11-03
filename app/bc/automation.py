"""Backend Automation API di Business Central.

Sequenza documentata da Microsoft (verificata su learn.microsoft.com, novembre 2026):
  1. POST   companies({id})/extensionUpload                      {"schedule", "schemaSyncMode"}
            (se il record esiste già: GET extensionUpload + PATCH extensionUpload({id}), vedi _upload_record)
  2. PATCH  companies({id})/extensionUpload({id})/extensionContent   body = .app, If-Match: *
  3. POST   companies({id})/extensionUpload({id})/Microsoft.NAV.upload   ← mancava in bc_deploy.py
  4. GET    companies({id})/extensionDeploymentStatus               polling

L'upload tramite extensionUpload è deprecato: Microsoft lo rimuove con la v30 (2027 release wave 1).
"""
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .base import DeployState, DeployStatus, InstalledApp, Schedule, SchemaSync, UploadHandle
from .http import BCError, BCHttp
from .manifest import AppManifest
from .version import Version

# Margine per differenze di orologio tra noi e BC nel confronto con startedOn
CLOCK_SKEW = timedelta(minutes=10)


def parse_bc_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    # BC può restituire 7 cifre di frazione: fromisoformat ne accetta al massimo 6
    v = re.sub(r"(\.\d{6})\d+", r"\1", value.strip()).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class AutomationClient:
    def __init__(self, http: BCHttp):
        self.http = http

    def list_companies(self) -> list[dict]:
        return self.http.request("GET", "/companies").json().get("value", [])

    def resolve_company(self, name: str | None = None) -> dict:
        """Restituisce la company indicata (per name o displayName) o la prima."""
        items = self.list_companies()
        if not items:
            raise BCError("Nessuna company trovata nell'environment")
        if name:
            for c in items:
                if name in (c.get("name"), c.get("displayName")):
                    return c
            raise BCError(f"Company '{name}' non trovata nell'environment")
        return items[0]


class AutomationBackend:
    """Implementazione di DeployBackend sull'Automation API."""

    def __init__(self, http: BCHttp, company: str | None = None, on_log: Callable[[str], None] | None = None):
        self.http = http
        self.client = AutomationClient(http)
        self._company_name = company
        self._company: dict | None = None
        self._log = on_log or (lambda m: None)

    @property
    def company_id(self) -> str:
        if self._company is None:
            self._company = self.client.resolve_company(self._company_name)
            self._log(f"Company: {self._company.get('name')} ({self._company['id']})")
        return self._company["id"]

    def _path(self, suffix: str) -> str:
        return f"/companies({self.company_id}){suffix}"

    def describe_target(self) -> str:
        return f"Automation API, company {self._company.get('name') if self._company else '?'}"

    def list_apps(self) -> list[InstalledApp]:
        items = self.http.request("GET", self._path("/extensions")).json().get("value", [])
        apps = []
        for e in items:
            try:
                version = Version.from_parts(
                    int(e.get("versionMajor", 0)), int(e.get("versionMinor", 0)),
                    int(e.get("versionBuild", 0)), int(e.get("versionRevision", 0)),
                )
            except (TypeError, ValueError):
                continue
            apps.append(
                InstalledApp(
                    id=str(e.get("id", "")).lower(),
                    name=e.get("displayName", ""),
                    publisher=e.get("publisher", ""),
                    version=version,
                    is_installed=bool(e.get("isInstalled")),
                    published_as=e.get("publishedAs"),
                )
            )
        return apps

    def upload_and_install(
        self, manifest: AppManifest, path: Path, schema_sync: SchemaSync, schedule: Schedule
    ) -> UploadHandle:
        started = datetime.now(UTC)
        upload_id = self._upload_record({"schedule": schedule.value, "schemaSyncMode": schema_sync.value})

        size_mb = path.stat().st_size / 1024 / 1024
        self._log(f"Invio del pacchetto ({size_mb:.1f} MB)…")
        self.http.request(
            "PATCH", self._path(f"/extensionUpload({upload_id})/extensionContent"),
            content_factory=lambda: open(path, "rb"),
            headers={"Content-Type": "application/octet-stream", "If-Match": "*"},
            timeout=self.http.upload_timeout,
        )
        # Le operazioni già registrate per questa app/versione non sono la nostra
        previous = frozenset(
            i["operationID"] for i in self._matching_operations(manifest) if i.get("operationID")
        )
        self._log("Pacchetto inviato, avvio installazione (Microsoft.NAV.upload)")
        # Non idempotente: niente retry su 5xx/timeout per non avviare due installazioni
        self.http.request(
            "POST", self._path(f"/extensionUpload({upload_id})/Microsoft.NAV.upload"), idempotent=False
        )
        return UploadHandle(manifest=manifest, started_at=started, upload_id=upload_id, ignore_operations=previous)

    def _upload_record(self, options: dict) -> str:
        """Id del record extensionUpload da usare per questo upload.

        BC tiene un solo record per company (chiave Id=0): se esiste già, per esempio
        da un upload precedente, il POST fallisce con Internal_EntityWithSameKeyExists.
        In quel caso si aggiorna il record esistente, come fa BcContainerHelper.
        """
        existing = self.http.request("GET", self._path("/extensionUpload")).json().get("value", [])
        record = next((r for r in existing if r.get("systemId") or r.get("id")), None)
        if record:
            upload_id = record.get("systemId") or record.get("id")
            self.http.request("PATCH", self._path(f"/extensionUpload({upload_id})"), json=options,
                              headers={"If-Match": "*"})
            self._log(f"Record extensionUpload esistente aggiornato ({upload_id})")
            return upload_id
        r = self.http.request("POST", self._path("/extensionUpload"), json=options)
        body = r.json() if r.content else {}
        upload_id = body.get("systemId") or body.get("id")
        if not upload_id:
            raise BCError("extensionUpload: BC non ha restituito l'id del record")
        self._log(f"Record extensionUpload creato ({upload_id})")
        return upload_id

    def _matching_operations(self, m: AppManifest) -> list[dict]:
        items = self.http.request("GET", self._path("/extensionDeploymentStatus")).json().get("value", [])
        return [
            i for i in items
            if (i.get("name") or "").lower() == m.name.lower()
            and i.get("appVersion") == str(m.version)
            and (not i.get("publisher") or i["publisher"].lower() == m.publisher.lower())
        ]

    def deploy_status(self, handle: UploadHandle) -> DeployStatus:
        since = handle.started_at - CLOCK_SKEW
        candidates = []
        for i in self._matching_operations(handle.manifest):
            if i.get("operationID") in handle.ignore_operations:
                continue
            started = parse_bc_datetime(i.get("startedOn"))
            if started and started < since:
                continue  # operazione vecchia, senza operationID utilizzabile
            candidates.append((started or datetime.min.replace(tzinfo=UTC), i))
        if not candidates:
            return DeployStatus(DeployState.PENDING)
        _, item = max(candidates, key=lambda c: c[0])
        raw = item.get("status")
        state = {"completed": DeployState.COMPLETED, "failed": DeployState.FAILED}.get(
            (raw or "").lower(), DeployState.PENDING
        )
        return DeployStatus(state, raw_status=raw, operation_id=item.get("operationID"))
