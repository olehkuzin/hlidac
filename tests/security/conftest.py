"""WP30 fixtures: an app on an in-memory store with invented accounts, controllable clocks and a
captured mailbox. No patient data."""

import pytest
from auth_helpers import NURSE, PATIENT_A, PATIENT_B, PATIENT_C, Mailbox, new_client
from factories import T0
from fastapi.testclient import TestClient

from hlidac.app import create_app
from hlidac.clock import DemoClock
from hlidac.config import Settings
from hlidac.schemas import PatientPlan
from hlidac.store import InMemoryStore


@pytest.fixture
def store() -> InMemoryStore:
    store = InMemoryStore()
    for account, team in ((PATIENT_A, "t1"), (PATIENT_B, "t1"), (PATIENT_C, "t2"), (NURSE, None)):
        store.save_account(account)
        if team is not None:
            store.save_patient_plan(
                PatientPlan(
                    patient_id=account.patient_id,
                    questionnaire_url="urn:hlidac:questionnaire:demo",
                    questionnaire_version="1",
                    care_team_id=team,
                    active_from=T0,
                )
            )
    return store


@pytest.fixture
def security_clock() -> DemoClock:
    return DemoClock(T0)


@pytest.fixture
def mailbox() -> Mailbox:
    return Mailbox()


@pytest.fixture
def app(store, security_clock, mailbox):
    config = Settings(_env_file=None, secret_key="test-secret-" + "x" * 32)
    return create_app(
        config, store=store, clock=DemoClock(T0), security_clock=security_clock, send_email=mailbox
    )


@pytest.fixture
def client(app) -> TestClient:
    return new_client(app)
