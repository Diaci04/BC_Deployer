"""Lettura di NavxManifest.xml da un pacchetto .app.

Un .app è uno zip preceduto da un header "NAVX" di 40 byte: zipfile lo apre comunque
perché individua l'archivio partendo dalla fine del file.

Rispetto a read_manifest di bc_deploy.py:
- controlla l'header NAVX e dà errori leggibili per file non validi;
- legge MinVersion, Name e Publisher delle dipendenze (attributo Id o, nei manifest
  vecchi, AppId) e le versioni minime di Application e Platform;
- usa defusedxml e un limite di dimensione contro XML/zip malevoli.
"""
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException

from .version import Version

NAVX_MAGIC = b"NAVX"
MAX_MANIFEST_BYTES = 5 * 1024 * 1024


class ManifestError(Exception):
    pass


@dataclass(frozen=True)
class Dependency:
    id: str
    name: str
    publisher: str
    min_version: Version


@dataclass
class AppManifest:
    id: str
    name: str
    publisher: str
    version: Version
    dependencies: list[Dependency] = field(default_factory=list)
    application: Version | None = None  # versione minima della Base Application
    platform: Version | None = None
    runtime: str | None = None
    target: str | None = None

    @property
    def label(self) -> str:
        return f"{self.name} {self.version}"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "publisher": self.publisher,
            "version": str(self.version),
            "application": str(self.application) if self.application else None,
            "platform": str(self.platform) if self.platform else None,
            "runtime": self.runtime,
            "target": self.target,
            "dependencies": [
                {"id": d.id, "name": d.name, "publisher": d.publisher, "min_version": str(d.min_version)}
                for d in self.dependencies
            ],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AppManifest":
        return cls(
            id=d["id"],
            name=d["name"],
            publisher=d["publisher"],
            version=Version.parse(d["version"]),
            dependencies=[
                Dependency(x["id"], x["name"], x["publisher"], Version.parse(x["min_version"]))
                for x in d.get("dependencies", [])
            ],
            application=Version.parse(d["application"]) if d.get("application") else None,
            platform=Version.parse(d["platform"]) if d.get("platform") else None,
            runtime=d.get("runtime"),
            target=d.get("target"),
        )


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _version_attr(el, *names: str, required: bool = False) -> Version | None:
    for n in names:
        if el.attrib.get(n):
            return Version.parse(el.attrib[n])
    if required:
        raise ManifestError(f"Attributo {names[0]} mancante")
    return None


def parse_manifest_xml(raw: bytes) -> AppManifest:
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, DefusedXmlException) as e:
        raise ManifestError(f"NavxManifest.xml non valido: {e}") from None

    app_el = next((e for e in root.iter() if _local(e.tag) == "App"), None)
    if app_el is None:
        raise ManifestError("NavxManifest.xml senza elemento App")
    try:
        app_id = app_el.attrib["Id"].strip().lower()
        name = app_el.attrib["Name"].strip()
        publisher = app_el.attrib["Publisher"].strip()
        version = Version.parse(app_el.attrib["Version"])
    except KeyError as e:
        raise ManifestError(f"Attributo {e.args[0]} mancante nell'elemento App") from None
    except ValueError as e:
        raise ManifestError(str(e)) from None

    deps = []
    for d in root.iter():
        if _local(d.tag) != "Dependency":
            continue
        dep_id = (d.attrib.get("Id") or d.attrib.get("AppId") or "").strip().lower()
        if not dep_id:
            raise ManifestError("Dipendenza senza Id nel manifest")
        try:
            min_v = _version_attr(d, "MinVersion", "Version") or Version.parse("0")
        except ValueError as e:
            raise ManifestError(str(e)) from None
        deps.append(Dependency(dep_id, d.attrib.get("Name", ""), d.attrib.get("Publisher", ""), min_v))

    try:
        application = _version_attr(app_el, "Application")
        platform = _version_attr(app_el, "Platform")
    except ValueError as e:
        raise ManifestError(str(e)) from None

    return AppManifest(
        id=app_id,
        name=name,
        publisher=publisher,
        version=version,
        dependencies=deps,
        application=application,
        platform=platform,
        runtime=app_el.attrib.get("Runtime"),
        target=app_el.attrib.get("Target"),
    )


def read_manifest(path: Path) -> AppManifest:
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
    except OSError as e:
        raise ManifestError(f"File non leggibile: {e.strerror}") from None
    if magic != NAVX_MAGIC:
        raise ManifestError("Non è un pacchetto .app di Business Central (header NAVX mancante)")
    try:
        with zipfile.ZipFile(path) as z:
            info = next((i for i in z.infolist() if i.filename.lower() == "navxmanifest.xml"), None)
            if info is None:
                raise ManifestError("NavxManifest.xml non trovato nel pacchetto")
            if info.file_size > MAX_MANIFEST_BYTES:
                raise ManifestError("NavxManifest.xml troppo grande")
            raw = z.read(info)
    except zipfile.BadZipFile:
        raise ManifestError(
            "Pacchetto .app non leggibile (archivio corrotto, oppure app protetta/runtime package)"
        ) from None
    return parse_manifest_xml(raw)
