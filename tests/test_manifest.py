import zipfile

import pytest

from app.bc.manifest import ManifestError, parse_manifest_xml, read_manifest
from app.bc.version import Version

from .apps import APP_A, APP_B, BASE_APP, app_bytes, manifest_xml, write_app


def test_reads_app_with_navx_header(tmp_path):
    p = write_app(
        tmp_path / "a.app", app_id=APP_A, name="App A", version="2.1.0.5",
        deps=[(APP_B, "App B", "Contoso", "1.2.0.0"), (BASE_APP, "Base Application", "Microsoft", "24.0.0.0")],
    )
    m = read_manifest(p)
    assert m.id == APP_A  # normalizzato in minuscolo
    assert (m.name, m.publisher, m.version) == ("App A", "Contoso", Version.parse("2.1.0.5"))
    assert [d.id for d in m.dependencies] == [APP_B, BASE_APP]
    assert m.dependencies[0].min_version == Version.parse("1.2")
    assert m.application == Version.parse("24.0.0.0") and m.platform == Version.parse("24")
    assert m.runtime == "13.0" and m.target == "Cloud"


def test_legacy_appid_attribute():
    m = parse_manifest_xml(manifest_xml(APP_A, "A", deps=[(APP_B, "B", "X", "1.0.0.0")], dep_attr="AppId"))
    assert m.dependencies[0].id == APP_B


def test_roundtrip_dict():
    m = parse_manifest_xml(manifest_xml(APP_A, "A", deps=[(APP_B, "B", "X", "1.0.0.0")]))
    from app.bc.manifest import AppManifest

    assert AppManifest.from_dict(m.to_dict()) == m


def test_missing_navx_header(tmp_path):
    p = tmp_path / "x.app"
    p.write_bytes(app_bytes(app_id=APP_A, name="A", magic=False))
    with pytest.raises(ManifestError, match="header NAVX"):
        read_manifest(p)


def test_not_a_zip(tmp_path):
    p = tmp_path / "x.app"
    p.write_bytes(b"NAVX" + b"garbage" * 100)
    with pytest.raises(ManifestError, match="non leggibile"):
        read_manifest(p)


def test_zip_without_manifest(tmp_path):
    p = tmp_path / "x.app"
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("other.txt", "x")
    p.write_bytes(b"NAVX" + b"\x00" * 36 + buf.getvalue())
    with pytest.raises(ManifestError, match="non trovato"):
        read_manifest(p)


@pytest.mark.parametrize(
    "xml, match",
    [
        (b"<Package><App", "non valido"),
        (b"<Package/>", "senza elemento App"),
        (b'<Package><App Id="x" Name="n" Version="1.0.0.0"/></Package>', "Publisher"),
        (b'<Package><App Id="x" Name="n" Publisher="p" Version="uno"/></Package>', "Versione non valida"),
    ],
)
def test_invalid_manifests(xml, match):
    with pytest.raises(ManifestError, match=match):
        parse_manifest_xml(xml)


def test_xml_entity_expansion_is_rejected():
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]>'
        b'<Package><App Id="x" Name="&lol2;" Publisher="p" Version="1.0.0.0"/></Package>'
    )
    with pytest.raises(ManifestError):
        parse_manifest_xml(bomb)


def test_version_comparison_is_numeric():
    assert Version.parse("1.10.0.0") > Version.parse("1.9.0.0")
    assert Version.parse("2") == Version.parse("2.0.0.0")
    assert str(Version.parse("1.2")) == "1.2.0.0"
    for bad in ("", "1.2.3.4.5", "a.b", "-1"):
        with pytest.raises(ValueError):
            Version.parse(bad)
