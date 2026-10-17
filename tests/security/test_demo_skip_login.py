"""TEMPORARY demo switch `DEMO_SKIP_LOGIN`: skip the email code. Off by default and localhost
only (so its plain "no account" answer can't be used to probe from outside). Remove with it."""

from auth_helpers import PATIENT_A, csrf, new_client, request_code
from fastapi.testclient import TestClient

from hlidac.ui.login import SKIP_UNKNOWN

SKIP = "Přeskočit ověření (DEMO)"


def skip_app(app, on=True):
    app.state.settings = app.state.settings.model_copy(update={"demo_skip_login": on})
    return app


def skip(client):
    return client.post(
        "/prihlaseni/preskocit", data={"csrf_token": csrf(client, "/prihlaseni/kod")}
    )


def test_off_by_default_no_button_and_404(client):
    request_code(client, PATIENT_A.email)
    assert SKIP not in client.get("/prihlaseni/kod").text
    token = csrf(client, "/prihlaseni/kod")
    response = client.post("/prihlaseni/preskocit", data={"csrf_token": token})
    assert response.status_code == 404
    assert client.get("/pacient").status_code != 200


def test_on_logs_in_the_entered_email_and_audits_it(app, store):
    client = new_client(skip_app(app))
    request_code(client, PATIENT_A.email)
    assert SKIP in client.get("/prihlaseni/kod").text
    response = skip(client)
    assert response.status_code == 303 and response.headers["location"] == "/pacient"
    assert client.get("/pacient").status_code == 200
    logins = [e for e in store.list_audit() if e.action == "login"]
    assert logins and logins[-1].actor == PATIENT_A.account_id
    assert logins[-1].details.get("method") == "demo_skip"


def test_unknown_email_is_said_plainly(app):
    client = new_client(skip_app(app))
    request_code(client, "nobody@demo.local")
    response = skip(client)
    assert response.status_code == 400 and SKIP_UNKNOWN in response.text


def test_without_an_entered_email_it_goes_back_to_login(app):
    client = new_client(skip_app(app))
    token = csrf(client)
    response = client.post("/prihlaseni/preskocit", data={"csrf_token": token})
    assert response.status_code == 303 and response.headers["location"] == "/prihlaseni"


def test_refused_from_another_machine(app):
    client = TestClient(
        skip_app(app), base_url="https://testserver", follow_redirects=False,
        client=("203.0.113.7", 50000),
    )  # fmt: skip
    request_code(client, PATIENT_A.email)
    assert SKIP not in client.get("/prihlaseni/kod").text
    assert skip(client).status_code == 404
    assert client.get("/pacient").status_code != 200


def test_needs_csrf(app):
    client = new_client(skip_app(app))
    request_code(client, PATIENT_A.email)
    response = client.post("/prihlaseni/preskocit", data={})
    assert response.status_code in (400, 403)
