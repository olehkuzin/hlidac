"""WP12: check-ins, grades and alerts become safe, tagged, idempotent FHIR transaction bundles."""

import json
from datetime import timedelta

import pytest
from factories import T0, alert, assessment, checkin, grade, ui_plan, urgent_rule

from hlidac import fhir_mapping, wizard
from hlidac.fhir_guard import TAG_CODE, TAG_SYSTEM, WRITABLE_TYPES, check_bundle
from hlidac.plans import load_plans
from hlidac.schemas import DONT_KNOW, TYPED_VALUE, GradeReview, InboundMessage
from hlidac.store import InMemoryStore

PLAN = ui_plan()


def answered(state, link_id, code, number=None, confirmed=True):
    button = wizard.button_answer(PLAN.item(link_id), code, number)
    finding = wizard.finding_from_answer(button, f"m-{link_id}")
    if confirmed:
        finding = finding.evolve(confirmed=True)
    return state.evolve(findings=wizard.with_finding(state.findings, finding))


def text_message(text="TEST text"):
    return InboundMessage(
        message_id="mt",
        checkin_id="c1",
        patient_id="p1",
        session_account_id="acc-p1",
        reported_by="patient",
        received_at=T0,
        kind="text",
        text=text,
    )


def full_checkin(status="completed"):
    state = checkin(status=status, messages=[text_message()])
    state = answered(state, "fever", "yes")
    state = answered(state, "temperature", TYPED_VALUE, 38.4)
    return answered(state, "nausea", DONT_KNOW)


def resources(bundle):
    return [entry["resource"] for entry in bundle["entry"]]


def of_type(bundle, resource_type):
    return [r for r in resources(bundle) if r["resourceType"] == resource_type]


def answers(bundle):
    [qr] = of_type(bundle, "QuestionnaireResponse")
    return {item["linkId"]: item["answer"] for item in qr.get("item", [])}


def test_every_entry_is_a_tagged_conditional_write_of_ours():
    bundle = fhir_mapping.checkin_bundle(
        full_checkin(), PLAN, assessment(findings=[]), alert(assessment_id="a1")
    )
    check_bundle(bundle)
    for entry in bundle["entry"]:
        resource, request = entry["resource"], entry["request"]
        assert resource["resourceType"] in WRITABLE_TYPES
        assert {"system": TAG_SYSTEM, "code": TAG_CODE} in resource["meta"]["tag"]
        assert request["method"] == "PUT"
        assert "Patient" not in request["url"]
    assert [r["resourceType"] for r in resources(bundle)] == [
        "QuestionnaireResponse",
        "Observation",
        "Task",
        "Provenance",
    ]


def test_answers_keep_exactly_what_the_patient_gave():
    bundle = fhir_mapping.checkin_bundle(full_checkin(), PLAN, None, None)
    got = answers(bundle)
    assert got["fever"] == [
        {"valueCoding": {"system": "urn:hlidac:answer", "code": "yes", "display": "TEST Ano"}}
    ]
    assert got["temperature"] == [
        {
            "valueQuantity": {
                "value": 38.4,
                "unit": "Cel",
                "system": "http://unitsofmeasure.org",
                "code": "Cel",
            }
        }
    ]
    assert got["nausea"][0]["valueCoding"]["code"] == DONT_KNOW  # "Nevím", never "Ne"
    assert got["note"] == [{"valueString": "TEST text"}]  # free text only in the text item


def test_no_answer_for_items_not_answered_or_an_unconfirmed_no():
    state = answered(checkin(), "fever", "no", confirmed=False)
    assert answers(fhir_mapping.checkin_bundle(state, PLAN, None, None)) == {}
    confirmed = answered(checkin(), "fever", "no")
    assert (
        answers(fhir_mapping.checkin_bundle(confirmed, PLAN, None, None))["fever"][0][
            "valueCoding"
        ]["code"]
        == "no"
    )


@pytest.mark.parametrize(
    ("status", "fhir_status"),
    [
        ("in_progress", "in-progress"),
        ("completed", "completed"),
        ("processing_failed", "in-progress"),  # an unassessed report never reads as finished
    ],
)
def test_questionnaire_response_status(status, fhir_status):
    failure = "processing error: RuntimeError" if status == "processing_failed" else None
    state = checkin(status=status, failure=failure)
    [qr] = of_type(fhir_mapping.checkin_bundle(state, PLAN, None, None), "QuestionnaireResponse")
    assert qr["status"] == fhir_status
    assert qr["questionnaire"] == "urn:hlidac:questionnaire:demo|1"
    assert qr["subject"] == {"reference": "Patient/p1"}
    assert qr["identifier"] == {"system": "urn:hlidac:checkin", "value": "c1"}


def test_provisional_grade_is_preliminary_and_points_to_the_report():
    bundle = fhir_mapping.checkin_bundle(checkin(), PLAN, assessment(findings=[]), None)
    [observation] = of_type(bundle, "Observation")
    assert observation["status"] == "preliminary"
    assert observation["valueInteger"] == 1
    assert observation["code"]["coding"][0]["system"] == "urn:hlidac:ctcae:v5.0"
    assert observation["derivedFrom"] == [
        {
            "type": "QuestionnaireResponse",
            "identifier": {"system": "urn:hlidac:checkin", "value": "c1"},
        }
    ]
    assert observation["method"] == {"text": "map-nausea v1"}
    [provenance] = of_type(bundle, "Provenance")
    assert provenance["id"].startswith("hlidac-")
    assert provenance["target"][0]["identifier"]["value"] == "g1"
    assert {"display": "hlidac engine 0.1"} in [a["who"] for a in provenance["agent"]]


def test_at_least_grade_says_it_needs_clinical_assessment():
    at_least = grade(grade=None, at_least=2, needs_clinical_assessment=True)
    result = assessment(findings=[], grades=[at_least], status="routine_review")
    [observation] = of_type(
        fhir_mapping.checkin_bundle(checkin(), PLAN, result, None), "Observation"
    )
    assert observation["valueInteger"] == 2
    assert "at least grade 2, needs clinical assessment" in [n["text"] for n in observation["note"]]


def review(status="confirmed", value=1, note=None):
    reviewed = grade(status=status, grade=value, confirmed_by="acc-n1", confirmed_at=T0)
    return GradeReview(review_id="r1", assessment_id="a1", grade=reviewed, note=note)


def test_confirmation_makes_the_grade_final():
    bundle = fhir_mapping.grade_review_bundle(checkin(), assessment(findings=[]), review())
    [observation] = of_type(bundle, "Observation")
    assert observation["status"] == "final"
    assert observation["identifier"][0]["value"] == "g1"  # the same resource as the provisional
    assert observation["performer"][0]["identifier"]["value"] == "acc-n1"
    [provenance] = of_type(bundle, "Provenance")
    assert provenance["agent"][0]["who"]["identifier"]["value"] == "acc-n1"


def test_correction_is_amended_with_the_reason():
    corrected = review(status="corrected", value=2, note="TEST důvod")
    [observation] = of_type(
        fhir_mapping.grade_review_bundle(checkin(), assessment(findings=[]), corrected),
        "Observation",
    )
    assert (observation["status"], observation["valueInteger"]) == ("amended", 2)
    assert "TEST důvod" in [n["text"] for n in observation["note"]]


def test_review_of_a_grade_not_in_the_assessment_is_refused():
    other = GradeReview(
        review_id="r1",
        assessment_id="a1",
        grade=grade(grade_id="g9", status="confirmed", confirmed_by="acc-n1", confirmed_at=T0),
    )
    with pytest.raises(ValueError):
        fhir_mapping.grade_review_bundle(checkin(), assessment(findings=[]), other)


@pytest.mark.parametrize(
    ("changes", "status"),
    [
        ({}, "requested"),
        (
            {"status": "acknowledged", "acknowledged_by": "acc-n1", "acknowledged_at": T0},
            "accepted",
        ),
        (
            {
                "status": "resolved",
                "acknowledged_by": "acc-n1",
                "acknowledged_at": T0,
                "resolved_by": "acc-n1",
                "resolved_at": T0 + timedelta(minutes=5),
                "resolution_note": "TEST",
            },
            "completed",
        ),
    ],
)
def test_task_follows_the_alert(changes, status):
    [task] = resources(fhir_mapping.alert_bundle(alert(**changes), checkin()))
    assert task["status"] == status
    assert task["priority"] == "stat"
    assert task["intent"] == "order"
    assert task["restriction"]["period"]["end"] == (T0 + timedelta(minutes=30)).isoformat()
    assert task["focus"]["identifier"] == {"system": "urn:hlidac:checkin", "value": "c1"}
    assert "TEST" not in json.dumps(task)  # no free text (resolution notes stay in SQL)


def test_enqueue_is_idempotent_and_deterministic():
    store = InMemoryStore()
    urgent = assessment(findings=[], status="urgent", triggered_rules=[urgent_rule()])
    args = (full_checkin(), PLAN, urgent, alert())
    assert fhir_mapping.enqueue_checkin(store, *args, T0)
    assert not fhir_mapping.enqueue_checkin(store, *args, T0 + timedelta(minutes=1))
    assert fhir_mapping.checkin_bundle(*args) == fhir_mapping.checkin_bundle(*args)
    assert fhir_mapping.enqueue_grade_review(store, args[0], urgent, review(), T0)
    assert not fhir_mapping.enqueue_grade_review(store, args[0], urgent, review(), T0)
    assert fhir_mapping.enqueue_alert(store, alert(), args[0], T0)
    assert not fhir_mapping.enqueue_alert(store, alert(), args[0], T0)
    keys = [item.idempotency_key for item in store.list_outbox()]
    assert keys == ["checkin:c1:completed:a1", "grade-review:r1", "alert:al1:open"]
    for item in store.list_outbox():
        check_bundle(item.job.bundle())


def test_mismatched_records_are_refused():
    with pytest.raises(ValueError):
        fhir_mapping.checkin_bundle(checkin(), PLAN, assessment(checkin_id="c2"), None)
    with pytest.raises(ValueError):
        fhir_mapping.checkin_bundle(checkin(), PLAN, None, alert())
    with pytest.raises(ValueError):
        fhir_mapping.alert_bundle(alert(patient_id="p2"), checkin())


@pytest.mark.parametrize("plan", list(load_plans().values()), ids=lambda p: p.questionnaire_url)
def test_real_demo_plan_maps_and_passes_the_guard(plan):
    state = checkin(
        questionnaire_url=plan.questionnaire_url, questionnaire_version=plan.questionnaire_version
    )
    fever = wizard.finding_from_answer(wizard.button_answer(plan.item("fever"), "no"), "m1")
    state = state.evolve(findings=[fever.evolve(confirmed=True)], messages=[text_message()])
    bundle = fhir_mapping.checkin_bundle(state, plan, None, None)
    check_bundle(bundle)
    got = answers(bundle)
    assert got["fever"][0]["valueCoding"]["display"] == plan.item("fever").options[1].text_cs
    assert got["other"] == [{"valueString": "TEST text"}]
