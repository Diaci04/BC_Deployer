"""Notifiche email: impostazioni SMTP, invio di prova, email a fine deploy."""
import smtplib

import pytest

from app.models import SmtpSettings
from app.services import mail
from app.services.mail import parse_emails

from .test_deploy_flow import B, get_run, upload_ok
from .test_notify import bc, client, make_conn, run_logs  # noqa: F401  (fixture)
from .fakebc import FakeBC

SMTP_PW = "PasswordSmtpSegreta!"


class FakeSMTP:
    """Sostituisce smtplib.SMTP: registra i messaggi o simula un errore."""

    sent: list = []
    fail: Exception | None = None
    logins: list = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        FakeSMTP.logins.append((user, password))
        if FakeSMTP.fail:
            raise FakeSMTP.fail

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.sent, FakeSMTP.fail, FakeSMTP.logins = [], None, []
    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def save_smtp(client, **over):
    data = {"host": "smtp.office365.com", "port": "587", "security": "starttls",
            "username": "deploy@azienda.it", "password": SMTP_PW, "from_addr": "deploy@azienda.it", **over}
    return client.post("/settings/smtp", data=data, follow_redirects=False)


def test_parse_emails():
    assert parse_emails("a@x.it; b@y.com\nA@x.it") == (["a@x.it", "b@y.com"], None)
    assert parse_emails("")[0] == []
    assert parse_emails("nope")[1].startswith("Indirizzo non valido")


def test_smtp_password_is_encrypted_and_never_shown(client, app):
    assert save_smtp(client).status_code == 303
    with app.state.sessionmaker() as db:
        s = db.get(SmtpSettings, 1)
        assert SMTP_PW not in s.password_enc and app.state.secret_box.decrypt(s.password_enc) == SMTP_PW
    page = client.get("/settings").text
    assert SMTP_PW not in page and "salvata" in page and "smtp.office365.com" in page
    # Salvando senza password resta quella di prima
    save_smtp(client, password="", port="25")
    with app.state.sessionmaker() as db:
        s = db.get(SmtpSettings, 1)
        assert s.port == 25 and app.state.secret_box.decrypt(s.password_enc) == SMTP_PW


def test_smtp_validation(client):
    r = save_smtp(client, host="smtp office", port="99999", from_addr="no")
    assert r.status_code == 422 and "Porta non valida" in r.text and SMTP_PW not in r.text


def test_test_email_and_auth_error_without_password(client, fake_smtp):
    save_smtp(client)
    r = client.post("/settings/smtp/test", data={"to": "io@azienda.it"})
    assert "Email di prova inviata a io@azienda.it" in r.text
    assert fake_smtp.sent[-1]["To"] == "io@azienda.it" and fake_smtp.logins[-1] == ("deploy@azienda.it", SMTP_PW)
    fake_smtp.fail = smtplib.SMTPAuthenticationError(535, b"5.7.139 Authentication unsuccessful")
    r = client.post("/settings/smtp/test", data={"to": "io@azienda.it"})
    assert "autenticazione rifiutata" in r.text and "535" in r.text and SMTP_PW not in r.text


def test_email_after_failed_deploy(client, app, bc, fake_smtp):  # noqa: F811
    save_smtp(client)
    conn_id = make_conn(client, app, notify_emails="team@azienda.it, capo@azienda.it", notify_on="failure")
    fake = FakeBC(bc)
    client.post("/deploy", data={"connection_id": conn_id, "file_ids": [upload_ok(client, **B)]})
    assert fake_smtp.sent == []  # riuscito, e si notificano solo gli errori
    fake.outcomes["App B"] = "Failed"
    client.post("/deploy", data={"connection_id": conn_id, "file_ids": [upload_ok(client, **{**B, "version": "2.0.0.0"})]})
    msg = fake_smtp.sent[-1]
    assert "Fallito" in msg["Subject"] and msg["To"] == "team@azienda.it, capo@azienda.it"
    html = msg.get_body(("html",)).get_content()
    assert "App B" in html and "FALLITA" in html
    assert "Email inviata a team@azienda.it, capo@azienda.it" in run_logs(app, get_run(app).id)


def test_email_without_smtp_is_logged(client, app, bc):  # noqa: F811
    conn_id = make_conn(client, app, notify_emails="team@azienda.it")
    FakeBC(bc)
    client.post("/deploy", data={"connection_id": conn_id, "file_ids": [upload_ok(client, **B)]})
    run = get_run(app)
    assert run.status == "succeeded"
    assert any("server SMTP non configurato" in m for m in run_logs(app, run.id))


def test_connection_test_email_button(client, app):
    conn_id = make_conn(client, app, notify_emails="team@azienda.it")
    assert "Server SMTP non configurato" in client.post(f"/connections/{conn_id}/test-email").text
    save_smtp(client)
    assert "Email di prova inviata a team@azienda.it" in client.post(f"/connections/{conn_id}/test-email").text
