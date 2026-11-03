"""Interfaccia comune dei backend di deploy.

Oggi esiste solo l'Automation API. Microsoft ne rimuove l'upload con la v30
(2027 release wave 1): il backend Admin Center API andrà aggiunto implementando
questa stessa interfaccia, senza toccare orchestrazione e interfaccia web.
"""
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from .manifest import AppManifest
from .version import Version


class SchemaSync(StrEnum):
    ADD = "Add"
    FORCE = "Force Sync"


class Schedule(StrEnum):
    # Valori come scritti nella documentazione Microsoft (v minuscola)
    CURRENT = "Current version"
    NEXT_MINOR = "Next minor version"
    NEXT_MAJOR = "Next major version"


class DeployState(StrEnum):
    PENDING = "pending"  # operazione non ancora visibile o in corso
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class InstalledApp:
    id: str
    name: str
    publisher: str
    version: Version
    is_installed: bool
    published_as: str | None = None


@dataclass
class DeployStatus:
    state: DeployState
    raw_status: str | None = None
    operation_id: str | None = None
    message: str | None = None


@dataclass
class UploadHandle:
    manifest: AppManifest
    started_at: datetime
    upload_id: str | None = None
    # Operazioni già presenti prima del nostro upload (es. un tentativo fallito poco prima): da ignorare
    ignore_operations: frozenset[str] = frozenset()


class DeployBackend(Protocol):
    def describe_target(self) -> str: ...

    def list_apps(self) -> list[InstalledApp]: ...

    def upload_and_install(
        self, manifest: AppManifest, path: Path, schema_sync: SchemaSync, schedule: Schedule
    ) -> UploadHandle: ...

    def deploy_status(self, handle: UploadHandle) -> DeployStatus: ...
