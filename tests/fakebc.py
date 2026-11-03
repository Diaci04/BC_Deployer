"""Finto Business Central (Automation API) costruito su respx, con stato in memoria."""
import io
import json
import re
import uuid
import zipfile
from datetime import UTC, datetime

import httpx

from app.bc.manifest import parse_manifest_xml

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
COMPANY_ID = "c0000000-0000-0000-0000-000000000001"
TOKEN_URL = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"


def api_base(env: str = "Sandbox") -> str:
    return f"https://api.businesscentral.dynamics.com/v2.0/{TENANT}/{env}/api/microsoft/automation/v2.0"


class FakeBC:
    def __init__(self, mock, env: str = "Sandbox", installed: list[dict] | None = None,
                 outcomes: dict[str, str] | None = None):
        self.base = api_base(env)
        self.installed = installed or []  # dict: id, name, version
        self.outcomes = outcomes or {}  # nome app -> "Completed" | "Failed"
        self.uploads: dict[str, dict] = {}
        self.operations: list[dict] = []
        self.polls: dict[str, int] = {}
        self.calls: list[str] = []
        self.install_error: int | None = None  # se impostato, Microsoft.NAV.upload risponde con questo status
        self.install_attempts = 0

        c = f"{self.base}/companies({COMPANY_ID})"
        mock.post(TOKEN_URL).respond(json={"access_token": "tok", "expires_in": 3600})
        mock.get(f"{self.base}/companies").respond(json={"value": [{"id": COMPANY_ID, "name": "CRONUS IT"}]})
        mock.get(f"{c}/extensions").mock(side_effect=self._extensions)
        mock.get(f"{c}/extensionUpload").mock(side_effect=self._list_uploads)
        mock.post(f"{c}/extensionUpload").mock(side_effect=self._create_upload)
        esc = re.escape(c)
        mock.patch(url__regex=rf"^{esc}/extensionUpload\((?P<uid>[^)]+)\)$").mock(side_effect=self._update_upload)
        mock.patch(url__regex=rf"^{esc}/extensionUpload\((?P<uid>[^)]+)\)/extensionContent$").mock(
            side_effect=self._content
        )
        mock.post(url__regex=rf"^{esc}/extensionUpload\((?P<uid>[^)]+)\)/Microsoft\.NAV\.upload$").mock(
            side_effect=self._install
        )
        mock.get(f"{c}/extensionDeploymentStatus").mock(side_effect=self._status)

    def _extensions(self, request):
        value = []
        for a in self.installed:
            major, minor, build, rev = (int(x) for x in a["version"].split("."))
            value.append({
                "id": a["id"], "packageId": str(uuid.uuid4()), "displayName": a["name"], "publisher": "Contoso",
                "versionMajor": major, "versionMinor": minor, "versionBuild": build, "versionRevision": rev,
                "isInstalled": a.get("isInstalled", True), "publishedAs": "Dev",
            })
        return httpx.Response(200, json={"value": value})

    # Come in BC reale: un solo record extensionUpload per company (chiave Id=0), che resta dopo l'upload

    def add_existing_upload(self) -> str:
        uid = str(uuid.uuid4())
        self.uploads[uid] = {"body": {"schedule": "Current version", "schemaSyncMode": "Add"}, "content": b""}
        return uid

    def _list_uploads(self, request):
        return httpx.Response(200, json={"value": [{"systemId": uid, **u["body"]} for uid, u in self.uploads.items()]})

    def _create_upload(self, request):
        if self.uploads:
            self.calls.append("create-rejected")
            return httpx.Response(400, json={"error": {
                "code": "Internal_EntityWithSameKeyExists",
                "message": "The record in table API Extension Upload already exists. Identification fields and values: Id='0'",
            }})
        body = json.loads(request.content)
        uid = str(uuid.uuid4())
        self.uploads[uid] = {"body": body, "content": b""}
        self.calls.append("create")
        return httpx.Response(201, json={"systemId": uid, **body, "extensionContent": ""})

    def _update_upload(self, request, uid):
        assert request.headers["if-match"] == "*"
        if uid not in self.uploads:
            return httpx.Response(404, json={"error": {"code": "BadRequest_NotFound", "message": "not found"}})
        self.uploads[uid]["body"] = json.loads(request.content)
        self.calls.append("update")
        return httpx.Response(200, json={"systemId": uid, **self.uploads[uid]["body"]})

    def _content(self, request, uid):
        assert request.headers["content-type"] == "application/octet-stream"
        assert request.headers["if-match"] == "*"
        self.uploads[uid]["content"] = request.content
        self.calls.append("content")
        return httpx.Response(204)

    def _install(self, request, uid):
        self.install_attempts += 1
        if self.install_error:
            return httpx.Response(self.install_error, json={"error": {"code": "Internal", "message": "boom"}})
        raw = self.uploads[uid]["content"]
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            m = parse_manifest_xml(z.read("NavxManifest.xml"))
        op = {
            "operationID": str(uuid.uuid4()), "name": m.name, "publisher": m.publisher,
            "operationType": "Upload", "status": "InProgress", "schedule": self.uploads[uid]["body"]["schedule"],
            "appVersion": str(m.version), "startedOn": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "7Z",
        }
        self.operations.append(op)
        self.calls.append(f"install:{m.name}")
        return httpx.Response(204)

    def _status(self, request):
        # Alla seconda lettura ogni operazione passa all'esito configurato
        for op in self.operations:
            n = self.polls.get(op["operationID"], 0) + 1
            self.polls[op["operationID"]] = n
            if n >= 2 and op["status"] == "InProgress":
                op["status"] = self.outcomes.get(op["name"], "Completed")
                if op["status"] == "Completed":
                    self.installed = [a for a in self.installed if a["name"] != op["name"]]
                    self.installed.append({"id": "?", "name": op["name"], "version": op["appVersion"]})
        return httpx.Response(200, json={"value": list(self.operations)})
