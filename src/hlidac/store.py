"""The persistence seam.

The app talks only to `Store` and `ContextSource`. `InMemoryStore` and `StaticContextSource` run the
app and the tests without IRIS; the IRIS versions implement the same methods on the tables in
`iris/schema.sql` (`hlidac.iris.store.IrisStore`, WP10) and on the hospital FHIR data (WP14). Routes
do the access checks; the store does none.
"""

import threading
from typing import Protocol

from hlidac.schemas import (
    Account,
    Alert,
    AlertStatus,
    Assessment,
    AuditEntry,
    Checkin,
    GradeReview,
    JobKind,
    LoginCode,
    Notification,
    OutboxFailure,
    OutboxItem,
    OutboxStatus,
    PatientContext,
    PatientPlan,
)


class DuplicateError(ValueError):
    """An append-only record with this id exists already."""


def check_outbox_update(old: OutboxItem, new: OutboxItem) -> None:
    """The outbox update contract, shared by every Store: only the status columns change, except
    that a done or failed email may be redacted (EmailJob.redacted, schemas/outbox.py)."""
    if (old.idempotency_key, old.created_at) != (new.idempotency_key, new.created_at):
        raise ValueError("only the status columns of an outbox row change")
    redacting = (
        old.job.kind == "email" and new.status != "pending" and new.job == old.job.redacted()
    )
    if new.job != old.job and not redacting:
        raise ValueError("the job of an outbox row never changes, except a redaction")


class Store(Protocol):
    def get_account(self, account_id: str) -> Account | None: ...
    def find_account_by_email(self, email: str) -> Account | None: ...
    def save_account(self, account: Account) -> None: ...
    def list_team_accounts(self, team_id: str) -> list[Account]: ...  # WP16: a team's nurses
    def list_patient_accounts(self, patient_id: str) -> list[Account]: ...  # WP16

    def save_login_code(self, code: LoginCode) -> None: ...
    def list_login_codes(self, account_id: str) -> list[LoginCode]: ...

    def get_patient_plan(self, patient_id: str) -> PatientPlan | None: ...
    def save_patient_plan(self, plan: PatientPlan) -> None: ...
    def list_patient_plans(self, care_team_id: str) -> list[PatientPlan]: ...  # WP13

    def get_checkin(self, checkin_id: str) -> Checkin | None: ...
    def save_checkin(self, checkin: Checkin) -> None: ...
    def list_checkins(self, patient_id: str) -> list[Checkin]: ...

    def add_assessment(self, assessment: Assessment) -> None: ...  # append-only
    def get_assessment(self, assessment_id: str) -> Assessment | None: ...
    def list_assessments(self, patient_id: str) -> list[Assessment]: ...

    def add_grade_review(self, review: GradeReview) -> None: ...  # append-only
    def list_grade_reviews(self, assessment_id: str) -> list[GradeReview]: ...

    def get_alert(self, alert_id: str) -> Alert | None: ...
    def save_alert(self, alert: Alert) -> None: ...
    def list_alerts(
        self, status: AlertStatus | None = None, patient_id: str | None = None
    ) -> list[Alert]: ...

    def save_notification(self, notification: Notification) -> None: ...
    def list_notifications(self, recipient_account_id: str) -> list[Notification]: ...

    def enqueue(self, item: OutboxItem) -> bool: ...  # False if the idempotency key exists
    # Status columns only (status, attempts, last_error, processed_at); the job never changes,
    # except that a done or failed email may be redacted (EmailJob.redacted, schemas/outbox.py).
    def update_outbox_item(self, item: OutboxItem) -> None: ...
    def get_outbox_item(self, outbox_id: str) -> OutboxItem | None: ...
    def list_outbox(
        self, status: OutboxStatus | None = None, kind: JobKind | None = None
    ) -> list[OutboxItem]: ...
    # Failed rows from their columns only, never validating the job, for Unresolved.
    def list_outbox_failures(self) -> list[OutboxFailure]: ...

    def append_audit(self, entry: AuditEntry) -> None: ...  # append-only
    def list_audit(self, target: str | None = None) -> list[AuditEntry]: ...


class ContextSource(Protocol):
    """Reads treatment context from the hospital record (read only)."""

    def get_context(self, patient_id: str) -> PatientContext | None: ...


class InMemoryStore:
    """Lists come back oldest first."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._accounts: dict[str, Account] = {}
        self._codes: dict[str, LoginCode] = {}
        self._plans: dict[str, PatientPlan] = {}
        self._checkins: dict[str, Checkin] = {}
        self._assessments: dict[str, Assessment] = {}
        self._reviews: dict[str, GradeReview] = {}
        self._alerts: dict[str, Alert] = {}
        self._notifications: dict[str, Notification] = {}
        self._outbox: dict[str, OutboxItem] = {}
        self._audit: dict[str, AuditEntry] = {}

    def get_account(self, account_id: str) -> Account | None:
        return self._accounts.get(account_id)

    def find_account_by_email(self, email: str) -> Account | None:
        wanted = email.strip().casefold()
        return next((a for a in self._accounts.values() if a.email.casefold() == wanted), None)

    def save_account(self, account: Account) -> None:
        with self._lock:
            other = self.find_account_by_email(account.email)
            if other is not None and other.account_id != account.account_id:
                raise DuplicateError("another account uses this email")
            self._accounts[account.account_id] = account

    def list_team_accounts(self, team_id: str) -> list[Account]:
        found = [a for a in self._accounts.values() if a.role == "nurse" and a.team_id == team_id]
        return sorted(found, key=lambda a: a.account_id)

    def list_patient_accounts(self, patient_id: str) -> list[Account]:
        found = [
            a for a in self._accounts.values() if a.role == "patient" and a.patient_id == patient_id
        ]
        return sorted(found, key=lambda a: a.account_id)

    def save_login_code(self, code: LoginCode) -> None:
        with self._lock:
            self._codes[code.code_id] = code

    def list_login_codes(self, account_id: str) -> list[LoginCode]:
        codes = [c for c in self._codes.values() if c.account_id == account_id]
        return sorted(codes, key=lambda c: c.created_at)

    def get_patient_plan(self, patient_id: str) -> PatientPlan | None:
        return self._plans.get(patient_id)

    def save_patient_plan(self, plan: PatientPlan) -> None:
        with self._lock:
            self._plans[plan.patient_id] = plan

    def list_patient_plans(self, care_team_id: str) -> list[PatientPlan]:
        found = [p for p in self._plans.values() if p.care_team_id == care_team_id]
        return sorted(found, key=lambda p: p.patient_id)

    def get_checkin(self, checkin_id: str) -> Checkin | None:
        return self._checkins.get(checkin_id)

    def save_checkin(self, checkin: Checkin) -> None:
        with self._lock:
            self._checkins[checkin.checkin_id] = checkin

    def list_checkins(self, patient_id: str) -> list[Checkin]:
        checkins = [c for c in self._checkins.values() if c.patient_id == patient_id]
        return sorted(checkins, key=lambda c: c.started_at)

    def add_assessment(self, assessment: Assessment) -> None:
        with self._lock:
            if assessment.assessment_id in self._assessments:
                raise DuplicateError("assessments are never overwritten")
            self._assessments[assessment.assessment_id] = assessment

    def get_assessment(self, assessment_id: str) -> Assessment | None:
        return self._assessments.get(assessment_id)

    def list_assessments(self, patient_id: str) -> list[Assessment]:
        found = [a for a in self._assessments.values() if a.patient_id == patient_id]
        return sorted(found, key=lambda a: a.assessed_at)

    def add_grade_review(self, review: GradeReview) -> None:
        with self._lock:
            if review.review_id in self._reviews:
                raise DuplicateError("grade reviews are never overwritten")
            self._reviews[review.review_id] = review

    def list_grade_reviews(self, assessment_id: str) -> list[GradeReview]:
        found = [r for r in self._reviews.values() if r.assessment_id == assessment_id]
        return sorted(found, key=lambda r: r.review_id)

    def get_alert(self, alert_id: str) -> Alert | None:
        return self._alerts.get(alert_id)

    def save_alert(self, alert: Alert) -> None:
        with self._lock:
            self._alerts[alert.alert_id] = alert

    def list_alerts(
        self, status: AlertStatus | None = None, patient_id: str | None = None
    ) -> list[Alert]:
        alerts = [
            a
            for a in self._alerts.values()
            if (status is None or a.status == status)
            and (patient_id is None or a.patient_id == patient_id)
        ]
        return sorted(alerts, key=lambda a: a.created_at)

    def save_notification(self, notification: Notification) -> None:
        with self._lock:
            self._notifications[notification.notification_id] = notification

    def list_notifications(self, recipient_account_id: str) -> list[Notification]:
        found = [
            n
            for n in self._notifications.values()
            if n.recipient_account_id == recipient_account_id
        ]
        return sorted(found, key=lambda n: n.created_at)

    def enqueue(self, item: OutboxItem) -> bool:
        with self._lock:
            if any(o.idempotency_key == item.idempotency_key for o in self._outbox.values()):
                return False
            if item.outbox_id in self._outbox:
                raise DuplicateError("outbox ids are unique")
            self._outbox[item.outbox_id] = item
            return True

    def update_outbox_item(self, item: OutboxItem) -> None:
        with self._lock:
            old = self._outbox.get(item.outbox_id)
            if old is None:
                raise KeyError(item.outbox_id)
            check_outbox_update(old, item)
            self._outbox[item.outbox_id] = item

    def get_outbox_item(self, outbox_id: str) -> OutboxItem | None:
        return self._outbox.get(outbox_id)

    def list_outbox(
        self, status: OutboxStatus | None = None, kind: JobKind | None = None
    ) -> list[OutboxItem]:
        items = [
            o
            for o in self._outbox.values()
            if (status is None or o.status == status) and (kind is None or o.job.kind == kind)
        ]
        return sorted(items, key=lambda o: o.created_at)

    def list_outbox_failures(self) -> list[OutboxFailure]:
        return [OutboxFailure.of(o) for o in self.list_outbox("failed")]

    def append_audit(self, entry: AuditEntry) -> None:
        with self._lock:
            if entry.audit_id in self._audit:
                raise DuplicateError("the audit log is append-only")
            self._audit[entry.audit_id] = entry

    def list_audit(self, target: str | None = None) -> list[AuditEntry]:
        entries = [e for e in self._audit.values() if target is None or e.target == target]
        return sorted(entries, key=lambda e: e.at)


class StaticContextSource:
    """Hand-made contexts for development and tests. Never real patient data."""

    def __init__(self, contexts: dict[str, PatientContext]) -> None:
        self._contexts = dict(contexts)

    def get_context(self, patient_id: str) -> PatientContext | None:
        return self._contexts.get(patient_id)
