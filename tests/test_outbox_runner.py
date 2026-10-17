"""WP30: the dev outbox runner keeps the outbox contract; the login email has no clinical content.
The store tests also run on IrisStore with HLIDAC_IRIS_TESTS=1 (WP10, conftest.py)."""

import threading
import typing

import pytest
from factories import T0, email_item, fhir_item

import hlidac.outbox_runner as runner
from hlidac import emails
from hlidac.outbox_runner import run_once
from hlidac.schemas import MAX_ATTEMPTS, REDACTED, OutboxItem


class Refused(Exception):
    pass


def refuse(job):
    raise Refused("sestra@demo.local refused")


def test_email_job_is_sent_and_done(store):
    sent = []
    store.enqueue(email_item())
    assert run_once(store, T0, sent.append) == 1
    assert len(sent) == 1
    [row] = store.list_outbox()
    assert row.status == "done"
    assert row.processed_at == T0


def test_failures_are_retried_then_failed_without_the_address(store):
    store.enqueue(email_item())
    for _ in range(MAX_ATTEMPTS):
        run_once(store, T0, refuse)
    [row] = store.list_outbox()
    assert row.status == "failed"
    assert row.attempts == MAX_ATTEMPTS
    assert row.last_error == "Refused"
    run_once(store, T0, refuse)
    assert store.list_outbox()[0].attempts == MAX_ATTEMPTS  # failed rows are left alone


def test_poller_retries_until_the_mail_server_answers(monkeypatch, store):
    monkeypatch.setattr(runner, "POLL_SECONDS", 0.01)
    sent, calls = [], []

    def flaky(job):
        calls.append(job)
        if len(calls) == 1:
            raise ConnectionRefusedError  # e.g. Mailpit still starting
        sent.append(job)

    store.enqueue(email_item())
    stop = runner.start_poller(store, lambda: T0, flaky)
    try:
        for _ in range(200):
            if store.list_outbox()[0].status == "done":
                break
            threading.Event().wait(0.01)
    finally:
        stop.set()
    assert store.list_outbox()[0].status == "done"
    assert len(sent) == 1


def test_fhir_jobs_are_left_for_the_production(store):
    sent = []
    store.enqueue(fhir_item())
    assert run_once(store, T0, sent.append) == 0
    assert store.list_outbox()[0].status == "pending"


def test_update_changes_only_status_columns(store):
    store.enqueue(email_item())
    row = store.list_outbox()[0]
    with pytest.raises(ValueError):
        store.update_outbox_item(row.evolve(idempotency_key="other"))
    with pytest.raises(KeyError):
        store.update_outbox_item(email_item(outbox_id="missing", key="k2"))


def test_login_email_carries_only_the_code():
    job = emails.render("login-code", "a@demo.local", code="123456")
    assert "123456" in job.text_body
    assert job.template_id == "login-code"
    assert '<html lang="cs">' in job.html_body
    assert job.text_body.count("\n\n") == 4  # the five approved paragraphs, nothing else
    assert "<title>" in job.html_body


@pytest.mark.parametrize("value", ["<b>1</b>", "12345", "Pacient A"])
def test_email_values_are_restricted(value):
    with pytest.raises(ValueError):
        emails.render("login-code", "a@demo.local", code=value)


def _login_email_item() -> OutboxItem:
    job = emails.render("login-code", "a@demo.local", code="123456")
    return OutboxItem(outbox_id="o3", idempotency_key="login-code:o3", created_at=T0, job=job)


def test_login_code_is_redacted_once_sent(store):
    store.enqueue(_login_email_item())
    run_once(store, T0, lambda job: None)
    [row] = store.list_outbox()
    assert row.status == "done"
    assert row.job.text_body == row.job.html_body == REDACTED
    assert "123456" not in row.model_dump_json()


def test_login_code_is_redacted_once_failed_but_kept_while_retrying(store):
    store.enqueue(_login_email_item())
    run_once(store, T0, refuse)
    assert "123456" in store.list_outbox()[0].job.text_body  # still needed for the retry
    for _ in range(MAX_ATTEMPTS - 1):
        run_once(store, T0, refuse)
    [row] = store.list_outbox()
    assert row.status == "failed"
    assert "123456" not in row.model_dump_json()


def test_other_emails_keep_their_body(store):
    store.enqueue(email_item())
    run_once(store, T0, lambda job: None)
    assert store.list_outbox()[0].job.text_body != REDACTED


def test_store_allows_only_a_redaction_of_a_finished_email(store):
    store.enqueue(_login_email_item())
    row = store.list_outbox()[0]
    with pytest.raises(ValueError):  # not while pending: the job still has to be sent
        store.update_outbox_item(row.evolve(job=row.job.redacted().model_dump()))
    with pytest.raises(ValueError):  # never any other change
        changed = row.job.evolve(subject="Jiný předmět")
        store.update_outbox_item(
            row.evolve(status="done", processed_at=T0, job=changed.model_dump())
        )


class FakeSmtp:
    calls: typing.ClassVar[list] = []

    def __init__(self, host, port, timeout):
        FakeSmtp.calls.append(("connect", host, port))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        FakeSmtp.calls.append(("starttls",))

    def login(self, user, password):
        FakeSmtp.calls.append(("login", user))

    def send_message(self, message):
        FakeSmtp.calls.append(("send", message["To"]))


def test_real_smtp_sends_only_to_allowed_addresses(monkeypatch):
    monkeypatch.setattr(runner.smtplib, "SMTP", FakeSmtp)
    FakeSmtp.calls = []
    send = runner.smtp_sender(
        "smtp.example", 587, "from@example.org", "user", "pw", frozenset({"allowed@example.org"})
    )
    with pytest.raises(runner.RecipientNotAllowed):
        send(email_item().job.evolve(to="nurse@example.org"))
    assert FakeSmtp.calls == []  # refused before connecting
    send(email_item().job.evolve(to="Allowed@example.org"))
    assert FakeSmtp.calls == [
        ("connect", "smtp.example", 587),
        ("starttls",),
        ("login", "user"),
        ("send", "Allowed@example.org"),
    ]


def test_a_star_allows_every_address(monkeypatch):
    monkeypatch.setattr(runner.smtplib, "SMTP", FakeSmtp)
    FakeSmtp.calls = []
    send = runner.smtp_sender("smtp.example", 587, "f@example.test", "user", "pw", frozenset("*"))
    send(email_item().job.evolve(to="nurse@example.org"))
    assert FakeSmtp.calls[-1] == ("send", "nurse@example.org")


def test_real_smtp_needs_an_allow_list():
    with pytest.raises(ValueError):
        runner.smtp_sender("smtp.example", 587, "from@example.test", "user", "pw", None)


def test_real_smtp_never_sends_to_reserved_addresses(monkeypatch, store):
    monkeypatch.setattr(runner.smtplib, "SMTP", FakeSmtp)
    FakeSmtp.calls = []
    send = runner.smtp_sender("smtp.example", 587, "f@example.test", "user", "pw", frozenset("*"))
    store.enqueue(email_item())  # to sestra@demo.local
    assert run_once(store, T0, send) == 0
    assert FakeSmtp.calls == []  # never connected
    [row] = store.list_outbox()
    assert (row.status, row.attempts, row.last_error) == ("done", 0, "Undeliverable")
    assert store.list_outbox_failures() == []  # not shown in Unresolved


def test_mailpit_still_gets_the_demo_addresses(monkeypatch):
    monkeypatch.setattr(runner.smtplib, "SMTP", FakeSmtp)
    FakeSmtp.calls = []
    runner.smtp_sender("localhost", 1025, "f@demo.local")(email_item().job)  # no login: Mailpit
    assert FakeSmtp.calls[-1] == ("send", "sestra@demo.local")
