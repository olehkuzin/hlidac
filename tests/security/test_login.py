"""WP30: passwordless login with a one-time email code (D18)."""

import logging
import re
from datetime import timedelta

from auth_helpers import NURSE, PATIENT_A, csrf, login, new_client, request_code, submit_code

from hlidac.app import SESSION_COOKIE
from hlidac.security.codes import CODE_TTL, MAX_ATTEMPTS_PER_CODE, MAX_CODES_PER_WINDOW
from hlidac.ui.login import CODE_ERROR


def test_patient_logs_in_with_the_emailed_code(client, mailbox):
    response = request_code(client, "A@Demo.local ")  # case and spaces don't matter
    assert response.status_code == 303
    assert response.headers["location"] == "/prihlaseni/kod"
    assert [job.to for job in mailbox.sent] == [PATIENT_A.email]

    response = submit_code(client, mailbox.last_code())
    assert response.status_code == 303
    assert response.headers["location"] == "/pacient"
    page = client.get("/pacient")
    assert page.status_code == 200
    assert PATIENT_A.display_name in page.text


def test_nurse_lands_on_the_queue(client, mailbox):
    assert login(client, mailbox, NURSE.email).headers["location"] == "/sestra"
    assert client.get("/sestra").status_code == 200


def test_unknown_email_gets_the_same_answer_and_no_email(client, mailbox):
    response = request_code(client, "nikdo@demo.local")
    assert response.status_code == 303
    assert response.headers["location"] == "/prihlaseni/kod"
    assert mailbox.sent == []
    assert submit_code(client, "123456").status_code == 400


def test_malformed_email_is_explained_next_to_the_field(client, mailbox):
    response = request_code(client, "neni-email")
    assert response.status_code == 400
    assert 'aria-invalid="true"' in response.text
    assert 'aria-describedby="email-error"' in response.text
    assert mailbox.sent == []


def test_only_a_keyed_hash_of_the_code_is_stored(client, mailbox, store):
    request_code(client, PATIENT_A.email)
    code = mailbox.last_code()
    [stored] = store.list_login_codes(PATIENT_A.account_id)
    assert code not in stored.model_dump_json()
    assert len(stored.code_hash) == 64


def test_wrong_code_is_rejected(client, mailbox):
    request_code(client, PATIENT_A.email)
    wrong = "000000" if mailbox.last_code() != "000000" else "111111"
    response = submit_code(client, wrong)
    assert response.status_code == 400
    assert CODE_ERROR in response.text
    assert client.get("/pacient").status_code == 303  # still logged out


def test_code_is_locked_after_too_many_wrong_attempts(client, mailbox):
    request_code(client, PATIENT_A.email)
    code = mailbox.last_code()
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(MAX_ATTEMPTS_PER_CODE):
        submit_code(client, wrong)
    response = submit_code(client, code)
    assert response.status_code == 400
    assert CODE_ERROR in response.text


def test_expired_code_is_rejected(client, mailbox, security_clock):
    request_code(client, PATIENT_A.email)
    security_clock.advance(CODE_TTL)
    response = submit_code(client, mailbox.last_code())
    assert response.status_code == 400
    assert CODE_ERROR in response.text


def test_reused_code_is_rejected(app, client, mailbox):
    request_code(client, PATIENT_A.email)
    code = mailbox.last_code()
    other = new_client(app)
    other.cookies.update(client.cookies)  # a copy of the session before the code is spent
    assert submit_code(client, code).status_code == 303
    response = submit_code(other, code)
    assert response.status_code == 400
    assert CODE_ERROR in response.text


def test_only_the_newest_code_counts(client, mailbox):
    request_code(client, PATIENT_A.email)
    first = mailbox.last_code()
    request_code(client, PATIENT_A.email)
    if first != mailbox.last_code():
        assert submit_code(client, first).status_code == 400


def test_codes_per_account_are_rate_limited(client, mailbox, security_clock):
    for _ in range(MAX_CODES_PER_WINDOW + 2):
        assert request_code(client, PATIENT_A.email).status_code == 303  # same answer
    assert len(mailbox.sent) == MAX_CODES_PER_WINDOW
    security_clock.advance(timedelta(minutes=16))
    request_code(client, PATIENT_A.email)
    assert len(mailbox.sent) == MAX_CODES_PER_WINDOW + 1


def test_session_cookie_is_secure_httponly_and_strict(client, mailbox):
    response = request_code(client, PATIENT_A.email)
    cookie = response.headers["set-cookie"].lower()
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    for flag in ("httponly", "secure", "samesite=strict"):
        assert flag in cookie


def test_login_rotates_the_session(client, mailbox):
    before = csrf(client)
    login(client, mailbox, PATIENT_A.email)
    assert csrf(client, "/pacient") != before


def test_logout_ends_the_session(client, mailbox):
    login(client, mailbox, PATIENT_A.email)
    response = client.post("/odhlaseni", data={"csrf_token": csrf(client, "/pacient")})
    assert response.headers["location"] == "/prihlaseni"
    assert client.get("/pacient").status_code == 303


def test_no_email_or_code_in_urls_or_logs(client, mailbox, caplog):
    caplog.set_level(logging.DEBUG)
    locations = [request_code(client, PATIENT_A.email).headers["location"]]
    code = mailbox.last_code()
    locations.append(submit_code(client, code).headers["location"])
    assert locations == ["/prihlaseni/kod", "/pacient"]
    assert PATIENT_A.email not in caplog.text
    assert code not in caplog.text


def test_logins_are_audited_without_the_email(client, mailbox, store):
    request_code(client, PATIENT_A.email)
    submit_code(client, "999999" if mailbox.last_code() != "999999" else "888888")
    submit_code(client, mailbox.last_code())
    entries = store.list_audit()
    assert [(e.actor, e.action) for e in entries] == [
        (PATIENT_A.account_id, "login_code_sent"),
        (PATIENT_A.account_id, "login_failed"),
        (PATIENT_A.account_id, "login"),
    ]
    assert all(PATIENT_A.email not in e.model_dump_json() for e in entries)


def _page(response) -> str:
    return re.sub(r'value="[^"]*"', "", response.text)  # drop the per-session CSRF token


def test_registered_and_unknown_emails_look_the_same(app, mailbox, security_clock):
    """Nobody can learn who is a patient from the code step: wrong, locked or expired."""
    known, unknown = new_client(app), new_client(app)
    request_code(known, PATIENT_A.email)
    request_code(unknown, "nikdo@demo.local")
    wrong = "000000" if mailbox.last_code() != "000000" else "111111"
    for _ in range(MAX_ATTEMPTS_PER_CODE + 1):
        a, b = submit_code(known, wrong), submit_code(unknown, wrong)
        assert (a.status_code, _page(a)) == (b.status_code, _page(b))
    security_clock.advance(CODE_TTL)
    a, b = submit_code(known, wrong), submit_code(unknown, wrong)
    assert (a.status_code, _page(a)) == (b.status_code, _page(b))


def test_new_code_without_typing_the_email_again(client, mailbox):
    request_code(client, PATIENT_A.email)
    first = mailbox.last_code()
    token = csrf(client, "/prihlaseni/kod")
    response = client.post("/prihlaseni/novy-kod", data={"csrf_token": token})
    assert response.headers["location"] == "/prihlaseni/kod"
    assert len(mailbox.sent) == 2
    assert mailbox.sent[1].to == PATIENT_A.email
    if mailbox.last_code() != first:
        assert submit_code(client, first).status_code == 400
    assert submit_code(client, mailbox.last_code()).headers["location"] == "/pacient"


def test_new_code_for_an_unknown_email_sends_nothing(client, mailbox):
    request_code(client, "nikdo@demo.local")
    token = csrf(client, "/prihlaseni/kod")
    response = client.post("/prihlaseni/novy-kod", data={"csrf_token": token})
    assert response.headers["location"] == "/prihlaseni/kod"
    assert mailbox.sent == []


def test_code_page_states_the_limits(client, mailbox):
    request_code(client, PATIENT_A.email)
    page = client.get("/prihlaseni/kod").text
    assert "10 minut" in page
    assert "3krát za 15 minut" in page


def test_sent_login_code_does_not_stay_in_the_outbox(client, mailbox, store):
    request_code(client, PATIENT_A.email)
    code = mailbox.last_code()
    [row] = store.list_outbox()
    assert row.status == "done"
    assert code not in row.model_dump_json()
