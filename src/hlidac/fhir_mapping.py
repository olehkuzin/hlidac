"""Check-ins, grades and alerts as FHIR R4 transaction bundles (WP12, system-design §7).

Pure: the bundle builders read only their arguments. The `enqueue_*` helpers put a bundle in the
outbox (the store and `now` are passed in); the IRIS production posts it.

- **What we write:** `QuestionnaireResponse` (the check-in), one `Observation` per provisional
  CTCAE grade, a `Provenance`, and a `Task` for an alert. Every resource carries the `hlidac` tag
  and its own identifier in one of our systems, and is written by a conditional PUT on it, so a
  retry updates the same resource and never duplicates it. `Provenance` has no identifier in R4:
  it gets a stable `hlidac-` id and is written by PUT on that id. `fhir_guard.check_bundle` runs on
  every bundle before it is returned.
- **References:** our resources point at each other by **identifier** (`Reference.identifier`),
  never by server id, because a review or an alert update is written in a later bundle than the
  check-in, and an identifier means the same thing in every bundle. The patient is the hospital's
  own `Patient/<id>`; we reference it and never write it.
- **Statuses:** the check-in `in_progress` → `in-progress`, `completed` → `completed`;
  `processing_failed` stays `in-progress`, because a report nobody could assess must not read as
  a finished one. A grade is `preliminary`; a clinician's confirmation makes it `final`, a
  correction `amended` (with the clinician's reason as a note). An alert's Task goes `requested`
  (open) → `accepted` (acknowledged) → `completed` (resolved).
- **Only what was answered:** an item without a finding, or `not_mentioned`, has no answer; an
  unconfirmed "Ne" to a consequential item is left out (`wizard.for_assessment`), as for the rules.
  Free text goes only into the Questionnaire's free-text item. Grades come only from the assessment
  or the clinician's review; nothing here proposes a value.
"""

import hashlib
from datetime import datetime
from typing import Any

from hlidac import wizard
from hlidac.fhir_guard import ID_PREFIX, TAG_CODE, TAG_SYSTEM, check_bundle
from hlidac.schemas import (
    Alert,
    Assessment,
    Checkin,
    CtcaeGrade,
    FhirTransactionJob,
    Finding,
    GradeReview,
    MonitoringPlan,
    OutboxItem,
    PlanItem,
)
from hlidac.store import Store

CHECKIN_SYSTEM = "urn:hlidac:checkin"
GRADE_SYSTEM = "urn:hlidac:grade"
ALERT_SYSTEM = "urn:hlidac:alert"
TEAM_SYSTEM = "urn:hlidac:team"
ACCOUNT_SYSTEM = "urn:hlidac:account"
ANSWER_SYSTEM = "urn:hlidac:answer"  # as in plans.py: the Questionnaire's answerOption codings
UCUM = "http://unitsofmeasure.org"
PROVENANCE_AGENT_TYPE = "http://terminology.hl7.org/CodeSystem/provenance-participant-type"

QR_STATUS = {
    "in_progress": "in-progress",
    "completed": "completed",
    "processing_failed": "in-progress",
}
TASK_STATUS = {"open": "requested", "acknowledged": "accepted", "resolved": "completed"}
TASK_PRIORITY = {"urgent": "stat", "routine": "routine"}
REVIEW_STATUS = {"confirmed": "final", "corrected": "amended"}


def _tag() -> dict[str, Any]:
    return {"tag": [{"system": TAG_SYSTEM, "code": TAG_CODE}]}


def _identifier(system: str, value: str) -> dict[str, str]:
    return {"system": system, "value": value}


def _ref(resource_type: str, system: str, value: str) -> dict[str, Any]:
    return {"type": resource_type, "identifier": _identifier(system, value)}


def _patient(patient_id: str) -> dict[str, str]:
    return {"reference": f"Patient/{patient_id}"}


def _put(resource: dict[str, Any], system: str, value: str) -> dict[str, Any]:
    url = f"{resource['resourceType']}?identifier={system}|{value}"
    return {"resource": resource, "request": {"method": "PUT", "url": url}}


def _bundle(entries: list[dict[str, Any]]) -> dict[str, Any]:
    bundle = {"resourceType": "Bundle", "type": "transaction", "entry": entries}
    check_bundle(bundle)
    return bundle


def _hash(text: str, length: int) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:length]


# --- QuestionnaireResponse -------------------------------------------------------------------


def _answer(item: PlanItem, finding: Finding) -> dict[str, Any] | None:
    """The FHIR answer for one finding, or None if the finding gives no answer to write."""
    if finding.state == "not_mentioned":
        return None
    if finding.value is not None:
        quantity = finding.value
        return {
            "valueQuantity": {
                "value": quantity.value,
                "unit": quantity.unit,
                "system": UCUM,
                "code": quantity.unit,
            }
        }
    options = {o.code: o for o in item.options}
    code = finding.code
    if code is None:  # a quoted finding on a yes/no item: its state names the option
        code = {"present": "yes", "absent": "no", "unknown": "dont_know"}[finding.state]
    option = options.get(code)
    if option is None:
        return None
    return {
        "valueCoding": {"system": ANSWER_SYSTEM, "code": option.code, "display": option.text_cs}
    }


def questionnaire_response(checkin: Checkin, plan: MonitoringPlan) -> dict[str, Any]:
    if (checkin.questionnaire_url, checkin.questionnaire_version) != (
        plan.questionnaire_url,
        plan.questionnaire_version,
    ):
        raise ValueError("the check-in answers another Questionnaire version")
    findings = {f.link_id: f for f in wizard.for_assessment(plan, checkin).findings}
    texts = [m.text for m in checkin.messages if m.kind == "text" and m.text]
    items = []
    for item in plan.items:
        if item.type == "text":
            answers = [{"valueString": "\n\n".join(texts)}] if texts else []
        else:
            finding = findings.get(item.link_id)
            answer = _answer(item, finding) if finding is not None else None
            answers = [answer] if answer is not None else []
        if answers:
            items.append({"linkId": item.link_id, "text": item.text_cs, "answer": answers})
    patient = _patient(checkin.patient_id)
    resource: dict[str, Any] = {
        "resourceType": "QuestionnaireResponse",
        "meta": _tag(),
        "identifier": _identifier(CHECKIN_SYSTEM, checkin.checkin_id),
        "questionnaire": f"{plan.questionnaire_url}|{plan.questionnaire_version}",
        "status": QR_STATUS[checkin.status],
        "subject": patient,
        "authored": checkin.updated_at.isoformat(),
        # Who reported: the patient's own login (caregiver mode will reference a RelatedPerson).
        "author": patient,
        "source": patient,
    }
    if items:
        resource["item"] = items
    return resource


def _qr_ref(checkin: Checkin) -> dict[str, Any]:
    return _ref("QuestionnaireResponse", CHECKIN_SYSTEM, checkin.checkin_id)


# --- CTCAE grade Observations ----------------------------------------------------------------


def grade_observation(
    checkin: Checkin, assessment: Assessment, grade: CtcaeGrade, review: GradeReview | None = None
) -> dict[str, Any]:
    """The provisional grade, or, with a review, the clinician's confirmed or corrected grade."""
    shown = review.grade if review is not None else grade
    resource: dict[str, Any] = {
        "resourceType": "Observation",
        "meta": _tag(),
        "identifier": [_identifier(GRADE_SYSTEM, grade.grade_id)],
        "status": REVIEW_STATUS[shown.status] if review is not None else "preliminary",
        "code": {
            # The MedDRA code joins the coding once grades carry it (the Questionnaire has it).
            "coding": [
                {"system": f"urn:hlidac:ctcae:v{grade.ctcae_version}", "display": grade.ctcae_term}
            ],
            "text": f"CTCAE v{grade.ctcae_version} {grade.ctcae_term}",
        },
        "subject": _patient(checkin.patient_id),
        "effectiveDateTime": assessment.assessed_at.isoformat(),
        "derivedFrom": [_qr_ref(checkin)],
        "method": {"text": f"{grade.mapping_id} v{grade.mapping_version}"},
    }
    notes = []
    if shown.grade is not None:
        resource["valueInteger"] = shown.grade
    else:
        resource["valueInteger"] = shown.at_least
        notes.append(f"at least grade {shown.at_least}, needs clinical assessment")
    if review is not None:
        resource["issued"] = shown.confirmed_at.isoformat()  # type: ignore[union-attr]
        resource["performer"] = [_ref("Practitioner", ACCOUNT_SYSTEM, shown.confirmed_by or "")]
        if review.note is not None:
            notes.append(review.note)
    else:
        notes.append("provisional, patient-reported; a clinician confirms or corrects it")
    resource["note"] = [{"text": n} for n in notes]
    return resource


def _grade_entry(observation: dict[str, Any]) -> dict[str, Any]:
    return _put(observation, GRADE_SYSTEM, observation["identifier"][0]["value"])


# --- Provenance ------------------------------------------------------------------------------


def _agent(display: str, code: str = "assembler") -> dict[str, Any]:
    return {
        "type": {"coding": [{"system": PROVENANCE_AGENT_TYPE, "code": code}]},
        "who": {"display": display},
    }


def _provenance(
    key: str,
    targets: list[dict[str, Any]],
    entity: dict[str, Any] | None,
    agents: list[dict[str, Any]],
    recorded: datetime,
) -> dict[str, Any]:
    resource_id = f"{ID_PREFIX}{_hash(key, 40)}"
    resource: dict[str, Any] = {
        "resourceType": "Provenance",
        "id": resource_id,
        "meta": _tag(),
        "target": targets,
        "recorded": recorded.isoformat(),
        "agent": agents,
    }
    if entity is not None:
        resource["entity"] = [{"role": "source", "what": entity}]
    return {"resource": resource, "request": {"method": "PUT", "url": f"Provenance/{resource_id}"}}


def _engine_agents(checkin: Checkin, assessment: Assessment) -> list[dict[str, Any]]:
    agents = [_agent(f"hlidac engine {assessment.engine_version}")]
    extractors = sorted({f.extractor_version for f in checkin.findings if f.extractor_version})
    agents += [_agent(f"hlidac extractor {version}") for version in extractors]
    return agents


# --- Task ------------------------------------------------------------------------------------


def alert_task(alert: Alert, checkin: Checkin) -> dict[str, Any]:
    if alert.patient_id != checkin.patient_id:
        raise ValueError("the alert belongs to the check-in's patient")
    changed = alert.resolved_at or alert.acknowledged_at or alert.created_at
    return {
        "resourceType": "Task",
        "meta": _tag(),
        "identifier": [_identifier(ALERT_SYSTEM, alert.alert_id)],
        "status": TASK_STATUS[alert.status],
        "intent": "order",
        "priority": TASK_PRIORITY[alert.priority],
        "focus": _qr_ref(checkin),
        "for": _patient(alert.patient_id),
        "authoredOn": alert.created_at.isoformat(),
        "lastModified": changed.isoformat(),
        "owner": {"identifier": _identifier(TEAM_SYSTEM, alert.owner_team_id)},
        "restriction": {"period": {"end": alert.ack_deadline.isoformat()}},
    }


def _task_entry(alert: Alert, checkin: Checkin) -> dict[str, Any]:
    return _put(alert_task(alert, checkin), ALERT_SYSTEM, alert.alert_id)


# --- Bundles ---------------------------------------------------------------------------------


def checkin_bundle(
    checkin: Checkin, plan: MonitoringPlan, assessment: Assessment | None, alert: Alert | None
) -> dict[str, Any]:
    """The check-in, its provisional grades, their Provenance and the alert's Task."""
    if assessment is not None and (
        assessment.checkin_id != checkin.checkin_id or assessment.patient_id != checkin.patient_id
    ):
        raise ValueError("the assessment belongs to this check-in")
    if alert is not None and (
        assessment is None or alert.assessment_id != assessment.assessment_id
    ):
        raise ValueError("the alert belongs to this assessment")
    entries = [_put(questionnaire_response(checkin, plan), CHECKIN_SYSTEM, checkin.checkin_id)]
    grades = assessment.grades if assessment is not None else []
    entries += [_grade_entry(grade_observation(checkin, assessment, g)) for g in grades]  # type: ignore[arg-type]
    if alert is not None:
        entries.append(_task_entry(alert, checkin))
    if assessment is not None:
        targets = [_ref("Observation", GRADE_SYSTEM, g.grade_id) for g in grades] or [
            _qr_ref(checkin)
        ]
        entity = _qr_ref(checkin) if grades else None
        key = f"checkin:{checkin.checkin_id}:{assessment.assessment_id}"
        agents = _engine_agents(checkin, assessment)
        entries.append(_provenance(key, targets, entity, agents, assessment.assessed_at))
    return _bundle(entries)


def grade_review_bundle(
    checkin: Checkin, assessment: Assessment, review: GradeReview
) -> dict[str, Any]:
    """The clinician's confirmation (`final`) or correction (`amended`) of one provisional grade."""
    if (
        review.assessment_id != assessment.assessment_id
        or assessment.checkin_id != checkin.checkin_id
    ):
        raise ValueError("the review belongs to this assessment and check-in")
    grade = next((g for g in assessment.grades if g.grade_id == review.grade.grade_id), None)
    if grade is None:
        raise ValueError("the review reviews a grade of this assessment")
    if (grade.link_id, grade.ctcae_term, grade.ctcae_version) != (
        review.grade.link_id,
        review.grade.ctcae_term,
        review.grade.ctcae_version,
    ):
        raise ValueError("a review keeps the grade's item, term and CTCAE version")
    observation = grade_observation(checkin, assessment, grade, review)
    clinician = review.grade.confirmed_by or ""
    agents = [
        {
            "type": {"coding": [{"system": PROVENANCE_AGENT_TYPE, "code": "verifier"}]},
            "who": {"identifier": _identifier(ACCOUNT_SYSTEM, clinician)},
        }
    ]
    provenance = _provenance(
        f"grade-review:{review.review_id}",
        [_ref("Observation", GRADE_SYSTEM, grade.grade_id)],
        _qr_ref(checkin),
        agents,
        review.grade.confirmed_at,  # type: ignore[arg-type]  # a review always has its time
    )
    return _bundle([_grade_entry(observation), provenance])


def alert_bundle(alert: Alert, checkin: Checkin) -> dict[str, Any]:
    """The alert's Task with its current status (acknowledged, resolved)."""
    return _bundle([_task_entry(alert, checkin)])


# --- Outbox ----------------------------------------------------------------------------------


def _enqueue(store: Store, key: str, bundle: dict[str, Any], now: datetime) -> bool:
    item = OutboxItem(
        outbox_id=f"fhir-{_hash(key, 32)}",
        idempotency_key=key,
        created_at=now,
        job=FhirTransactionJob.of(bundle),
    )
    return store.enqueue(item)


def checkin_key(checkin: Checkin, assessment: Assessment | None) -> str:
    assessed = assessment.assessment_id if assessment is not None else "none"
    return f"checkin:{checkin.checkin_id}:{checkin.status}:{assessed}"


def enqueue_checkin(
    store: Store,
    checkin: Checkin,
    plan: MonitoringPlan,
    assessment: Assessment | None,
    alert: Alert | None,
    now: datetime,
) -> bool:
    """False if this check-in state was already enqueued (a no-op)."""
    bundle = checkin_bundle(checkin, plan, assessment, alert)
    return _enqueue(store, checkin_key(checkin, assessment), bundle, now)


def enqueue_grade_review(
    store: Store, checkin: Checkin, assessment: Assessment, review: GradeReview, now: datetime
) -> bool:
    bundle = grade_review_bundle(checkin, assessment, review)
    return _enqueue(store, f"grade-review:{review.review_id}", bundle, now)


def enqueue_alert(store: Store, alert: Alert, checkin: Checkin, now: datetime) -> bool:
    return _enqueue(
        store, f"alert:{alert.alert_id}:{alert.status}", alert_bundle(alert, checkin), now
    )
