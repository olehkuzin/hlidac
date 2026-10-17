"""The outbox: the contract between the app and the IRIS production.

The app decides and shows the result first (urgent never waits), then puts a ready job here. The
production is the only writer to FHIR; the app's runner (`outbox_runner.py`) sends the emails
(D40). Each takes its own `pending` rows in `created_at` order, runs the job, and marks the row
`done`, or counts the attempt and retries, up to MAX_ATTEMPTS, then `failed` (shown in
Unresolved). Every job is safe to run twice.

The job never changes, with one exception: when an email from a template in REDACT_AFTER_SEND
(the login code) becomes `done` or `failed`, its bodies are replaced by REDACTED (`redacted()`).
"""

import json
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from hlidac.fhir_guard import TAG_CODE, TAG_SYSTEM, WRITABLE_TYPES, check_json
from hlidac.schemas._base import Count, Email, Id, NonEmpty, Strict, Version

__all__ = ["TAG_CODE", "TAG_SYSTEM", "WRITABLE_TYPES"]  # re-exported from fhir_guard

MAX_ATTEMPTS = 5

# The body of a sent or failed email whose content is a secret (templates in REDACT_AFTER_SEND).
REDACTED = "[odstraněno po odeslání]"
REDACT_AFTER_SEND = frozenset({"login-code"})

OutboxStatus = Literal["pending", "done", "failed"]
JobKind = Literal["fhir_transaction", "email"]
IdempotencyKey = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class FhirTransactionJob(Strict):
    """A FHIR R4 transaction Bundle, posted as is. Kept as a JSON string so it can't be changed
    after the guard has checked it; the production runs `fhir_guard` again before posting."""

    kind: Literal["fhir_transaction"]
    bundle_json: NonEmpty

    @model_validator(mode="after")
    def _safe_bundle(self) -> "FhirTransactionJob":
        check_json(self.bundle_json)
        return self

    @classmethod
    def of(cls, bundle: dict[str, Any]) -> "FhirTransactionJob":
        return cls(kind="fhir_transaction", bundle_json=json.dumps(bundle, ensure_ascii=False))

    def bundle(self) -> dict[str, Any]:
        """A fresh copy; changing it changes nothing in the job."""
        return json.loads(self.bundle_json)


class EmailJob(Strict):
    """Rendered by the app from an approved template; no clinical content (principle 3)."""

    kind: Literal["email"]
    to: Email
    template_id: Id
    template_version: Version
    subject: NonEmpty
    text_body: NonEmpty
    html_body: NonEmpty

    def redacted(self) -> "EmailJob":
        """The same job with the bodies removed. A login-code email is redacted once its row is
        done or failed, so a live code never stays in the outbox (WP30)."""
        return self.evolve(text_body=REDACTED, html_body=REDACTED)


Job = Annotated[FhirTransactionJob | EmailJob, Field(discriminator="kind")]


class OutboxItem(Strict):
    outbox_id: Id
    idempotency_key: IdempotencyKey  # unique: enqueueing the same key twice is a no-op
    created_at: AwareDatetime
    status: OutboxStatus = "pending"
    attempts: Count = 0
    last_error: NonEmpty | None = None
    # The app's runner writes demo-clock time; the IRIS production writes real UTC time.
    processed_at: AwareDatetime | None = None
    job: Job

    @model_validator(mode="after")
    def _consistent(self) -> "OutboxItem":
        if (self.status == "pending") != (self.processed_at is None):
            raise ValueError("processed_at is set once the row is done or failed")
        if self.status == "failed" and self.last_error is None:
            raise ValueError("a failed row says why")
        return self


class OutboxFailure(Strict):
    """A failed row for Unresolved, from the row's columns only: the job is never validated, so a
    job that no longer passes (changed in the database, or refused by a stricter guard) still
    shows. `template_id` is an email's template (None for FHIR, or when the job can't be read);
    never the body or the address."""

    outbox_id: Id
    kind: JobKind  # the job_kind column
    attempts: Count
    last_error: NonEmpty | None
    created_at: AwareDatetime
    template_id: Id | None

    @classmethod
    def of(cls, item: OutboxItem) -> "OutboxFailure":
        job = item.job
        return cls(
            outbox_id=item.outbox_id,
            kind=job.kind,
            attempts=item.attempts,
            last_error=item.last_error,
            created_at=item.created_at,
            template_id=job.template_id if job.kind == "email" else None,
        )
