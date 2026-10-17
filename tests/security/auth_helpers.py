"""WP30 test helpers: invented accounts, a captured mailbox and login steps. No patient data."""

import re

from fastapi.testclient import TestClient

from hlidac.schemas import Account, EmailJob

PATIENT_A = Account(
    account_id="acc-a", email="a@demo.local", role="patient", display_name="A", patient_id="pa"
)
PATIENT_B = Account(
    account_id="acc-b", email="b@demo.local", role="patient", display_name="B", patient_id="pb"
)
PATIENT_C = Account(  # cared for by another team
    account_id="acc-c", email="c@demo.local", role="patient", display_name="C", patient_id="pc"
)
NURSE = Account(
    account_id="acc-n", email="sestra@demo.local", role="nurse", display_name="N", team_id="t1"
)


class Mailbox:
    def __init__(self) -> None:
        self.sent: list[EmailJob] = []

    def __call__(self, job: EmailJob) -> None:
        self.sent.append(job)

    def last_code(self) -> str:
        match = re.search(r"\b(\d{6})\b", self.sent[-1].text_body)
        assert match, "the login email carries a 6-digit code"
        return match.group(1)


def new_client(app) -> TestClient:
    # https: the session cookie is Secure, so a client on http would never send it back.
    return TestClient(app, base_url="https://testserver", follow_redirects=False)


def csrf(client: TestClient, page: str = "/prihlaseni") -> str:
    html = client.get(page).text
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, f"{page} has a CSRF field"
    return match.group(1)


def request_code(client: TestClient, email: str):
    return client.post("/prihlaseni", data={"email": email, "csrf_token": csrf(client)})


def submit_code(client: TestClient, code: str):
    token = csrf(client, "/prihlaseni/kod")
    return client.post("/prihlaseni/kod", data={"code": code, "csrf_token": token})


def login(client: TestClient, mailbox: Mailbox, email: str):
    request_code(client, email)
    response = submit_code(client, mailbox.last_code())
    assert response.status_code == 303, response.text
    return response
