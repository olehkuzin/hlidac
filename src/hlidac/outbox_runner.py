"""The outbox runner: the only email sender (D40, OUTBOX_RUNNER=app); the IRIS production sends none.

It follows the outbox contract (`schemas/outbox.py`): oldest first, `done` on success, otherwise one
more attempt and the error's type, and `failed` after MAX_ATTEMPTS. FHIR jobs stay pending for the
production (WP11). The stored error is only the exception's type, so no address ends up in it.
"""

import logging
import smtplib
import threading
from collections.abc import Callable
from datetime import datetime
from email.message import EmailMessage

from hlidac import emails
from hlidac.schemas import MAX_ATTEMPTS, REDACT_AFTER_SEND, EmailJob, OutboxItem
from hlidac.store import Store

SendEmail = Callable[[EmailJob], None]

log = logging.getLogger(__name__)

POLL_SECONDS = 3

# The request that enqueues a login code and the poller may run at the same time; one at a time,
# so the same email is never sent twice.
_run_lock = threading.Lock()


ALL_RECIPIENTS = "*"


class RecipientNotAllowed(Exception):
    """The address isn't on SMTP_ALLOWED_RECIPIENTS; the row fails visibly, nothing is sent."""


class Undeliverable(Exception):
    """A reserved address (`@demo.local` …) on a real SMTP server: never sent; the row is closed
    as done with this reason, no retries (Mailpit, without a login, still catches them)."""


def smtp_sender(
    host: str,
    port: int,
    mail_from: str,
    user: str | None = None,
    password: str | None = None,
    allowed: frozenset[str] | None = None,
) -> SendEmail:
    """Mailpit by default. With a login (a real SMTP server, D14) it uses STARTTLS and sends only
    to the `allowed` addresses, or to every account's address if they include "*", which must be
    asked for explicitly."""
    if user is not None and not allowed:
        raise ValueError("real SMTP (SMTP_USER) needs SMTP_ALLOWED_RECIPIENTS")
    if user is not None and ALL_RECIPIENTS in allowed:
        log.warning("real SMTP to every account's address (SMTP_ALLOWED_RECIPIENTS=*)")

    def send(job: EmailJob) -> None:
        if user is not None and not emails.deliverable(job.to):
            raise Undeliverable
        if allowed is not None and ALL_RECIPIENTS not in allowed and job.to.lower() not in allowed:
            raise RecipientNotAllowed
        message = EmailMessage()
        message["From"] = mail_from
        message["To"] = job.to
        message["Subject"] = job.subject
        message["Content-Language"] = "cs"
        message.set_content(job.text_body)
        message.add_alternative(job.html_body, subtype="html")
        with smtplib.SMTP(host, port, timeout=10) as smtp:
            if user is not None:
                smtp.starttls()
                smtp.login(user, password or "")
            smtp.send_message(message)

    return send


def run_once(store: Store, now: datetime, send_email: SendEmail) -> int:
    """Process every pending email job once; returns how many were sent."""
    with _run_lock:
        return _run(store, now, send_email)


def _run(store: Store, now: datetime, send_email: SendEmail) -> int:
    sent = 0
    # Email rows only, so a FHIR row the guard refuses on loading can't stop the emails.
    for item in store.list_outbox("pending", kind="email"):
        if item.job.kind != "email":  # for the type checker; the query returns emails only
            continue
        try:
            send_email(item.job)
        except Undeliverable:  # closed, not failed: nothing to retry or to show in Unresolved
            closed = item.evolve(status="done", processed_at=now, last_error="Undeliverable")
            store.update_outbox_item(_redact(closed))
            continue
        except Exception as error:  # noqa: BLE001 - any failure is recorded on the row
            attempts = item.attempts + 1
            failed = attempts >= MAX_ATTEMPTS
            updated = item.evolve(
                attempts=attempts,
                last_error=type(error).__name__,
                status="failed" if failed else "pending",
                processed_at=now if failed else None,
            )
            store.update_outbox_item(_redact(updated) if failed else updated)
            continue
        store.update_outbox_item(_redact(item.evolve(status="done", processed_at=now)))
        sent += 1
    return sent


def _redact(item: OutboxItem) -> OutboxItem:
    """A finished login-code email keeps no live code (schemas/outbox.py)."""
    job = item.job
    if job.kind == "email" and job.template_id in REDACT_AFTER_SEND:
        return item.evolve(job=job.redacted().model_dump())
    return item


def start_poller(
    store: Store, clock: Callable[[], datetime], send_email: SendEmail
) -> threading.Event:
    """Retry pending emails every few seconds, as the production does FHIR rows; set the event to
    stop."""
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(POLL_SECONDS):
            try:
                run_once(store, clock(), send_email)
            except Exception as error:  # noqa: BLE001 - keep polling
                log.warning("outbox run failed: %s", type(error).__name__)  # the type only

    threading.Thread(target=loop, name="outbox-runner", daemon=True).start()
    return stop
