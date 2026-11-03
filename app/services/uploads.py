"""Ricezione dei pacchetti .app: streaming su disco, limite di dimensione, validazione del manifest."""
import hashlib
import json
import logging
import re
import unicodedata
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..bc.manifest import AppManifest, ManifestError, read_manifest
from ..config import Settings
from ..models import AppFile, utcnow

log = logging.getLogger(__name__)

# Un file caricato ma mai usato in un deploy viene eliminato dopo questo tempo
STAGED_TTL = timedelta(hours=24)


class UploadError(Exception):
    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


def sanitize_filename(name: str) -> str:
    """Nome solo per la visualizzazione: su disco il file ha sempre un nome generato."""
    name = unicodedata.normalize("NFKC", name or "")
    name = re.split(r"[\\/]", name)[-1]  # niente percorsi
    name = re.sub(r"[^\w.\- ()]", "_", name, flags=re.ASCII).strip(" .")
    name = re.sub(r"_+", "_", name)[:150]
    return name or "pacchetto.app"


def file_path(settings: Settings, f: AppFile) -> Path:
    return settings.upload_dir / f.stored_name


def manifest_of(f: AppFile) -> AppManifest:
    return AppManifest.from_dict(json.loads(f.manifest_json))


async def receive_upload(
    db: Session, settings: Settings, filename: str, chunks: AsyncIterator[bytes], content_length: int | None
) -> AppFile:
    display_name = sanitize_filename(filename)
    if not display_name.lower().endswith(".app"):
        raise UploadError("Sono accettati solo file .app")
    max_bytes = settings.max_app_size_mb * 1024 * 1024
    if content_length is not None and content_length > max_bytes:
        raise UploadError(f"File troppo grande (massimo {settings.max_app_size_mb} MB)", 413)

    stored = f"{uuid.uuid4().hex}.app"
    tmp = settings.upload_dir / f"{stored}.part"
    sha = hashlib.sha256()
    size = 0
    try:
        with open(tmp, "wb") as out:
            async for chunk in chunks:
                size += len(chunk)
                if size > max_bytes:
                    raise UploadError(f"File troppo grande (massimo {settings.max_app_size_mb} MB)", 413)
                sha.update(chunk)
                out.write(chunk)
        if size == 0:
            raise UploadError("File vuoto")
        try:
            manifest = await run_in_threadpool(read_manifest, tmp)
        except ManifestError as e:
            raise UploadError(f"{display_name}: {e}") from None
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    digest = sha.hexdigest()
    existing = db.scalars(
        select(AppFile).where(AppFile.sha256 == digest, AppFile.status == "staged")
    ).first()
    if existing and file_path(settings, existing).exists():
        tmp.unlink(missing_ok=True)
        existing.expires_at = utcnow() + STAGED_TTL
        db.commit()
        return existing

    tmp.rename(settings.upload_dir / stored)
    f = AppFile(
        sha256=digest,
        original_name=display_name,
        stored_name=stored,
        size=size,
        app_id=manifest.id,
        name=manifest.name[:200],
        publisher=manifest.publisher[:200],
        version=str(manifest.version),
        manifest_json=json.dumps(manifest.to_dict()),
        status="staged",
        expires_at=utcnow() + STAGED_TTL,
    )
    db.add(f)
    db.commit()
    log.info("Caricato %s (%s %s, %d byte)", display_name, manifest.name, manifest.version, size)
    return f


def delete_file(db: Session, settings: Settings, f: AppFile) -> None:
    file_path(settings, f).unlink(missing_ok=True)
    f.status = "deleted"
    f.expires_at = None
