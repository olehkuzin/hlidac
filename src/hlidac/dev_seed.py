"""Hand-made demo accounts for the in-memory store (STORE_BACKEND=memory). No patient data: every
name, id and address is invented, and the patient ids match no hospital record."""

from datetime import datetime, timedelta

from hlidac.schemas import Account, PatientContext, PatientPlan
from hlidac.store import StaticContextSource, Store

DEMO_TEAM = "team-demo"
DEMO_QUESTIONNAIRE = "urn:hlidac:questionnaire:demo"
# Invented treatment context for demo patient A: the last SACT dose 10 days before the demo clock's
# start, so demo scenario 2 (fever on day 10 after a dose) fires the fever rule for a visible reason.
DEMO_DAYS_SINCE_DOSE = 10

ACCOUNTS = [
    Account(
        account_id="acc-demo-a",
        email="pacient.a@demo.local",
        role="patient",
        display_name="Pacient A",
        patient_id="demo-patient-a",
    ),
    Account(
        account_id="acc-demo-b",
        email="pacient.b@demo.local",
        role="patient",
        display_name="Pacient B",
        patient_id="demo-patient-b",
    ),
    Account(
        account_id="acc-demo-nurse",
        email="sestra@demo.local",
        role="nurse",
        display_name="Sestra Demo",
        team_id=DEMO_TEAM,
    ),
    # The team's fallback contact (data/plans/escalation-demo-v1.yaml): told only when an alert
    # passes its acknowledgement deadline with nobody having taken it (WP18).
    Account(
        account_id="acc-demo-lead",
        email="vedouci@demo.local",
        role="nurse",
        display_name="Vedoucí sestra Demo",
        team_id=DEMO_TEAM,
    ),
]


def seed(store: Store, now: datetime) -> None:
    for account in ACCOUNTS:
        store.save_account(account)
        if account.patient_id is not None:
            store.save_patient_plan(
                PatientPlan(
                    patient_id=account.patient_id,
                    questionnaire_url=DEMO_QUESTIONNAIRE,
                    questionnaire_version="1",
                    care_team_id=DEMO_TEAM,
                    active_from=now,
                )
            )


def contexts(now: datetime) -> dict[str, PatientContext]:
    """Invented treatment contexts, relative to the demo clock. Patient A had a dose 10 days ago;
    patient B has no record (unknown context, so the engine applies the stricter rows)."""
    return {
        "demo-patient-a": PatientContext(
            patient_id="demo-patient-a",
            last_sact_dose_at=now - timedelta(days=DEMO_DAYS_SINCE_DOSE),
            regimen=None,
            immunocompromised=None,  # unknown, never assumed False
            source_refs=["MedicationAdministration/demo-invented-a1"],
            read_at=now,
        )
    }


def context_source(now: datetime) -> StaticContextSource:
    """The demo stand-in for the hospital record in IRIS (WP14 reads the real one)."""
    return StaticContextSource(contexts(now))
