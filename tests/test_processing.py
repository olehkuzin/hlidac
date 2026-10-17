"""WP18 part 1: the live `Pipeline.process` (assess, store, alert) and its wiring in the app.
Hand-made fixtures only; the demo accounts and contexts are invented (dev_seed)."""

import html
import re
from datetime import timedelta

import pytest
from demo_helpers import ALL_CLEAR, MAPPING, NOW, PLAN, RULES, context, findings
from factories import checkin
from security.auth_helpers import Mailbox, csrf, login, new_client

from hlidac import assessment as engine
from hlidac import dev_seed
from hlidac.app import create_app
from hlidac.config import Settings
from hlidac.processing import (
    ROUTINE_ACK_HOURS,
    URGENT_ACK_MINUTES,
    alert_of,
    check_plans,
    grouped_ids,
    make_process,
)
from hlidac.schemas import TYPED_VALUE, PatientPlan
from hlidac.store import InMemoryStore, StaticContextSource


class Broken:
    def get_context(self, patient_id):
        raise ConnectionError("IRIS down")


def setup(contexts=None, source=None, **kw):
    store = InMemoryStore()
    store.save_patient_plan(
        PatientPlan(
            patient_id="p1",
            questionnaire_url=PLAN.questionnaire_url,
            questionnaire_version=PLAN.questionnaire_version,
            care_team_id="t1",
            active_from=NOW,
        )
    )
    if source is None:
        source = StaticContextSource({"p1": context()} if contexts is None else contexts)
    return store, make_process(store, source, plans=[PLAN], base_url="http://localhost:8000", **kw)


def sent(found, **kw):
    return checkin(findings=found, status="completed", **kw)


def audited(store, action):
    return [e for e in store.list_audit() if e.action == action]


# --- urgent ----------------------------------------------------------------------------------


def test_fever_ten_days_after_a_dose_is_urgent_with_one_alert_before_returning():
    store, process = setup()
    result = process(sent(findings(fever="yes", temperature=38.4)), PLAN, NOW)
    assert result.status == "urgent"
    assert store.get_assessment(result.assessment_id) == result
    [alert] = store.list_alerts()
    assert (alert.priority, alert.status, alert.owner_team_id) == ("urgent", "open", "t1")
    assert alert.assessment_id == result.assessment_id
    assert alert.ack_deadline == NOW + timedelta(minutes=URGENT_ACK_MINUTES)
    rule = next(t for t in result.triggered_rules if t.rule_id == "ukons-red")
    assert rule.inputs["days_since_dose"] == 10


def test_urgent_mid_report_is_stored_and_alerted_at_once():
    store, process = setup()
    partial = checkin(findings=findings({}, fever="yes"))  # still in progress, much missing
    result = process(partial, PLAN, NOW)
    assert result.status == "urgent" and result.missing_link_ids
    assert len(store.list_alerts()) == 1


def test_processing_the_same_report_twice_keeps_one_assessment_and_one_alert():
    store, process = setup()
    report = sent(findings(fever="yes"))
    first = process(report, PLAN, NOW)
    again = process(report, PLAN, NOW + timedelta(minutes=5))
    assert again == first
    assert len(store.list_assessments("p1")) == 1 and len(store.list_alerts()) == 1


def test_a_resolved_alert_is_not_raised_again_by_a_retry():
    store, process = setup()
    report = sent(findings(fever="yes"))
    process(report, PLAN, NOW)
    [alert] = store.list_alerts()
    store.save_alert(
        alert.evolve(status="resolved", acknowledged_by="n", acknowledged_at=NOW,
                     resolved_by="n", resolved_at=NOW, resolution_note="TEST")
    )  # fmt: skip
    process(report, PLAN, NOW)
    assert len(store.list_alerts()) == 1


def test_a_worse_assessment_of_the_same_report_renews_the_alert_and_a_milder_one_groups():
    store, process = setup()
    process(sent(findings(vomiting="yes", vomiting_episodes="episodes_3_5")), PLAN, NOW)
    worse = process(sent(findings(fever="yes")), PLAN, NOW + timedelta(minutes=1))
    assert [a.priority for a in store.list_alerts()] == ["routine", "urgent"]
    later = NOW + timedelta(minutes=2)
    milder = process(sent(findings(fever="yes", temperature=38.0)), PLAN, later)
    assert milder.status == "urgent" and milder.assessment_id != worse.assessment_id
    assert len(store.list_alerts()) == 2
    assert audited(store, "alert_grouped")


def test_no_care_team_fails_visibly_after_storing_the_urgent_assessment():
    store, process = setup(contexts={})
    report = sent(findings(fever="yes"), patient_id="p-no-team")
    with pytest.raises(LookupError):
        process(report, PLAN, NOW)
    [stored] = store.list_assessments("p-no-team")
    assert stored.status == "urgent"  # the patient app still shows the urgent screen


# --- not urgent ------------------------------------------------------------------------------


def test_an_all_clear_report_is_no_escalation_without_an_alert():
    store, process = setup()
    result = process(sent(findings()), PLAN, NOW)
    assert result.status == "no_escalation"
    assert store.list_alerts() == []
    assert store.get_assessment(result.assessment_id) == result


def test_one_amber_rating_is_a_routine_alert_due_within_the_rules_hours():
    store, process = setup()
    result = process(sent(findings(vomiting="yes", vomiting_episodes="episodes_3_5")), PLAN, NOW)
    assert result.status == "routine_review"
    [alert] = store.list_alerts()
    assert alert.priority == "routine"
    assert alert.ack_deadline == NOW + timedelta(hours=24)


def test_a_routine_review_without_hours_uses_the_default_deadline():
    store, process = setup()
    result = process(sent(findings(chills="yes", chills_severity="shivering")), PLAN, NOW)
    assert result.status == "routine_review"
    [alert] = store.list_alerts()
    assert alert.ack_deadline == NOW + timedelta(hours=ROUTINE_ACK_HOURS)


def test_a_report_still_being_filled_in_is_not_stored_unless_urgent():
    store, process = setup()
    assert process(checkin(findings=findings({}, nausea="no")), PLAN, NOW) is None
    assert store.list_assessments("p1") == [] and store.list_alerts() == []


@pytest.mark.parametrize("red_flag", ["fever", "chills", "unwell", "diarrhoea_blood"])
@pytest.mark.parametrize("answer", [None, "dont_know"])
def test_a_missing_red_flag_answer_never_ends_as_no_escalation(red_flag, answer):
    store, process = setup()
    result = process(sent(findings(**{red_flag: answer})), PLAN, NOW)
    assert result.status != "no_escalation"
    assert red_flag in result.missing_link_ids
    if result.status == "insufficient_information":
        assert store.list_alerts() == []  # the nurse's Unresolved list shows it instead


# --- context ---------------------------------------------------------------------------------


def test_a_failing_context_source_still_assesses_on_the_stricter_row_and_is_audited():
    store, process = setup(source=Broken())
    result = process(sent(findings(fever="yes", temperature=38.4)), PLAN, NOW)
    assert result.context is None and result.status == "urgent"
    assert len(store.list_alerts()) == 1
    [entry] = audited(store, "context_read_failed")
    assert entry.details["error"] == "ConnectionError"


def test_a_failing_context_source_never_makes_an_all_clear_look_assessed_with_context():
    store, process = setup(source=Broken())
    result = process(sent(findings()), PLAN, NOW)
    assert result.context is None  # the record shows the context was unknown
    assert audited(store, "context_read_failed")


def test_another_patients_context_is_not_used():
    store, process = setup(contexts={"p1": context().evolve(patient_id="p2")})
    result = process(sent(findings(fever="yes")), PLAN, NOW)
    assert result.context is None and result.status == "urgent"
    assert audited(store, "context_read_failed")


# --- traceability ----------------------------------------------------------------------------


def test_the_assessment_records_its_versions_and_replays_exactly():
    store, process = setup()
    result = process(sent(findings(fever="yes", temperature=38.4)), PLAN, NOW)
    assert (result.ruleset_version, result.mapping_version, result.engine_version) == (
        RULES.version,
        MAPPING.version,
        engine.ENGINE_VERSION,
    )
    assert engine.replay(store.get_assessment(result.assessment_id), PLAN, MAPPING, RULES) == result
    [entry] = audited(store, "assessment_created")
    assert entry.details["ruleset"] == RULES.version and entry.target.endswith(result.assessment_id)


# --- advice hook (WP6) -----------------------------------------------------------------------


def test_the_card_hook_runs_only_for_no_escalation():
    calls = []

    def select(assessment, plan):
        calls.append(assessment.status)
        return ("card-x", "1")

    plan = PLAN.evolve(advice_card_ids=["card-x"])
    _, process = setup(select_card=select)
    calm = process(sent(findings()), plan, NOW)
    assert (calm.advice_card_id, calm.advice_card_version) == ("card-x", "1")
    urgent = process(sent(findings(fever="yes"), checkin_id="c2"), plan, NOW)
    assert urgent.advice_card_id is None
    assert calls == ["no_escalation"]


def test_a_card_outside_the_plan_fails_visibly():
    _, process = setup(select_card=lambda a, p: ("card-unknown", "1"))
    with pytest.raises(ValueError, match="not in the patient's plan"):
        process(sent(findings()), PLAN, NOW)


# --- content check at start-up ---------------------------------------------------------------


def test_content_that_does_not_fit_the_plan_fails_at_start_up():
    other = PLAN.evolve(rule_ids=PLAN.rule_ids[:-1])
    with pytest.raises(ValueError, match="other rules"):
        check_plans([other], MAPPING, RULES)
    with pytest.raises(ValueError):
        make_process(
            InMemoryStore(), StaticContextSource({}), plans=[other], base_url="http://localhost:8000"
        )


def test_a_plan_without_a_free_text_item_fails_at_start_up():
    no_text = PLAN.evolve(items=[i for i in PLAN.items if i.type != "text"])
    with pytest.raises(ValueError, match="free-text item"):
        check_plans([no_text], MAPPING, RULES)


# --- the live app ----------------------------------------------------------------------------


def demo_app():
    """The app exactly as `uv run hlidac` builds it (memory store, dev seed, live process)."""
    mailbox = Mailbox()
    config = Settings(_env_file=None, secret_key="test-secret-" + "x" * 32)
    return create_app(config, send_email=mailbox), mailbox


def signed_in(app, mailbox, email):
    client = new_client(app)
    login(client, mailbox, email)
    return client


def answer(client, base, link_id, value, **extra):
    """Answer one question and confirm it if asked (red flags, numbers)."""
    page = f"{base}/otazka/{link_id}"
    form = {"csrf_token": csrf(client, page), "answer": value, **extra}
    response = client.post(page, data=form)
    location = response.headers["location"]
    if "/potvrdit/" in location:
        response = client.post(location, data={"csrf_token": csrf(client, location)})
        location = response.headers["location"]
    return location


def next_page(client, location):
    """Where "/dalsi" sends the patient: the next question, or the location itself."""
    if location.endswith("/dalsi"):
        return client.get(location, follow_redirects=False).headers["location"]
    return location


def start(client):
    token = csrf(client, "/pacient")
    return client.post("/pacient/hlaseni", data={"csrf_token": token}).headers["location"]


def test_the_default_app_assesses_with_the_invented_demo_context():
    app, _ = demo_app()
    assert app.state.pipeline.process is not None
    source = dev_seed.context_source(app.state.clock.now())
    a = source.get_context("demo-patient-a")
    assert app.state.clock.now() - a.last_sact_dose_at == timedelta(days=10)
    assert source.get_context("demo-patient-b") is None


def test_live_fever_report_shows_the_urgent_screen_and_reaches_the_nurse_queue():
    app, mailbox = demo_app()
    store = app.state.store
    patient = signed_in(app, mailbox, "pacient.a@demo.local")
    base = start(patient)
    # D54: "Ano" to fever asks the thermometer reading next, before anything else
    assert next_page(patient, answer(patient, base, "fever", "yes")) == f"{base}/otazka/temperature"
    assert store.list_alerts(status="open") == []
    result = answer(patient, base, "temperature", TYPED_VALUE, number="38,5")
    assert result == f"{base}/vysledek"  # no other question first: urgent never waits
    page = patient.get(result).text
    assert "Zavolejte hned svému týmu" in page
    # the team's line is shown first; the ambulance action is held back for the stricter screen
    assert 'href="tel:+420000000000"' in page and "+420 000 000 000" in page
    assert 'href="tel:155"' not in page
    assert "Váš tým jsme o hlášení informovali." in page
    assert "CTCAE" not in page and "UKONS" not in page

    [alert] = store.list_alerts(status="open")
    assert alert.priority == "urgent" and alert.patient_id == "demo-patient-a"
    nurse = signed_in(app, mailbox, "sestra@demo.local")
    queue = nurse.get("/sestra").text
    assert f"/sestra/pripad/{alert.alert_id}" in queue and "Naléhavé" in queue
    case = nurse.get(f"/sestra/pripad/{alert.alert_id}").text
    assert "5. Fever on SACT" in case
    assert any(k.startswith("checkin:") for k in (i.idempotency_key for i in store.list_outbox()))
    # WP16: the demo nurse is notified in the app and by one email that links only to the queue.
    [mail] = [i.job for i in store.list_outbox() if i.job.kind == "email"
              and i.job.template_id == "new-alert"]  # fmt: skip
    assert mail.to == "sestra@demo.local" and "http://localhost:8000/sestra\n" in mail.text_body
    assert [n.kind for n in store.list_notifications("acc-demo-nurse")] == ["urgent_alert"]
    assert [n.kind for n in store.list_notifications("acc-demo-a")] == ["team_notified"]


def test_live_all_clear_report_raises_no_alert():
    app, mailbox = demo_app()
    store = app.state.store
    patient = signed_in(app, mailbox, "pacient.b@demo.local")
    base = start(patient)
    for link_id, value in ALL_CLEAR.items():
        answer(patient, base, link_id, value)
    summary = f"{base}/odeslat"
    result = patient.post(summary, data={"csrf_token": csrf(patient, summary)})
    page = patient.get(result.headers["location"]).text
    assert "Zavolejte hned" not in page
    [saved] = store.list_assessments("demo-patient-b")
    assert saved.status == "no_escalation" and saved.context is None
    assert store.list_alerts() == []
    assert not re.search(r"informovali", page)


def test_live_mild_nausea_report_shows_the_approved_advice_card():
    app, mailbox = demo_app()
    store = app.state.store
    patient = signed_in(app, mailbox, "pacient.a@demo.local")
    base = start(patient)
    for link_id, value in {**ALL_CLEAR, "nausea": "yes", "nausea_intake": "intake_usual"}.items():
        answer(patient, base, link_id, value)
    summary = f"{base}/odeslat"
    result = patient.post(summary, data={"csrf_token": csrf(patient, summary)})
    page = patient.get(result.headers["location"]).text
    [saved] = store.list_assessments("demo-patient-a")
    assert saved.status == "no_escalation"
    assert (saved.advice_card_id, saved.advice_card_version) == ("nausea-mild", "1")
    card = app.state.pipeline.card("nausea-mild", "1")
    assert html.escape(card.text_cs.split("\n")[0], quote=False) in page
    assert "Zavolejte hned" not in page
    assert store.list_alerts() == []


def test_live_text_no_model_read_is_a_routine_review_whatever_the_buttons_say():
    app, mailbox = demo_app()  # no model configured: the text is never extracted
    store = app.state.store
    patient = signed_in(app, mailbox, "pacient.b@demo.local")
    base = start(patient)
    write = f"{base}/napsat"
    patient.post(write, data={"csrf_token": csrf(patient, write), "text": "TEST bolí mě záda"})
    for link_id, value in ALL_CLEAR.items():
        answer(patient, base, link_id, value)
    summary = f"{base}/odeslat"
    patient.post(summary, data={"csrf_token": csrf(patient, summary)})
    [saved] = store.list_assessments("demo-patient-b")
    assert saved.status == "routine_review"  # never no_escalation on unread words
    assert [r.rule_id for r in saved.triggered_rules] == ["free-text-reported"]
    [alert] = store.list_alerts(status="open")
    assert alert.priority == "routine"
    nurse = signed_in(app, mailbox, "sestra@demo.local")
    case = nurse.get(f"/sestra/pripad/{alert.alert_id}").text
    assert "TEST bolí mě záda" in case and "model ho nečetl" in case


# --- grouping across check-ins (WP18, spec §9) -----------------------------------------------

AMBER = {"vomiting": "yes", "vomiting_episodes": "episodes_3_5"}


def report(checkin_id, **answers):
    return sent(findings(**answers), checkin_id=checkin_id)


def test_the_same_episode_in_a_later_report_joins_the_open_alert():
    store, process = setup()
    first = process(report("c1", **AMBER), PLAN, NOW)
    later = NOW + timedelta(hours=3)
    again = process(report("c2", **AMBER), PLAN, later)
    [alert] = store.list_alerts()
    assert alert.assessment_id == first.assessment_id
    assert again.status == "routine_review"  # grouping never lowers the verdict
    assert grouped_ids(store, alert) == [again.assessment_id]
    assert alert_of(store, again) == alert
    [entry] = audited(store, "alert_grouped")
    assert entry.target == f"alert:{alert.alert_id}" and entry.details["checkin"] == "c2"
    process(report("c2", **AMBER), PLAN, later)  # a retry adds nothing
    assert len(audited(store, "alert_grouped")) == 1 and len(store.list_alerts()) == 1


def test_a_worse_or_new_symptom_raises_a_new_alert_naming_the_one_it_renews():
    store, process = setup()
    process(report("c1", **AMBER), PLAN, NOW)
    [first] = store.list_alerts()
    worse = process(
        report("c2", **AMBER, diarrhoea="yes", diarrhoea_stools="stools_4_6"), PLAN, NOW
    )
    assert len(store.list_alerts()) == 2 and alert_of(store, worse).alert_id != first.alert_id
    created = audited(store, "alert_created")[-1]
    assert created.details["renews"] == first.alert_id and "Diarrhea" in created.details["worse"]


def test_an_urgent_report_joins_only_an_alert_nobody_has_taken_over():
    store, process = setup()
    process(report("c1", fever="yes", temperature=38.4), PLAN, NOW)
    process(report("c2", fever="yes", temperature=38.4), PLAN, NOW + timedelta(minutes=5))
    [alert] = store.list_alerts()  # still open: the team is alerted already
    store.save_alert(
        alert.evolve(status="acknowledged", acknowledged_by="n", acknowledged_at=NOW)
    )
    third = process(report("c3", fever="yes", temperature=38.4), PLAN, NOW + timedelta(hours=1))
    assert len(store.list_alerts()) == 2
    assert alert_of(store, third).priority == "urgent" and alert_of(store, third).status == "open"


def test_a_routine_report_joins_an_acknowledged_alert_but_never_a_resolved_one():
    store, process = setup()
    process(report("c1", **AMBER), PLAN, NOW)
    [alert] = store.list_alerts()
    acknowledged = alert.evolve(status="acknowledged", acknowledged_by="n", acknowledged_at=NOW)
    store.save_alert(acknowledged)
    process(report("c2", **AMBER), PLAN, NOW)
    assert len(store.list_alerts()) == 1
    store.save_alert(
        acknowledged.evolve(status="resolved", resolved_by="n", resolved_at=NOW,
                            resolution_note="TEST")
    )  # fmt: skip
    process(report("c3", **AMBER), PLAN, NOW)
    assert len(store.list_alerts()) == 2


def test_a_report_triggering_another_rule_is_not_grouped():
    store, process = setup()
    process(report("c1", **AMBER), PLAN, NOW)
    process(report("c2", chills="yes", chills_severity="shivering"), PLAN, NOW)
    assert len(store.list_alerts()) == 2


def _fever_then(reading, **extra):
    """Patient A (on SACT) answers fever "Ano", then the temperature question (D54)."""
    app, mailbox = demo_app()
    patient = signed_in(app, mailbox, "pacient.a@demo.local")
    base = start(patient)
    assert next_page(patient, answer(patient, base, "fever", "yes")) == f"{base}/otazka/temperature"
    return app, patient, base, answer(patient, base, "temperature", reading, **extra)


def test_reported_fever_with_a_normal_reading_goes_to_the_nurse_not_to_155():
    app, patient, base, location = _fever_then(TYPED_VALUE, number="37,0")
    assert location != f"{base}/vysledek"  # not urgent: the check-in goes on
    assert app.state.store.list_alerts(status="open") == []
    for link_id, value in ALL_CLEAR.items():
        if link_id not in ("fever", "temperature"):
            answer(patient, base, link_id, value)
    summary = f"{base}/odeslat"
    result = patient.post(summary, data={"csrf_token": csrf(patient, summary)})
    page = patient.get(result.headers["location"]).text
    assert "Zavolejte hned" not in page and "Hlášení si prohlédne zdravotní sestra" in page
    [saved] = app.state.store.list_assessments("demo-patient-a")
    assert saved.status == "routine_review"  # amber, never green: a nurse reviews it
    [alert] = app.state.store.list_alerts(status="open")
    assert alert.priority == "routine"


def test_reported_fever_without_a_reading_stays_urgent():
    app, _, base, location = _fever_then("dont_know")
    assert location == f"{base}/vysledek"  # "Nevím" never lowers urgency
    [alert] = app.state.store.list_alerts(status="open")
    assert alert.priority == "urgent"


def test_reported_fever_sent_without_answering_the_reading_stays_urgent():
    app, mailbox = demo_app()
    patient = signed_in(app, mailbox, "pacient.a@demo.local")
    base = start(patient)
    answer(patient, base, "fever", "yes")
    summary = f"{base}/odeslat"
    result = patient.post(summary, data={"csrf_token": csrf(patient, summary)})
    page = patient.get(result.headers["location"]).text
    assert "Zavolejte hned" in page
    assert [a.priority for a in app.state.store.list_alerts(status="open")] == ["urgent"]


def test_a_team_without_a_line_gets_the_team_notified_screen_without_155_by_default():
    app, patient, _, location = _fever_then(TYPED_VALUE, number="39,0")
    escalation = app.state.escalation
    app.state.escalation = escalation.model_copy(update={"teams": {
        team_id: team.model_copy(update={"patient_line": None})
        for team_id, team in escalation.teams.items()
    }})  # fmt: skip
    page = patient.get(location).text
    assert "Jednejte hned" in page and "svému týmu" not in page
    assert 'href="tel:155"' not in page and "tel:+420" not in page
