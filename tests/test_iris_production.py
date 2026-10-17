"""WP11 end to end on the team's IRIS (HLIDAC_IRIS_TESTS=1 only; `python -m hlidac.iris.deploy`
first): Hlidac.Production picks rows up from the real Hlidac.outbox and writes them into the FHIR
repository. The test writes one tagged self-test QuestionnaireResponse with its own identifier,
no patient and no patient data, and removes it and its outbox rows afterwards.
"""

import json
import time
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from factories import T0, assessment, checkin, grade, ui_plan
from iris_support import IRIS_TESTS

from hlidac import fhir_mapping
from hlidac.config import Settings
from hlidac.iris import setup
from hlidac.iris.db import Database
from hlidac.iris.deploy import PRODUCTION
from hlidac.iris.store import IrisStore
from hlidac.schemas import TAG_CODE, TAG_SYSTEM, FhirTransactionJob, OutboxItem

pytestmark = pytest.mark.skipif(not IRIS_TESTS, reason="IRIS tests run only with HLIDAC_IRIS_TESTS=1")

SYSTEM = "urn:hlidac:checkin"
WAIT_SECONDS = 60


@pytest.fixture(scope="module")
def iris_env():
    config, admin = Settings(), setup.AdminSettings()
    admin_db = setup.admin_database(config)
    app_db = Database.from_settings(config)
    value = f"selftest-{uuid.uuid4().hex[:12]}"
    yield config, admin, admin_db, IrisStore(app_db), value
    admin_db.execute("DELETE FROM Hlidac.outbox WHERE idempotency_key %STARTSWITH ?", [value])
    httpx.delete(  # a conditional delete on our own self-test identifier only
        f"{config.fhir_base_url}/QuestionnaireResponse",
        params={"identifier": f"{SYSTEM}|{value}"},
        auth=(admin.user, admin.password.get_secret_value()),
        timeout=60,
    )
    admin_db.close()
    app_db.close()


def bundle(value: str) -> dict:
    resource = {
        "resourceType": "QuestionnaireResponse",
        "meta": {"tag": [{"system": TAG_SYSTEM, "code": TAG_CODE}]},
        "identifier": {"system": SYSTEM, "value": value},
        "status": "in-progress",
    }
    url = f"QuestionnaireResponse?identifier={SYSTEM}|{value}"
    request = {"method": "PUT", "url": url}
    return {"resourceType": "Bundle", "type": "transaction", "entry": [{"resource": resource, "request": request}]}


def enqueue(store: IrisStore, value: str, n: int, written: dict | None = None) -> str:
    outbox_id = f"{value}-{n}"
    item = OutboxItem(
        outbox_id=outbox_id,
        idempotency_key=f"{value}:{n}",
        created_at=datetime.now(UTC),
        job=FhirTransactionJob.of(written or bundle(value)),
    )
    assert store.enqueue(item)
    return outbox_id


def wait_until_finished(store: IrisStore, outbox_id: str) -> OutboxItem:
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        row = store.get_outbox_item(outbox_id)
        if row.status != "pending":
            return row
        time.sleep(1)
    pytest.fail(f"{PRODUCTION} left the row pending for {WAIT_SECONDS}s; is it deployed and running?")


def count_in_fhir(config, admin, value: str) -> int:
    response = httpx.get(
        f"{config.fhir_base_url}/QuestionnaireResponse",
        params={"identifier": f"{SYSTEM}|{value}", "_summary": "count"},
        auth=(admin.user, admin.password.get_secret_value()),
        timeout=60,
    )
    response.raise_for_status()
    return response.json()["total"]


def test_a_row_is_written_to_fhir_once_even_when_sent_twice(iris_env):
    config, admin, _, store, value = iris_env
    first = wait_until_finished(store, enqueue(store, value, 1))
    assert (first.status, first.last_error, first.attempts) == ("done", None, 0)
    assert first.processed_at is not None
    assert count_in_fhir(config, admin, value) == 1
    second = wait_until_finished(store, enqueue(store, value, 2))  # the same bundle again
    assert second.status == "done"
    assert count_in_fhir(config, admin, value) == 1  # a conditional PUT creates nothing new


def delete_what_the_bundle_wrote(config, admin, written: dict) -> None:
    """Undo a test bundle: a conditional delete on each entry's own identifier, or its own id."""
    for entry in written["entry"]:
        response = httpx.delete(
            f"{config.fhir_base_url}/{entry['request']['url']}",
            auth=(admin.user, admin.password.get_secret_value()),
            timeout=60,
        )
        assert response.status_code in {200, 204}, entry["request"]["url"]


def test_iris_accepts_a_check_in_bundle_from_fhir_mapping(iris_env):
    """WP12's open question (D33): IRIS takes identifier-only references, the conditional PUT on
    the QuestionnaireResponse's single identifier, and the Provenance PUT on our own id."""
    config, admin, _, store, value = iris_env
    started = checkin(checkin_id=f"{value}-c", patient_id=f"{value}-patient", status="completed")
    assessed = assessment(
        assessment_id=f"{value}-a",
        checkin_id=started.checkin_id,
        patient_id=started.patient_id,
        grades=[grade(grade_id=f"{value}-g")],
    )
    written = fhir_mapping.checkin_bundle(started, ui_plan(), assessed, None)
    try:
        assert fhir_mapping.enqueue_checkin(store, started, ui_plan(), assessed, None, T0)
        [outbox_id] = [
            o.outbox_id
            for o in store.list_outbox("pending")
            if o.idempotency_key == fhir_mapping.checkin_key(started, assessed)
        ]
        row = wait_until_finished(store, outbox_id)
        assert (row.status, row.last_error) == ("done", None)
    finally:
        delete_what_the_bundle_wrote(config, admin, written)
        store_rows = [f"checkin:{started.checkin_id}:%"]
        iris_env[2].execute("DELETE FROM Hlidac.outbox WHERE idempotency_key LIKE ?", store_rows)


def test_a_rejected_row_waits_and_holds_back_newer_rows(iris_env):
    """D33 and the backoff: a row FHIR rejects is retried only after RetryBaseSeconds, and no newer
    row overtakes it. The test deletes its bad row quickly, so real rows wait only seconds."""
    _, _, admin_db, store, value = iris_env
    rejected = bundle(f"{value}-rejected")
    rejected["entry"][0]["resource"]["status"] = "not-a-status"  # passes the guard, not FHIR
    bad = enqueue(store, value, 10, rejected)
    good = enqueue(store, value, 11)
    try:
        deadline = time.monotonic() + WAIT_SECONDS
        while store.get_outbox_item(bad).attempts == 0 and time.monotonic() < deadline:
            time.sleep(1)
        row = store.get_outbox_item(bad)
        assert (row.status, row.attempts) == ("pending", 1)
        assert row.last_error.startswith("Fhir")
        time.sleep(8)  # well inside the first 30 s wait
        assert store.get_outbox_item(bad).attempts == 1  # no immediate retry
        assert store.get_outbox_item(good).status == "pending"  # held back behind it
    finally:
        admin_db.execute("DELETE FROM Hlidac.outbox WHERE outbox_id = ?", [bad])
    assert wait_until_finished(store, good).status == "done"  # and it goes once the bad row is gone


def test_an_unsafe_row_edited_in_the_database_is_refused(iris_env):
    config, admin, admin_db, _, value = iris_env
    unsafe = bundle(f"{value}-unsafe")
    del unsafe["entry"][0]["resource"]["meta"]  # untagged: the guard must refuse it
    job = json.dumps({"kind": "fhir_transaction", "bundle_json": json.dumps(unsafe)})
    admin_db.execute(  # bypasses the app's own guard, as a tampered row would
        "INSERT INTO Hlidac.outbox (outbox_id, idempotency_key, job_kind, outbox_status,"
        " attempts, created_at, job_json) VALUES (?, ?, 'fhir_transaction', 'pending', 0, ?, ?)",
        [f"{value}-u", f"{value}:unsafe", datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f"), job],
    )
    # The app refuses to even load this row (its guard runs on every read), so read the columns.
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        [(status, last_error)] = admin_db.query(
            "SELECT outbox_status, last_error FROM Hlidac.outbox WHERE outbox_id = ?", [f"{value}-u"]
        )
        if status != "pending":
            break
        time.sleep(1)
    assert (status, last_error) == ("failed", "UnsafeBundle")
    assert count_in_fhir(config, admin, f"{value}-unsafe") == 0
