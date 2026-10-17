"""WP25: the case's audit trail (what each model saw, the replay check) and the rules page.
Hand-made fixtures only; every text below is invented."""

from demo_helpers import MAPPING, NOW, PLAN, RULES, context, findings
from factories import T0
from security.auth_helpers import PATIENT_B
from test_clinician_app import OTHER_NURSE, logged_in, urgent_case

from hlidac import assessment as engine
from hlidac import audit
from hlidac.schemas import InboundMessage, ModelCall
from hlidac.ui.clinician import REPLAY_SAME, replay_verdict

SENT = "Mám horečku, píše [JMÉNO]."  # invented; the masked text as the gateway would send it


def with_model_call(store, alert):
    """Give the case's check-in a written message and the gateway's audit row for it."""
    [checkin] = store.list_checkins(alert.patient_id)
    message = InboundMessage(
        message_id="m-test",
        checkin_id=checkin.checkin_id,
        patient_id=checkin.patient_id,
        session_account_id=PATIENT_B.account_id,
        reported_by="patient",
        received_at=T0,
        kind="text",
        text="Mám horečku.",
    )
    store.save_checkin(checkin.evolve(messages=[*checkin.messages, message]))
    call = ModelCall(
        purpose="extraction",
        data_class="PATIENT",
        backend="azure",
        endpoint_host="example.openai.azure.com",
        model="demo-model",
        request_text=SENT,
        response_text='{"fever": {"state": "present"}}',
        duration_ms=1200,
        ok=True,
    )
    audit.record(store, T0, "system", "model_call", "message:m-test", model_call=call)
    audit.record(store, T0, "system", "checkin_started", "checkin:someone-else")


def test_trail_shows_the_case_steps_and_exactly_what_the_model_saw():
    _, store, _, nurse, alert = urgent_case()
    with_model_call(store, alert)
    case = f"/sestra/pripad/{alert.alert_id}"
    assert f"{case}/zaznam" in nurse.get(case).text
    page = nurse.get(f"{case}/zaznam").text
    assert "Pacient začal hlášení" in page and "Případ otevřen" in page
    assert "[JMÉNO]" in page  # the pseudonymised text, as sent
    assert "example.openai.azure.com" in page and "demo-model" in page
    assert 'href="#volani-1"' in page and 'id="volani-1"' in page
    assert "checkin:someone-else" not in page  # only this case's entries
    assert any(e.action == "view_case_audit" for e in store.list_audit(f"alert:{alert.alert_id}"))


def test_trail_says_when_the_assessment_cannot_be_replayed():
    _, _, _, nurse, alert = urgent_case()  # the stand-in engine records other versions
    page = nurse.get(f"/sestra/pripad/{alert.alert_id}/zaznam").text
    assert "Nelze přepočítat" in page
    assert REPLAY_SAME not in page


def test_trail_is_scoped_to_the_nurses_team():
    app, _, mailbox, _, alert = urgent_case()
    other = logged_in(app, mailbox, OTHER_NURSE)
    assert other.get(f"/sestra/pripad/{alert.alert_id}/zaznam").status_code == 403
    assert other.get("/sestra/pripad/missing/zaznam").status_code == 404


def test_patients_cannot_open_the_trail_or_the_rules():
    app, _, mailbox, _, alert = urgent_case()
    patient = logged_in(app, mailbox, PATIENT_B)
    for path in (f"/sestra/pripad/{alert.alert_id}/zaznam", "/sestra/pravidla"):
        assert patient.get(path).status_code == 403


def real_assessment():
    return engine.assess(
        assessment_id="a-replay",
        checkin_id="c1",
        patient_id="p1",
        findings=findings(fever="yes", temperature=38.4),
        plan=PLAN,
        mapping=MAPPING,
        rules=RULES,
        context=context(),
        now=NOW,
    )


def test_replay_of_a_stored_assessment_matches():
    assert replay_verdict(real_assessment(), PLAN, RULES, MAPPING) == (True, REPLAY_SAME)


def test_replay_reports_a_tampered_assessment_as_different():
    stored = real_assessment().evolve(status="routine_review", triggered_rules=[])
    same, text = replay_verdict(stored, PLAN, RULES, MAPPING)
    assert not same and text.startswith("Liší se")


def test_replay_never_claims_a_match_without_the_plan_or_versions():
    stored = real_assessment()
    assert replay_verdict(stored, None, RULES, MAPPING)[0] is False
    assert replay_verdict(stored.evolve(engine_version="0.0"), PLAN, RULES, MAPPING)[0] is False


def test_rules_page_lists_versions_and_sources():
    _, store, _, nurse, _ = urgent_case()
    page = nurse.get("/sestra/pravidla").text
    assert f"v{RULES.version}" in page and f"v{MAPPING.version}" in page
    assert RULES.rules[0].rule_id in page and RULES.rules[0].source_ref.split("'")[0] in page
    assert "DEMO" in page
    assert any(e.action == "view_rules" for e in store.list_audit())
