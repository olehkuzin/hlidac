"""WP14: hospital treatment context (`IrisContextSource`), demo patient selection, pseudonymous
labels, and demo reset (`reset_demo`).

Unit tests here use fake `Database` queries and hand-made fixtures with no real patient data.
When `HLIDAC_IRIS_TESTS=1` is set, the live IRIS tests also verify `IrisContextSource` on the
app's least-privilege account and `hospital_resource_counts` on the hospital repository.
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from iris_support import IRIS_TESTS

from hlidac.iris.context import (
    FHIR_SEARCH_SCHEMA,
    PROC_CODE_SCHEMA,
    READ_ONLY_FHIR_TABLES,
    SACT_PROCEDURE_CODE,
    IrisContextSource,
)
from hlidac.iris.demo_patients import BACKUP_NURSE, NURSES, demo_records, dose_patient_ids
from hlidac.iris.reset import (
    DEMO_WORKFLOW_TABLES,
    clear_demo_sql,
    hospital_resource_counts,
    tagged_fhir_keys,
)
from hlidac.schemas import TAG_CODE, TAG_SYSTEM

NOW = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)


class FakeDb:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.rows = rows or []
        self.queries: list[tuple[str, list[Any]]] = []

    def query(self, sql: str, params: list[Any] | None = None) -> list[tuple[Any, ...]]:
        self.queries.append((sql, list(params or [])))
        return list(self.rows)

    def execute(self, sql: str, params: list[Any] | None = None) -> int:
        self.queries.append((sql, list(params or [])))
        return 1


def test_context_source_picks_latest_completed_dose_not_after_now():
    db = FakeDb(
        [
            ("MedicationAdministration/ma-old", "2026-09-10T00:00:00+02:00"),
            ("Procedure/proc-recent", "2026-10-06T00:00:00+02:00"),
            ("MedicationAdministration/ma-future", "2026-11-01T00:00:00+01:00"),
        ]
    )
    source = IrisContextSource(db, lambda: NOW)
    ctx = source.get_context("demo-p1")
    assert ctx is not None
    assert ctx.patient_id == "demo-p1"
    assert ctx.last_sact_dose_at == datetime.fromisoformat("2026-10-06T00:00:00+02:00")
    assert ctx.regimen is None
    assert ctx.immunocompromised is None  # unknown, never assumed False
    assert ctx.source_refs == ["Procedure/proc-recent", "MedicationAdministration/ma-old"]
    assert ctx.read_at == NOW
    [(sql, params)] = db.queries
    assert params == ["Patient/demo-p1", "Patient/demo-p1", SACT_PROCEDURE_CODE]
    assert FHIR_SEARCH_SCHEMA in sql and PROC_CODE_SCHEMA in sql


def test_context_source_returns_none_when_no_past_dose_exists():
    db = FakeDb([("MedicationAdministration/ma-future", "2026-12-01T00:00:00+01:00")])
    source = IrisContextSource(db, lambda: NOW)
    assert source.get_context("demo-p2") is None


def test_dose_patient_ids_prioritises_in_window_doses_near_day_10():
    db = FakeDb(
        [
            ("Patient/p-old", "2026-01-10T00:00:00+01:00"),
            ("Patient/p-day10", "2026-10-06T00:00:00+02:00"),
            ("Patient/p-day25", "2026-09-21T00:00:00+02:00"),
            ("Patient/p-future", "2027-01-01T00:00:00+01:00"),
        ]
    )
    chosen = dose_patient_ids(db, NOW, 3)
    assert chosen == ["p-day10", "p-day25", "p-old"]


def test_demo_records_assign_pseudonymous_labels_and_all_nurses():
    accounts, plans = demo_records(["id-1", "id-2"], NOW)
    patients = [a for a in accounts if a.role == "patient"]
    nurses = [a for a in accounts if a.role == "nurse"]
    assert [p.display_name for p in patients] == ["Pacient 01", "Pacient 02"]
    assert [p.email for p in patients] == ["pacient-01@demo.local", "pacient-02@demo.local"]
    assert len(nurses) == len(NURSES) and BACKUP_NURSE in nurses
    assert [p.patient_id for p in plans] == ["id-1", "id-2"]


def test_tagged_fhir_keys_refuses_non_writable_resource_type():
    db = FakeDb([("Patient/123",)])
    with pytest.raises(RuntimeError, match="refusing to delete tagged resource of type 'Patient'"):
        tagged_fhir_keys(db)


def test_tagged_fhir_keys_accepts_writable_types():
    db = FakeDb([("QuestionnaireResponse/10",), ("Observation/11",)])
    assert tagged_fhir_keys(db) == ["QuestionnaireResponse/10", "Observation/11"]
    [(_, params)] = db.queries
    assert params == [TAG_SYSTEM, TAG_CODE]


def test_clear_demo_sql_keeps_accounts_by_default():
    db = FakeDb([(2,)])
    removed = clear_demo_sql(db, schema="HlidacTest")
    assert tuple(removed) == DEMO_WORKFLOW_TABLES
    deleted_tables = [sql for sql, _ in db.queries if sql.startswith("DELETE FROM")]
    assert len(deleted_tables) == len(DEMO_WORKFLOW_TABLES)
    assert not any("account" in sql or "patient_plan" in sql for sql in deleted_tables)


@pytest.mark.skipif(not IRIS_TESTS, reason="IRIS tests run only with HLIDAC_IRIS_TESTS=1")
def test_iris_app_account_reads_context_and_cannot_write_search_tables(iris_admin, iris_app_db):
    from hlidac.iris import setup

    setup.grant_app_role(iris_admin)
    for schema, table in READ_ONLY_FHIR_TABLES:
        target = f'"{schema}"."{table}"'
        with pytest.raises(Exception, match="<-99>"):
            iris_app_db.execute(f"DELETE FROM {target} WHERE 1=0")
    source = IrisContextSource(iris_app_db, lambda: NOW)
    assert source.get_context("non-existent-patient") is None
    counts = hospital_resource_counts(iris_admin)
    assert counts.get("Patient", 0) > 0
