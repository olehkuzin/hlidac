"""WP30: per-patient and per-role access control."""

from typing import Annotated

import pytest
from auth_helpers import NURSE, PATIENT_A, PATIENT_B, PATIENT_C, login
from fastapi import APIRouter, Depends, Request
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient

from hlidac.app import create_app
from hlidac.config import Settings
from hlidac.schemas import Account
from hlidac.security.access import current_account, ensure_patient_access

# "/prihlaseni/preskocit" is the TEMPORARY demo skip (404 unless DEMO_SKIP_LOGIN and localhost;
# tests/security/test_demo_skip_login.py). Remove it from here with the switch.
PUBLIC_ROUTES = {
    "/api/health",
    "/prihlaseni",
    "/prihlaseni/kod",
    "/prihlaseni/novy-kod",
    "/prihlaseni/preskocit",
}


@pytest.fixture
def probe(app):
    """A route shaped like the WP9/WP13 ones: it reads one patient's data after the access check."""
    router = APIRouter()

    @router.get("/test/patients/{patient_id}")
    def read(
        request: Request, patient_id: str, account: Annotated[Account, Depends(current_account)]
    ) -> dict:
        ensure_patient_access(account, patient_id, request.app.state.store)
        return {"patient_id": patient_id}

    app.include_router(router)


@pytest.mark.parametrize("path", ["/", "/pacient", "/sestra"])
def test_pages_need_a_login(client, path):
    response = client.get(path)
    assert response.status_code == 303
    assert response.headers["location"] == "/prihlaseni"


def test_patient_cannot_open_the_nurse_queue(client, mailbox):
    login(client, mailbox, PATIENT_A.email)
    assert client.get("/sestra").status_code == 403


def test_nurse_cannot_open_the_patient_app(client, mailbox):
    login(client, mailbox, NURSE.email)
    assert client.get("/pacient").status_code == 403


def test_patient_a_cannot_read_patient_b(probe, client, mailbox):
    login(client, mailbox, PATIENT_A.email)
    assert client.get(f"/test/patients/{PATIENT_A.patient_id}").status_code == 200
    response = client.get(f"/test/patients/{PATIENT_B.patient_id}")
    assert response.status_code == 403
    assert PATIENT_B.patient_id not in response.text


def test_nurse_reads_only_their_teams_patients(probe, client, mailbox):
    login(client, mailbox, NURSE.email)
    assert client.get(f"/test/patients/{PATIENT_A.patient_id}").status_code == 200
    assert client.get(f"/test/patients/{PATIENT_B.patient_id}").status_code == 200
    assert client.get(f"/test/patients/{PATIENT_C.patient_id}").status_code == 403  # team t2
    assert client.get("/test/patients/unknown").status_code == 403


def test_removed_account_loses_access(client, mailbox, store):
    login(client, mailbox, PATIENT_A.email)
    store._accounts.pop(PATIENT_A.account_id)  # the store has no delete; simulate a removal
    assert client.get("/pacient").status_code == 303


def _requires_login(dependant) -> bool:
    return any(
        getattr(d.call, "requires_login", False) or _requires_login(d)
        for d in dependant.dependencies
    )


def test_every_route_except_login_requires_a_login():
    app = create_app(Settings(_env_file=None))
    routes = [r for r in iter_route_contexts(app.routes) if hasattr(r, "dependant")]
    login_needed = {r.path: _requires_login(r.dependant) for r in routes}
    assert {"/pacient", "/sestra", "/odhlaseni"} <= login_needed.keys()
    assert not login_needed["/api/health"]  # the check does tell the two apart
    unprotected = [p for p, needed in login_needed.items() if p not in PUBLIC_ROUTES and not needed]
    assert unprotected == []


def test_unexpected_errors_show_a_czech_page_without_details(app):
    @app.get("/test/boom")
    def boom() -> None:
        raise RuntimeError("internal detail")

    client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    response = client.get("/test/boom")
    assert response.status_code == 500
    assert '<html lang="cs">' in response.text
    assert "internal detail" not in response.text


def test_api_docs_are_not_published(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
