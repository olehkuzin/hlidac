"""`IrisStore`: the `Store` (store.py) on the IRIS SQL tables in `iris/schema.sql` (WP10).

`record_json` is the record; the other columns are kept in step for lookups, ordering and the
production. Outbox rows are the exception: their status columns are the truth. Lists come back
oldest first, ties in insertion order (`%ID`), as in `InMemoryStore`.

One app process writes (D28). The lock makes its check-then-write steps atomic; the unique
constraints in the schema catch anything else. Append-only tables are also append-only for IRIS:
the app's account has no UPDATE or DELETE on them (setup.py).
"""

import contextlib
import json
import re
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, TypeVar

from iris.dbapi import IntegrityError
from pydantic import BaseModel

from hlidac.iris.db import Database, Row
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
    PatientPlan,
)
from hlidac.store import DuplicateError, check_outbox_update

SCHEMA = "Hlidac"
_SCHEMA_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")

M = TypeVar("M", bound=BaseModel)

_OUTBOX_COLUMNS = (
    "outbox_id, idempotency_key, %ODBCOUT(created_at), outbox_status, attempts, last_error,"
    " %ODBCOUT(processed_at), job_json"
)


def _utc(value: datetime | None) -> str | None:
    """A TIMESTAMP parameter: UTC, microseconds."""
    return None if value is None else value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


def _aware(value: datetime | str | None) -> datetime | None:
    """A TIMESTAMP read with %ODBCOUT: naive, in UTC."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return None if value is None else value.replace(tzinfo=UTC)


def _is_duplicate(error: Exception) -> bool:
    return isinstance(error, IntegrityError) and "<-119>" in str(error)


class IrisStore:
    def __init__(self, db: Database, schema: str = SCHEMA) -> None:
        if not _SCHEMA_NAME.fullmatch(schema):
            raise ValueError("the schema is a plain SQL identifier")
        self._db = db
        self._schema = schema
        self._lock = threading.RLock()

    # -- accounts and login codes

    def get_account(self, account_id: str) -> Account | None:
        return self._record(
            Account, "SELECT record_json FROM Hlidac.account WHERE account_id = ?", [account_id]
        )

    def find_account_by_email(self, email: str) -> Account | None:
        # Stored lower-case; the column also compares without case (default collation).
        return self._record(
            Account, "SELECT record_json FROM Hlidac.account WHERE email = ?", [email.strip().lower()]
        )

    def save_account(self, account: Account) -> None:
        with self._lock:
            other = self.find_account_by_email(account.email)
            if other is not None and other.account_id != account.account_id:
                raise DuplicateError("another account uses this email")
            self._write(
                "INSERT OR UPDATE INTO Hlidac.account"
                " (account_id, email, account_role, patient_id, record_json) VALUES (?, ?, ?, ?, ?)",
                [account.account_id, account.email, account.role, account.patient_id, _json(account)],
                duplicate="another account uses this email",
            )

    def list_team_accounts(self, team_id: str) -> list[Account]:
        # The table has no team column; a team has a handful of nurses, so filter the records.
        nurses = self._records(
            Account,
            "SELECT record_json FROM Hlidac.account WHERE account_role = 'nurse'"
            " ORDER BY account_id",
            [],
        )
        return [a for a in nurses if a.team_id == team_id]

    def list_patient_accounts(self, patient_id: str) -> list[Account]:
        return self._records(
            Account,
            "SELECT record_json FROM Hlidac.account WHERE account_role = 'patient'"
            " AND patient_id = ? ORDER BY account_id",
            [patient_id],
        )

    def save_login_code(self, code: LoginCode) -> None:
        self._write(
            "INSERT OR UPDATE INTO Hlidac.login_code"
            " (code_id, account_id, created_at, expires_at, used_at, record_json)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                code.code_id,
                code.account_id,
                _utc(code.created_at),
                _utc(code.expires_at),
                _utc(code.used_at),
                _json(code),
            ],
        )

    def list_login_codes(self, account_id: str) -> list[LoginCode]:
        return self._records(
            LoginCode,
            "SELECT record_json FROM Hlidac.login_code WHERE account_id = ?"
            " ORDER BY created_at, %ID",
            [account_id],
        )

    # -- plans and check-ins

    def get_patient_plan(self, patient_id: str) -> PatientPlan | None:
        return self._record(
            PatientPlan,
            "SELECT record_json FROM Hlidac.patient_plan WHERE patient_id = ?",
            [patient_id],
        )

    def save_patient_plan(self, plan: PatientPlan) -> None:
        self._write(
            "INSERT OR UPDATE INTO Hlidac.patient_plan (patient_id, care_team_id, record_json)"
            " VALUES (?, ?, ?)",
            [plan.patient_id, plan.care_team_id, _json(plan)],
        )

    def list_patient_plans(self, care_team_id: str) -> list[PatientPlan]:
        return self._records(
            PatientPlan,
            "SELECT record_json FROM Hlidac.patient_plan WHERE care_team_id = ? ORDER BY patient_id",
            [care_team_id],
        )

    def get_checkin(self, checkin_id: str) -> Checkin | None:
        return self._record(
            Checkin, "SELECT record_json FROM Hlidac.checkin WHERE checkin_id = ?", [checkin_id]
        )

    def save_checkin(self, checkin: Checkin) -> None:
        self._write(
            "INSERT OR UPDATE INTO Hlidac.checkin"
            " (checkin_id, patient_id, checkin_status, started_at, updated_at, record_json)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                checkin.checkin_id,
                checkin.patient_id,
                checkin.status,
                _utc(checkin.started_at),
                _utc(checkin.updated_at),
                _json(checkin),
            ],
        )

    def list_checkins(self, patient_id: str) -> list[Checkin]:
        return self._records(
            Checkin,
            "SELECT record_json FROM Hlidac.checkin WHERE patient_id = ? ORDER BY started_at, %ID",
            [patient_id],
        )

    # -- assessments and grade reviews (append-only)

    def add_assessment(self, assessment: Assessment) -> None:
        self._write(
            "INSERT INTO Hlidac.assessment"
            " (assessment_id, checkin_id, patient_id, assessment_status, assessed_at, record_json)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                assessment.assessment_id,
                assessment.checkin_id,
                assessment.patient_id,
                assessment.status,
                _utc(assessment.assessed_at),
                _json(assessment),
            ],
            duplicate="assessments are never overwritten",
        )

    def get_assessment(self, assessment_id: str) -> Assessment | None:
        return self._record(
            Assessment,
            "SELECT record_json FROM Hlidac.assessment WHERE assessment_id = ?",
            [assessment_id],
        )

    def list_assessments(self, patient_id: str) -> list[Assessment]:
        return self._records(
            Assessment,
            "SELECT record_json FROM Hlidac.assessment WHERE patient_id = ?"
            " ORDER BY assessed_at, %ID",
            [patient_id],
        )

    def add_grade_review(self, review: GradeReview) -> None:
        self._write(
            "INSERT INTO Hlidac.grade_review"
            " (review_id, assessment_id, grade_id, reviewed_at, record_json) VALUES (?, ?, ?, ?, ?)",
            [
                review.review_id,
                review.assessment_id,
                review.grade.grade_id,
                _utc(review.grade.confirmed_at),  # set on every confirmed or corrected grade
                _json(review),
            ],
            duplicate="grade reviews are never overwritten",
        )

    def list_grade_reviews(self, assessment_id: str) -> list[GradeReview]:
        return self._records(
            GradeReview,
            "SELECT record_json FROM Hlidac.grade_review WHERE assessment_id = ?"
            " ORDER BY review_id",
            [assessment_id],
        )

    # -- alerts and notifications

    def get_alert(self, alert_id: str) -> Alert | None:
        return self._record(
            Alert, "SELECT record_json FROM Hlidac.alert WHERE alert_id = ?", [alert_id]
        )

    def save_alert(self, alert: Alert) -> None:
        self._write(
            "INSERT OR UPDATE INTO Hlidac.alert"
            " (alert_id, patient_id, alert_status, priority, created_at, ack_deadline, record_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                alert.alert_id,
                alert.patient_id,
                alert.status,
                alert.priority,
                _utc(alert.created_at),
                _utc(alert.ack_deadline),
                _json(alert),
            ],
        )

    def list_alerts(
        self, status: AlertStatus | None = None, patient_id: str | None = None
    ) -> list[Alert]:
        where, params = _filters(alert_status=status, patient_id=patient_id)
        return self._records(
            Alert, f"SELECT record_json FROM Hlidac.alert{where} ORDER BY created_at, %ID", params
        )

    def save_notification(self, notification: Notification) -> None:
        self._write(
            "INSERT OR UPDATE INTO Hlidac.notification"
            " (notification_id, recipient_account_id, created_at, read_at, record_json)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                notification.notification_id,
                notification.recipient_account_id,
                _utc(notification.created_at),
                _utc(notification.read_at),
                _json(notification),
            ],
        )

    def list_notifications(self, recipient_account_id: str) -> list[Notification]:
        return self._records(
            Notification,
            "SELECT record_json FROM Hlidac.notification WHERE recipient_account_id = ?"
            " ORDER BY created_at, %ID",
            [recipient_account_id],
        )

    # -- outbox (status columns are the truth; the production updates them too)

    def enqueue(self, item: OutboxItem) -> bool:
        with self._lock:
            if self._query(
                "SELECT 1 FROM Hlidac.outbox WHERE idempotency_key = ?", [item.idempotency_key]
            ):
                return False
            self._write(
                "INSERT INTO Hlidac.outbox (outbox_id, idempotency_key, job_kind, outbox_status,"
                " attempts, created_at, processed_at, last_error, job_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    item.outbox_id,
                    item.idempotency_key,
                    item.job.kind,
                    item.status,
                    item.attempts,
                    _utc(item.created_at),
                    _utc(item.processed_at),
                    item.last_error,
                    item.job.model_dump_json(),
                ],
                duplicate="outbox ids are unique",
            )
            return True

    def update_outbox_item(self, item: OutboxItem) -> None:
        with self._lock:
            old = self.get_outbox_item(item.outbox_id)
            if old is None:
                raise KeyError(item.outbox_id)
            check_outbox_update(old, item)
            columns = "outbox_status = ?, attempts = ?, last_error = ?, processed_at = ?"
            params: list[Any] = [item.status, item.attempts, item.last_error]
            params.append(_utc(item.processed_at))
            if item.job != old.job:  # a redaction, the one job change check_outbox_update allows
                columns += ", job_json = ?"
                params.append(item.job.model_dump_json())
            self._write(f"UPDATE Hlidac.outbox SET {columns} WHERE outbox_id = ?", [*params, item.outbox_id])

    def get_outbox_item(self, outbox_id: str) -> OutboxItem | None:
        rows = self._query(
            f"SELECT {_OUTBOX_COLUMNS} FROM Hlidac.outbox WHERE outbox_id = ?", [outbox_id]
        )
        return _outbox_item(rows[0]) if rows else None

    def list_outbox(
        self, status: OutboxStatus | None = None, kind: JobKind | None = None
    ) -> list[OutboxItem]:
        where, params = _filters(outbox_status=status, job_kind=kind)
        rows = self._query(
            f"SELECT {_OUTBOX_COLUMNS} FROM Hlidac.outbox{where} ORDER BY created_at, %ID", params
        )
        return [_outbox_item(row) for row in rows]

    def list_outbox_failures(self) -> list[OutboxFailure]:
        rows = self._query(
            "SELECT outbox_id, job_kind, attempts, last_error, %ODBCOUT(created_at), job_json"
            " FROM Hlidac.outbox WHERE outbox_status = 'failed' ORDER BY created_at, %ID",
            [],
        )
        return [_outbox_failure(row) for row in rows]

    # -- audit (append-only)

    def append_audit(self, entry: AuditEntry) -> None:
        self._write(
            "INSERT INTO Hlidac.audit_log"
            " (audit_id, logged_at, actor, audit_action, target, record_json)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [entry.audit_id, _utc(entry.at), entry.actor, entry.action, entry.target, _json(entry)],
            duplicate="the audit log is append-only",
        )

    def list_audit(self, target: str | None = None) -> list[AuditEntry]:
        where, params = _filters(target=target)
        return self._records(
            AuditEntry,
            f"SELECT record_json FROM Hlidac.audit_log{where} ORDER BY logged_at, %ID",
            params,
        )

    # -- helpers

    def _sql(self, statement: str) -> str:
        """Statements name the tables as in schema.sql; tests run them on their own schema."""
        return statement.replace("Hlidac.", f"{self._schema}.")

    def _query(self, statement: str, params: Sequence[Any]) -> list[Row]:
        return self._db.query(self._sql(statement), params)

    def _records(self, model: type[M], statement: str, params: Sequence[Any]) -> list[M]:
        return [model.model_validate_json(row[0]) for row in self._query(statement, params)]

    def _record(self, model: type[M], statement: str, params: Sequence[Any]) -> M | None:
        found = self._records(model, statement, params)
        return found[0] if found else None

    def _write(self, statement: str, params: Sequence[Any], duplicate: str | None = None) -> None:
        try:
            self._db.execute(self._sql(statement), params)
        except Exception as error:
            if duplicate is not None and _is_duplicate(error):
                raise DuplicateError(duplicate) from error
            raise


def _json(record: BaseModel) -> str:
    return record.model_dump_json()


def _filters(**columns: str | None) -> tuple[str, list[str]]:
    """A WHERE clause from fixed column names; only the values are parameters."""
    used = {name: value for name, value in columns.items() if value is not None}
    if not used:
        return "", []
    return " WHERE " + " AND ".join(f"{name} = ?" for name in used), list(used.values())


def _outbox_item(row: Row) -> OutboxItem:
    outbox_id, key, created_at, status, attempts, last_error, processed_at, job_json = row
    return OutboxItem(
        outbox_id=outbox_id,
        idempotency_key=key,
        created_at=_aware(created_at),
        status=status,
        attempts=attempts,
        last_error=last_error,
        processed_at=_aware(processed_at),
        job=json.loads(job_json),
    )


def _outbox_failure(row: Row) -> OutboxFailure:
    """The columns only; the job is never constructed, so it isn't validated (schemas/outbox.py)."""
    outbox_id, kind, attempts, last_error, created_at, job_json = row
    failure = OutboxFailure(
        outbox_id=outbox_id,
        kind=kind,
        attempts=attempts,
        last_error=last_error or None,
        created_at=_aware(created_at),
        template_id=None,
    )
    if kind == "email":
        with contextlib.suppress(Exception):  # an unreadable job still shows, without its template
            failure = failure.evolve(template_id=json.loads(job_json).get("template_id"))
    return failure
