"""D57: periodic check-in invitations. Invented accounts only, no patient data."""

from datetime import UTC, datetime, timedelta

from factories import T0, patient_account

from hlidac import emails, invitations
from hlidac.config import Settings
from hlidac.schemas import PatientPlan
from hlidac.store import InMemoryStore

EVERY = timedelta(minutes=15)
BASE = "https://hlidac.example.test"


def team_store() -> InMemoryStore:
    store = InMemoryStore()
    for n, email in ((1, "real.person@gmail.com"), (2, "pacient-02@demo.local")):
        store.save_account(
            patient_account(account_id=f"acc-p{n}", email=email, patient_id=f"p{n}")
        )
        store.save_patient_plan(
            PatientPlan(
                patient_id=f"p{n}",
                questionnaire_url="urn:hlidac:questionnaire:demo",
                questionnaire_version="1",
                care_team_id="team-demo",
                active_from=T0,
            )
        )
    return store


def sent_to(store: InMemoryStore) -> list[str]:
    return [o.job.to for o in store.list_outbox(kind="email")]


def test_periods_are_aligned_so_every_app_copy_agrees():
    at = datetime(2026, 10, 17, 9, 22, 41, tzinfo=UTC)
    assert invitations.period_start(at, EVERY) == datetime(2026, 10, 17, 9, 15, tzinfo=UTC)
    assert invitations.period_start(at + timedelta(minutes=8), EVERY).minute == 30


def test_one_invitation_per_patient_and_period_email_only_to_real_addresses():
    store = team_store()
    period = datetime(2026, 10, 17, 9, 15, tzinfo=UTC)
    assert invitations.invite_team(store, "team-demo", period, T0, base_url=BASE) == 2
    assert sent_to(store) == ["real.person@gmail.com"]  # @demo.local gets the in-app notice only
    assert [n.kind for n in store.list_notifications("acc-p2")] == ["checkin_due"]
    invitations.invite_team(store, "team-demo", period, T0, base_url=BASE)  # a restart, a 2nd copy
    assert sent_to(store) == ["real.person@gmail.com"]
    assert len(store.list_notifications("acc-p1")) == 1
    invitations.invite_team(store, "team-demo", period + EVERY, T0, base_url=BASE)
    assert sent_to(store) == ["real.person@gmail.com"] * 2
    assert len(store.list_notifications("acc-p2")) == 2


def test_the_email_carries_only_the_link_to_the_app():
    store = team_store()
    invitations.invite_team(store, "team-demo", T0, T0, base_url=BASE)
    [job] = [o.job for o in store.list_outbox(kind="email")]
    assert job.template_id == "checkin-due"
    assert f"{BASE}/pacient" in job.text_body


def test_undeliverable_addresses():
    assert emails.deliverable("someone@gmail.com")
    for address in ("a@demo.local", "a@x.test", "a@example", "a@localhost", "a@b.invalid"):
        assert not emails.deliverable(address)


def test_off_unless_configured():
    assert Settings(_env_file=None).checkin_invite_minutes is None
