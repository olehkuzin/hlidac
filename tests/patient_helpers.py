"""Helpers for the patient-app tests (WP9, WP15): an app on the in-memory store with the invented
TEST plan, and a stand-in for the assessment engine. No patient data."""

from factories import T0, alert, assessment, ui_plan, urgent_rule
from security.auth_helpers import NURSE, PATIENT_A, PATIENT_B, PATIENT_C, Mailbox

from hlidac.app import create_app
from hlidac.clock import DemoClock
from hlidac.config import Settings
from hlidac.pipeline import Pipeline
from hlidac.schemas import PatientPlan
from hlidac.store import InMemoryStore

PLAN = ui_plan()


def make_app(
    process=None,
    plans=True,
    advice_cards=None,
    extract=None,
    grading_attributes=None,
    transcribe=None,
    plan=PLAN,
    **settings,
):
    """`process(store)` builds the pipeline's process function. PATIENT_C has no plan."""
    store = InMemoryStore()
    for account in (PATIENT_A, PATIENT_B, PATIENT_C, NURSE):
        store.save_account(account)
    for patient in (PATIENT_A, PATIENT_B):
        store.save_patient_plan(
            PatientPlan(
                patient_id=patient.patient_id,
                questionnaire_url=plan.questionnaire_url,
                questionnaire_version=plan.questionnaire_version,
                care_team_id="t1",
                active_from=T0,
            )
        )
    pipeline = Pipeline(
        plans={(plan.questionnaire_url, plan.questionnaire_version): plan} if plans else {},
        process=process(store) if process else None,
        advice_cards=advice_cards or {},
        extract=extract,
        grading_attributes=grading_attributes or {},
        transcribe=transcribe,
    )
    mailbox = Mailbox()
    config = Settings(_env_file=None, secret_key="test-secret-" + "x" * 32, **settings)
    app = create_app(
        config,
        store=store,
        clock=DemoClock(T0),
        security_clock=DemoClock(T0),
        send_email=mailbox,
        pipeline=pipeline,
    )
    return app, store, mailbox


def urgent_on_fever(store):
    """A stand-in for WP4: fever answered yes is urgent; it stores the assessment and the alert."""

    def process(checkin, plan, now):
        if not any(f.link_id == "fever" and f.state == "present" for f in checkin.findings):
            return None
        result = assessment(
            assessment_id=f"a-{len(store.list_assessments(checkin.patient_id))}",
            checkin_id=checkin.checkin_id,
            patient_id=checkin.patient_id,
            status="urgent",
            findings=list(checkin.findings),
            triggered_rules=[urgent_rule()],
        )
        store.add_assessment(result)
        store.save_alert(
            alert(
                alert_id=f"al-{result.assessment_id}",
                patient_id=checkin.patient_id,
                assessment_id=result.assessment_id,
            )
        )
        return result

    return process
