"""WP18 part 2: acknowledgement deadlines, the fallback contact, and the demo panel that moves the
clock and invites patients. Hand-made fixtures only; every account and id is invented."""

from datetime import timedelta

import pytest
from demo_helpers import NOW, PLAN
from factories import alert, patient_account
from security.auth_helpers import Mailbox, csrf, login, new_client

from hlidac import alerts, notifications
from hlidac.alerts import Escalation, TeamEscalation, check_deadlines
from hlidac.app import create_app
from hlidac.config import Settings
from hlidac.schemas import Account, PatientPlan
from hlidac.store import InMemoryStore

BASE = "http://localhost:8000"
TEAM = "t1"
LEAD = "vedouci@demo.local"


def nurse(account_id="acc-n1", email="sestra1@demo.local", team_id=TEAM) -> Account:
    return Account(
        account_id=account_id, email=email, role="nurse", display_name="Sestra",
        team_id=team_id,
    )  # fmt: skip


def escalation(**teams: list[str]) -> Escalation:
    return Escalation(
        content_id="escalation-test",
        version="1",
        approval="DEMO — čeká na schválení FN Motol",
        approved_by=None,
        source_ref="test",
        teams={team: TeamEscalation(fallback_contacts=emails) for team, emails in teams.items()},
    )


def seeded(*accounts: Account) -> InMemoryStore:
    store = InMemoryStore()
    for account in accounts or (nurse(), nurse("acc-lead", LEAD)):
        store.save_account(account)
    store.save_account(patient_account())
    store.save_patient_plan(
        PatientPlan(
            patient_id="p1",
            questionnaire_url=PLAN.questionnaire_url,
            questionnaire_version=PLAN.questionnaire_version,
            care_team_id=TEAM,
            active_from=NOW,
        )
    )
    return store


def check(store, now, config=None):
    return check_deadlines(store, now, escalation=config or escalation(t1=[LEAD]), base_url=BASE)


def emails_to(store, address):
    return [o.job for o in store.list_outbox() if o.job.kind == "email" and o.job.to == address]


def audited(store, action):
    return [e for e in store.list_audit() if e.action == action]


DUE = NOW + timedelta(minutes=30)


# --- check_deadlines -------------------------------------------------------------------------


def test_an_open_alert_past_its_deadline_goes_to_the_fallback_contact_once():
    store = seeded()
    store.save_alert(alert(owner_team_id=TEAM, created_at=NOW, ack_deadline=DUE))
    assert check(store, DUE - timedelta(seconds=1)) == []  # not yet
    [raised] = check(store, DUE)
    assert raised.fallback_level == 1 and store.get_alert("al1").fallback_level == 1
    assert store.get_alert("al1").status == "open"  # still the team's to take; nothing closed
    [note] = store.list_notifications("acc-lead")
    assert note.kind == "alert_escalated" and note.link == "/sestra/pripad/al1"
    assert [d.channel for d in note.deliveries] == ["in_app", "email"]
    [mail] = emails_to(store, LEAD)
    assert mail.template_id == "alert-escalated" and f"{BASE}/sestra\n" in mail.text_body
    assert "p1" not in mail.text_body and "al1" not in mail.text_body  # no ids, no clinical text
    assert store.list_notifications("acc-n1") == []  # the escalation goes only to the fallback
    [entry] = audited(store, "alert_escalated")
    assert entry.details["fallback_level"] == 1 and entry.details["escalation"] == "1"

    assert check(store, DUE + timedelta(hours=5)) == []  # a later check adds nothing
    assert len(store.list_notifications("acc-lead")) == 1 and len(emails_to(store, LEAD)) == 1


@pytest.mark.parametrize("status", ["acknowledged", "resolved"])
def test_an_alert_someone_took_is_never_escalated(status):
    store = seeded()
    taken = {"acknowledged_by": "acc-n1", "acknowledged_at": NOW + timedelta(minutes=1)}
    if status == "resolved":
        taken |= {"resolved_by": "acc-n1", "resolved_at": NOW + timedelta(minutes=2),
                  "resolution_note": "TEST"}  # fmt: skip
    store.save_alert(alert(owner_team_id=TEAM, ack_deadline=DUE, status=status, **taken))
    assert check(store, DUE + timedelta(days=1)) == []
    assert store.list_notifications("acc-lead") == []


def test_a_team_without_a_fallback_contact_escalates_visibly_and_retries():
    store = seeded()
    store.save_alert(alert(owner_team_id=TEAM, ack_deadline=DUE))
    [raised] = check(store, DUE, escalation())  # nobody configured
    assert raised.fallback_level == 1
    assert notifications.failure_open(store, "al1")  # listed in Unresolved
    [failed] = audited(store, notifications.FAILED)
    assert failed.details["error"] == "LookupError"
    assert check(store, DUE + timedelta(minutes=5)) == []  # configured now: the retry completes
    assert not notifications.failure_open(store, "al1")
    assert [n.kind for n in store.list_notifications("acc-lead")] == ["alert_escalated"]


def test_a_fallback_contact_outside_the_team_is_refused():
    store = seeded(nurse(), nurse("acc-other", LEAD, team_id="t2"))
    store.save_alert(alert(owner_team_id=TEAM, ack_deadline=DUE))
    check(store, DUE)
    assert notifications.failure_open(store, "al1")
    assert store.list_notifications("acc-other") == []  # they couldn't open the case anyway


def test_the_demo_escalation_file_names_the_demo_fallback_nurse():
    config = alerts.read_escalation()
    assert "DEMO" in config.approval and config.approved_by is None
    assert config.fallback_emails("team-demo") == [LEAD]
    assert config.fallback_emails("unknown") == []


# --- the live app: first notice, deadline, fallback ------------------------------------------


def demo_app(panel=True):
    mailbox = Mailbox()
    config = Settings(_env_file=None, secret_key="test-secret-" + "x" * 32, demo_panel=panel)
    return create_app(config, send_email=mailbox), mailbox


def signed_in(app, mailbox, email):
    client = new_client(app)
    login(client, mailbox, email)
    return client


def post(client, path, form_page="/demo", **data):
    """POST like the form on `form_page` does (the session's CSRF token is read from it)."""
    return client.post(path, data={"csrf_token": csrf(client, form_page), **data})


def urgent_report(app, mailbox):
    patient = signed_in(app, mailbox, "pacient.a@demo.local")
    token = csrf(patient, "/pacient")
    base = patient.post("/pacient/hlaseni", data={"csrf_token": token}).headers["location"]
    page = f"{base}/otazka/fever"
    response = patient.post(page, data={"csrf_token": csrf(patient, page), "answer": "yes"})
    confirm = response.headers["location"]
    patient.post(confirm, data={"csrf_token": csrf(patient, confirm)})
    page = f"{base}/otazka/temperature"  # D54: the reported fever waits for the reading
    patient.post(
        page, data={"csrf_token": csrf(patient, page), "answer": "typed_value", "number": "38,5"}
    )
    [urgent] = app.state.store.list_alerts(status="open")
    return urgent


def test_live_unacknowledged_urgent_alert_reaches_the_fallback_nurse_when_the_clock_moves():
    app, mailbox = demo_app()
    store = app.state.store
    urgent = urgent_report(app, mailbox)
    # the first notice goes to the team's nurse, not to the fallback contact
    assert [n.kind for n in store.list_notifications("acc-demo-nurse")] == ["urgent_alert"]
    assert store.list_notifications("acc-demo-lead") == []

    nurse = signed_in(app, mailbox, "sestra@demo.local")
    response = post(nurse, "/demo/cas", step="15m")
    assert response.headers["location"] == "/demo"
    assert "předaná záložnímu kontaktu: 0" in nurse.get("/demo").text
    post(nurse, "/demo/cas", step="1h")  # past the 30-minute deadline
    assert "předaná záložnímu kontaktu: 1" in nurse.get("/demo").text

    assert store.get_alert(urgent.alert_id).fallback_level == 1
    assert [n.kind for n in store.list_notifications("acc-demo-lead")] == ["alert_escalated"]
    [mail] = emails_to(store, LEAD)
    assert mail.template_id == "alert-escalated"
    queue = nurse.get("/sestra").text
    assert "po termínu" in queue and "předáno záložnímu kontaktu" in queue
    case = nurse.get(f"/sestra/pripad/{urgent.alert_id}").text
    assert "nepřevzato včas" in case
    # the fallback nurse opens the case from the notice
    lead = signed_in(app, mailbox, LEAD)
    assert lead.get(f"/sestra/pripad/{urgent.alert_id}").status_code == 200
    actions = [e.action for e in store.list_audit("clock")]
    assert actions == ["demo_clock_advanced", "demo_clock_advanced"]


def test_a_nurse_page_load_checks_the_deadlines_too():
    app, mailbox = demo_app()
    urgent = urgent_report(app, mailbox)
    app.state.clock.advance(timedelta(hours=1))  # moved without the panel
    nurse = signed_in(app, mailbox, "sestra@demo.local")
    nurse.get("/sestra/oznameni")
    assert app.state.store.get_alert(urgent.alert_id).fallback_level == 1


def test_an_acknowledged_alert_stays_with_its_nurse_when_the_clock_moves():
    app, mailbox = demo_app()
    urgent = urgent_report(app, mailbox)
    nurse = signed_in(app, mailbox, "sestra@demo.local")
    case = f"/sestra/pripad/{urgent.alert_id}"
    post(nurse, f"{case}/prevzit", form_page=case)
    post(nurse, "/demo/cas", step="1d")
    assert app.state.store.get_alert(urgent.alert_id).fallback_level == 0
    assert app.state.store.list_notifications("acc-demo-lead") == []


# --- the demo panel --------------------------------------------------------------------------


def test_the_panel_is_off_unless_the_setting_turns_it_on():
    app, mailbox = demo_app(panel=False)
    nurse = signed_in(app, mailbox, "sestra@demo.local")
    assert nurse.get("/demo").status_code == 404
    assert 'href="/demo"' not in nurse.get("/sestra").text
    on, mailbox = demo_app()
    assert 'href="/demo"' in signed_in(on, mailbox, "sestra@demo.local").get("/sestra").text


def test_the_panel_is_for_nurses_only_and_needs_a_login_and_csrf():
    app, mailbox = demo_app()
    assert new_client(app).get("/demo").headers["location"] == "/prihlaseni"
    patient = signed_in(app, mailbox, "pacient.a@demo.local")
    assert patient.get("/demo").status_code == 403
    nurse = signed_in(app, mailbox, "sestra@demo.local")
    before = app.state.clock.now()
    assert nurse.post("/demo/cas", data={"step": "1d"}).status_code == 403  # no CSRF token
    assert post(nurse, "/demo/cas", step="-1d").status_code == 400  # only the fixed steps
    assert app.state.clock.now() == before


def test_inviting_a_patient_notifies_in_app_and_by_email_with_only_the_app_link():
    app, mailbox = demo_app()
    store = app.state.store
    nurse = signed_in(app, mailbox, "sestra@demo.local")
    page = nurse.get("/demo").text
    assert "Pacient A" in page and "Pacient B" in page
    post(nurse, "/demo/pozvat", patient_id="demo-patient-a")
    post(nurse, "/demo/pozvat", patient_id="demo-patient-a")  # a double click adds nothing
    [note] = store.list_notifications("acc-demo-a")
    assert note.kind == "checkin_due" and note.link == "/pacient"
    assert notifications.text_for(note) == "Je čas na krátké hlášení o tom, jak se cítíte."
    [mail] = emails_to(store, "pacient.a@demo.local")
    assert mail.template_id == "checkin-due" and f"{BASE}/pacient\n" in mail.text_body
    assert "demo-patient-a" not in mail.text_body
    patient = signed_in(app, mailbox, "pacient.a@demo.local")
    assert "Nová oznámení: 1" in patient.get("/pacient").text
    clicks = [e for e in store.list_audit("patient:demo-patient-a")
              if e.action == "demo_checkin_invited"]  # fmt: skip
    # every click is audited; both point to the one notification
    assert [e.actor for e in clicks] == ["acc-demo-nurse"] * 2
    assert {e.details["notification"] for e in clicks} == {note.notification_id}


def test_a_nurse_cannot_invite_another_teams_patient():
    app, mailbox = demo_app()
    store = app.state.store
    store.save_account(nurse("acc-x", "jina@demo.local", team_id="team-other"))
    other = signed_in(app, mailbox, "jina@demo.local")
    assert post(other, "/demo/pozvat", patient_id="demo-patient-a").status_code == 403
    assert "Pacient A" not in other.get("/demo").text
    assert store.list_notifications("acc-demo-a") == []
