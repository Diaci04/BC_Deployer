"""Generatore di pacchetti .app finti per i test (header NAVX + zip con NavxManifest.xml)."""
import io
import zipfile
from pathlib import Path

NS = "http://schemas.microsoft.com/navx/2015/manifest"

BASE_APP = "437dbf0e-84ff-417a-965d-ed2bb9650972"
APP_A = "aaaaaaaa-0000-0000-0000-000000000001"
APP_B = "bbbbbbbb-0000-0000-0000-000000000002"
APP_C = "cccccccc-0000-0000-0000-000000000003"


def manifest_xml(
    app_id: str, name: str, version: str = "1.0.0.0", publisher: str = "Contoso",
    deps: list[tuple] = (), application: str | None = "24.0.0.0", dep_attr: str = "Id",
) -> bytes:
    dep_xml = "".join(
        f'<Dependency {dep_attr}="{d[0]}" Name="{d[1]}" Publisher="{d[2]}" MinVersion="{d[3]}" />' for d in deps
    )
    app_attr = f' Application="{application}" Platform="24.0.0.0"' if application else ""
    return (
        f'<?xml version="1.0" encoding="utf-8"?><Package xmlns="{NS}">'
        f'<App Id="{app_id.upper()}" Name="{name}" Publisher="{publisher}" Version="{version}"'
        f' Runtime="13.0" Target="Cloud"{app_attr} />'
        f"<Dependencies>{dep_xml}</Dependencies></Package>"
    ).encode()


def app_bytes(manifest: bytes | None = None, *, magic: bool = True, **kw) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("NavxManifest.xml", manifest if manifest is not None else manifest_xml(**kw))
        z.writestr("src/Codeunit.al", "codeunit 50100 Test {}")
    header = (b"NAVX" if magic else b"XXXX") + b"\x28\x00\x00\x00" + b"\x00" * 32
    return header + buf.getvalue()


def write_app(path: Path, **kw) -> Path:
    path.write_bytes(app_bytes(**kw))
    return path
