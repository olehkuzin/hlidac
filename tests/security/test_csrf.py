"""WP30: every state-changing request needs this session's CSRF token."""

from auth_helpers import PATIENT_A, csrf, login, new_client


def test_form_without_token_is_rejected(client, mailbox):
    response = client.post("/prihlaseni", data={"email": PATIENT_A.email})
    assert response.status_code == 403
    assert mailbox.sent == []


def test_form_with_a_wrong_token_is_rejected(client, mailbox):
    csrf(client)  # the session has a token, but the form sends another
    response = client.post("/prihlaseni", data={"email": PATIENT_A.email, "csrf_token": "x" * 43})
    assert response.status_code == 403
    assert mailbox.sent == []


def test_token_from_another_session_is_rejected(app, client, mailbox):
    attacker = new_client(app)
    token = csrf(attacker)
    csrf(client)
    response = client.post("/prihlaseni", data={"email": PATIENT_A.email, "csrf_token": token})
    assert response.status_code == 403


def test_logout_needs_the_token(client, mailbox):
    login(client, mailbox, PATIENT_A.email)
    assert client.post("/odhlaseni").status_code == 403
    assert client.get("/pacient").status_code == 200  # still logged in


def test_token_in_header_is_accepted_for_scripts(client, mailbox):
    login(client, mailbox, PATIENT_A.email)
    token = csrf(client, "/pacient")
    response = client.post("/odhlaseni", headers={"X-CSRF-Token": token})
    assert response.status_code == 303


def test_rejection_page_is_accessible_czech(client):
    response = client.post("/prihlaseni", data={"email": PATIENT_A.email})
    assert '<html lang="cs">' in response.text
    assert response.text.count("<h1>") == 1
