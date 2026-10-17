"""WP11 without IRIS: the production's class files are consistent with the app, and the Python
inside Hlidac.Prod.Guard refuses everything the app's guard refuses (it runs here unchanged)."""

import json
import re
import textwrap

import pytest
from factories import fhir_item

from hlidac.iris import deploy
from hlidac.schemas import MAX_ATTEMPTS

SOURCES = deploy.class_sources()


def guard_check():
    """Hlidac.Prod.Guard.Check, the Python method exactly as deployed, as a local function."""
    text = SOURCES["Hlidac.Prod.Guard"]
    body = re.search(
        r"ClassMethod Check\(.*?\[ Language = python \]\n\{\n(.*?)\n\}", text, re.DOTALL
    )
    code = "def check(source, version, jobJson):\n" + textwrap.indent(
        textwrap.dedent(body.group(1).expandtabs(4)), "    "
    )
    namespace: dict = {}
    exec(compile(code, "Hlidac.Prod.Guard.Check", "exec"), namespace)  # noqa: S102 - our own file
    return namespace["check"]


@pytest.fixture
def check():
    return guard_check()


def job_json(bundle: dict) -> str:
    return json.dumps({"kind": "fhir_transaction", "bundle_json": json.dumps(bundle)})


def test_the_deployed_classes():
    assert set(SOURCES) == {
        "Hlidac.Production",
        "Hlidac.Prod.OutboxService",
        "Hlidac.Prod.OutboxProcess",
        "Hlidac.Prod.OutboxRow",
        "Hlidac.Prod.OutboxResult",
        "Hlidac.Prod.Guard",
    }


def test_the_production_wires_the_items_the_components_target():
    production = SOURCES["Hlidac.Production"]
    items = dict(re.findall(r'<Item Name="(\w+)"[^>]*ClassName="([\w.]+)"', production))
    assert items == {
        "OutboxService": "Hlidac.Prod.OutboxService",
        "OutboxProcess": "Hlidac.Prod.OutboxProcess",
        "FhirWriter": "HS.FHIRServer.Interop.Operation",
    }
    assert 'TargetConfigName As Ens.DataType.ConfigName [ InitialExpression = "OutboxProcess" ]' in (
        SOURCES["Hlidac.Prod.OutboxService"]
    )
    assert 'FhirTarget As Ens.DataType.ConfigName [ InitialExpression = "FhirWriter" ]' in (
        SOURCES["Hlidac.Prod.OutboxProcess"]
    )


def test_max_attempts_matches_the_outbox_contract():
    found = re.search(r"MaxAttempts As %Integer \[ InitialExpression = (\d+) \]",
                      SOURCES["Hlidac.Prod.OutboxProcess"])
    assert int(found.group(1)) == MAX_ATTEMPTS


def test_the_service_takes_pending_fhir_rows_in_enqueue_order_with_backoff():
    service = SOURCES["Hlidac.Prod.OutboxService"]
    assert "outbox_status = 'pending' AND job_kind = 'fhir_transaction'" in service
    assert "ORDER BY %ID" in service  # created_at follows the demo clock, which restarts
    assert 'If response.Outcome = "retry"' in service  # head of line (D33) ...
    assert "(..RetryBaseSeconds * (2 ** (response.Attempts - 1)))" in service  # ... with backoff
    assert "Quit:$Get(..NextTry(id), 0)>$ZHorolog" in service


def test_the_process_updates_only_status_columns_of_pending_rows():
    process = SOURCES["Hlidac.Prod.OutboxProcess"]
    updates = re.findall(r'"(UPDATE Hlidac\.outbox SET [^"]*)"', process)
    assert updates
    assert all("job_json" not in u and "created_at" not in u for u in updates)
    assert process.count("WHERE outbox_id = ? AND outbox_status = 'pending'") == 4


def test_the_process_posts_exactly_what_the_guard_checked():
    process = SOURCES["Hlidac.Prod.OutboxProcess"]
    assert "..Post(id, attempts, verdict.bundle, pResponse" in process
    assert "%FromJSON" not in process  # the process never re-parses the job itself
    assert "} Catch error {" in process  # any error is counted on the row, never left blocking


def verdict(check, job: str, source: str | None = None) -> dict:
    return json.loads(check(source or deploy.guard_source(), "v1", job))


def test_the_guard_in_iris_accepts_what_the_app_writes(check):
    job = fhir_item().job
    assert verdict(check, job.model_dump_json()) == {"bundle": job.bundle_json}


SAFE = fhir_item().job.bundle_json


@pytest.mark.parametrize(
    "job",
    [
        job_json({"resourceType": "Bundle", "type": "transaction", "entry": []}),
        job_json({"resourceType": "Bundle", "type": "batch", "entry": [{}]}),
        json.dumps({"kind": "email", "bundle_json": "{}"}),
        "not json",
        "[]",
        json.dumps({"kind": "fhir_transaction"}),
        json.dumps({"kind": "fhir_transaction", "bundle_json": {"resourceType": "Bundle"}}),
        # duplicate keys: Python keeps the last, another parser may keep the first
        (
            f'{{"kind": "fhir_transaction", "bundle_json": {json.dumps(SAFE)},'
            f' "bundle_json": {json.dumps(json.dumps({"resourceType": "Patient"}))}}}'
        ),
        f'{{"kind": "fhir_transaction", "bundle_json": {json.dumps(SAFE)}, "n": NaN}}',
    ],
)
def test_the_guard_in_iris_fails_closed(check, job):
    assert set(verdict(check, job)) == {"unsafe"}


def test_an_untagged_resource_is_refused_in_iris(check):
    bundle = fhir_item().job.bundle()
    del bundle["entry"][0]["resource"]["meta"]
    assert verdict(check, job_json(bundle))["unsafe"].startswith("ValueError")


def test_a_broken_guard_is_unavailable_not_a_verdict_on_the_row(check):
    broken = "raise RuntimeError('broken')"
    result = json.loads(check(broken, "v-broken", fhir_item().job.model_dump_json()))
    assert result == {"unavailable": "GuardBroken: RuntimeError"}
