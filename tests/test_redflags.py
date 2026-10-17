"""WP22: deterministic red-flag phrase scan on free text (`src/hlidac/redflags.py`) and its
wiring in `processing.py`, the patient app and the clinician replay check.

Hand-made fixtures only; no patient data."""

import pytest
from demo_helpers import ALL_CLEAR, MAPPING, NOW, PLAN, RULES, context, findings
from factories import T0, checkin
from security.auth_helpers import Mailbox, csrf, login, new_client

from hlidac import assessment as engine
from hlidac import redflags
from hlidac.app import create_app
from hlidac.config import Settings
from hlidac.processing import make_process
from hlidac.schemas import Finding, InboundMessage, PatientPlan, QuoteSource
from hlidac.store import InMemoryStore, StaticContextSource
from hlidac.ui.clinician import REPLAY_SAME, replay_verdict


def make_assessment(**found):
    return engine.assess(
        assessment_id="a-rf",
        checkin_id="c1",
        patient_id="p1",
        findings=findings(**found),
        plan=PLAN,
        mapping=MAPPING,
        rules=RULES,
        context=context(),
        now=NOW,
    )


# --- content & validation --------------------------------------------------------------------


def test_every_red_flag_phrase_is_cited_and_labelled_demo():
    definition = redflags.load_files()
    assert "DEMO" in definition.approval
    assert len(definition.phrases) >= 10
    for entry in definition.phrases:
        assert entry.phrase.strip()
        assert entry.source_ref.strip()


def test_load_files_rejects_unapproved_file_without_demo_label(tmp_path):
    bad = tmp_path / "redflags-demo-v1.yaml"
    bad.write_text(
        "content_id: rf\nversion: '1'\napproval: pending\napproved_by: null\n"
        "phrases:\n  - phrase: test\n    level: urgent\n    source_ref: ref\n",
        encoding="utf-8",
    )
    redflags.load_files.cache_clear()
    try:
        with pytest.raises(ValueError, match="DEMO"):
            redflags.load_files(tmp_path)
    finally:
        redflags.load_files.cache_clear()


# --- check() ---------------------------------------------------------------------------------


@pytest.mark.parametrize("empty", [None, "", "   ", "Dnes se cítím celkem dobře, jen mírná únava."])
def test_check_returns_none_when_no_red_flag_phrase_matches(empty):
    assert redflags.check(empty) is None


@pytest.mark.parametrize(
    ("text", "expected", "level"),
    [
        ("Dnes MÁM HOREČKU a je mi divně.", "mám horečku", "review"),
        ("Od rána vidím krev ve stolici.", "krev ve stolici", "urgent"),  # urgent beats review
        ("Je mi VELMI ZLE.", "velmi zle", "review"),
        ("Nemohu pít ani vodu.", "nemohu pít", "urgent"),
        ("nemuzu dychat", "nemůžu dýchat", "urgent"),  # no diacritics typed
        ("Od včera dušnost.", "dušnost", "review"),
        ("Mám bolest na hrudi.", "bolest na hrudi", "urgent"),
    ],
)
def test_check_matches_ignoring_case_and_diacritics(text, expected, level):
    hit = redflags.check(text)
    assert hit is not None and (hit.phrase, hit.level) == (expected, level)


@pytest.mark.parametrize("text", ["Mám kašel, nic víc.", "Bolí mě hlava.", "Spím špatně."])
def test_everyday_words_are_not_red_flags(text):
    assert redflags.check(text) is None


def test_a_phrase_starts_at_a_word_boundary():
    assert redflags.check("bezdušnost") is None  # not "dušnost" inside another word


def test_both_levels_are_there_and_the_urgent_list_is_critical_only():
    levels = {p.phrase: p.level for p in redflags.load_files().phrases}
    for vague in ("mám horečku", "velmi zle", "mám to horší", "vidím krev", "třesu se", "dušnost"):
        assert levels[vague] == "review"  # D56: the questions decide, never green
    for critical in ("krev ve stolici", "nemůžu dýchat", "bolest na hrudi", "nemohu pít"):
        assert levels[critical] == "urgent"


# --- raise_on_hit() --------------------------------------------------------------------------


def test_raise_on_hit_raises_every_non_urgent_status_to_urgent_and_clears_advice_card():
    calm = make_assessment().evolve(advice_card_id="nausea-mild", advice_card_version="1")
    assert calm.status == "no_escalation"
    raised_calm = redflags.raise_on_hit(calm, "Od rána nemohu dýchat.")
    assert raised_calm.status == "urgent"
    assert raised_calm.advice_card_id is None and raised_calm.advice_card_version is None
    assert any(r.rule_id == "red-flag-phrase" for r in raised_calm.triggered_rules)

    missing = make_assessment(fever=None)
    assert missing.status == "insufficient_information"
    assert redflags.raise_on_hit(missing, "Mám bolest na hrudi.").status == "urgent"


def test_a_review_phrase_raises_to_a_nurse_review_and_never_lowers():
    calm = make_assessment().evolve(advice_card_id="nausea-mild", advice_card_version="1")
    raised = redflags.raise_on_hit(calm, "Dnes mám horečku.")
    assert raised.status == "routine_review" and raised.advice_card_id is None
    assert [r.rule_id for r in raised.triggered_rules][-1] == redflags.REVIEW_RULE
    missing = make_assessment(fever=None)
    assert redflags.raise_on_hit(missing, "Je mi velmi zle.").status == "routine_review"
    urgent = make_assessment(fever="yes", temperature=38.4)
    assert redflags.raise_on_hit(urgent, "Je mi velmi zle.").status == "urgent"
    once = redflags.raise_on_hit(raised, "Dnes mám horečku.")
    assert once == raised  # a retry adds nothing

    routine = make_assessment(vomiting="yes", vomiting_episodes="episodes_3_5")
    assert routine.status == "routine_review"
    raised_routine = redflags.raise_on_hit(routine, "Od rána nemohu pít.")
    assert raised_routine.status == "urgent"
    assert [r.rule_id for r in raised_routine.triggered_rules] == [
        *(r.rule_id for r in routine.triggered_rules),
        "red-flag-phrase",
    ]
    assert raised_routine.grades == routine.grades  # grades are never lowered or altered


def test_raise_on_hit_never_lowers_an_urgent_assessment_or_duplicates_its_rule():
    urgent = make_assessment(fever="yes", temperature=38.4)
    assert urgent.status == "urgent"
    assert redflags.raise_on_hit(urgent, None) == urgent
    assert redflags.raise_on_hit(urgent, "Nic dalšího.") == urgent

    once = redflags.raise_on_hit(urgent, "Navíc mám krev ve stolici.")
    twice = redflags.raise_on_hit(once, "Navíc mám krev ve stolici.")
    assert once.status == "urgent"
    assert twice == once
    assert [r.rule_id for r in once.triggered_rules].count("red-flag-phrase") == 1


# --- pipeline & live app ---------------------------------------------------------------------


def test_in_progress_checkin_with_red_flag_quote_is_urgent_and_alerted_at_once():
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
    process = make_process(
        store, StaticContextSource({"p1": context()}), plans=[PLAN], base_url="http://localhost:8000"
    )
    quoted = Finding(
        link_id="other",
        state="present",
        source=QuoteSource(kind="quote", message_id="m1", quote="Mám krev ve stolici"),
        extractor_version="extract-v1",
    )
    partial = checkin(findings=[quoted], status="in_progress")
    result = process(partial, PLAN, NOW)
    assert result is not None and result.status == "urgent"
    [alert] = store.list_alerts()
    assert alert.priority == "urgent" and alert.assessment_id == result.assessment_id
    assert replay_verdict(result, PLAN, RULES, MAPPING, partial) == (True, REPLAY_SAME)


def test_live_free_text_with_red_flag_phrase_without_model_triggers_urgent_screen_at_once():
    mailbox = Mailbox()
    config = Settings(_env_file=None, secret_key="test-secret-" + "x" * 32)
    app = create_app(config, send_email=mailbox)  # no model configured: extraction is off
    store = app.state.store

    patient = new_client(app)
    login(patient, mailbox, "pacient.b@demo.local")
    base = patient.post("/pacient/hlaseni", data={"csrf_token": csrf(patient, "/pacient")}).headers[
        "location"
    ]
    write = f"{base}/napsat"
    response = patient.post(
        write,
        data={"csrf_token": csrf(patient, write), "text": "Je mi velmi zle a nemůžu dýchat."},
    )
    # Urgent never waits for button clarification questions: redirects straight to /vysledek.
    assert response.headers["location"] == f"{base}/vysledek"
    page = patient.get(response.headers["location"]).text
    assert "Zavolejte hned" in page and 'href="tel:155"' not in page
    assert "Váš tým jsme o hlášení informovali." in page

    [saved] = store.list_assessments("demo-patient-b")
    assert saved.status == "urgent"
    assert any(r.rule_id == "red-flag-phrase" for r in saved.triggered_rules)
    [alert] = store.list_alerts(status="open")
    assert alert.priority == "urgent" and alert.assessment_id == saved.assessment_id

    # The nurse sees the reason in the case view and the audit replay matches.
    nurse = new_client(app)
    login(nurse, mailbox, "sestra@demo.local")
    case_html = nurse.get(f"/sestra/pripad/{alert.alert_id}").text
    assert "Pacient použil varovnou frázi v popisu: nemůžu dýchat" in case_html
    audit_html = nurse.get(f"/sestra/pripad/{alert.alert_id}/zaznam").text
    assert REPLAY_SAME in audit_html


def test_red_flag_phrase_still_wins_even_if_all_buttons_say_no():
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
    process = make_process(
        store, StaticContextSource({"p1": context()}), plans=[PLAN], base_url="http://localhost:8000"
    )
    msg = InboundMessage(
        message_id="m-rf",
        checkin_id="c1",
        patient_id="p1",
        session_account_id="acc-1",
        reported_by="patient",
        received_at=T0,
        kind="text",
        text="Od včerejška vykašlávám krev.",
    )
    report = checkin(findings=findings(**ALL_CLEAR), messages=[msg], status="completed")
    result = process(report, PLAN, NOW)
    assert result is not None and result.status == "urgent"
    assert [r.rule_id for r in result.triggered_rules] == ["red-flag-phrase"]
    # D56: a worrying but not critical phrase on an all-clear report is a nurse review
    vague = msg.model_copy(update={"message_id": "m-v", "checkin_id": "c-v", "text": "Mám vysoká teplota."})
    report = checkin(checkin_id="c-v", findings=findings(**ALL_CLEAR), messages=[vague],
                     status="completed")  # fmt: skip
    result = process(report, PLAN, NOW)
    assert result is not None and result.status == "routine_review"


def test_vague_words_read_as_unwell_wait_for_the_button_before_urgent():
    # D49: "cítím se špatně" read by the model as "Je vám celkově zle? Ano" is red under UKONS
    # row 5 only once the patient confirms it (or sends the report); no phrase hit, so no 155 yet.
    from hlidac import wizard

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
    process = make_process(
        store, StaticContextSource({"p1": context()}), plans=[PLAN], base_url="http://localhost:8000"
    )
    unwell = Finding(
        link_id="unwell",
        state="present",
        code="yes",
        source=QuoteSource(kind="quote", message_id="m1", quote="cítím se špatně"),
        extractor_version="extract-v1",
    )
    partial = checkin(findings=[unwell], status="in_progress")
    assert process(wizard.for_assessment(PLAN, partial), PLAN, NOW) is None
    assert not store.list_alerts()

    confirmed = partial.evolve(findings=[unwell.evolve(confirmed=True)])
    result = process(wizard.for_assessment(PLAN, confirmed), PLAN, NOW)
    assert result is not None and result.status == "urgent"

    sent = checkin(checkin_id="c2", findings=[unwell], status="completed")
    result = process(wizard.for_assessment(PLAN, sent), PLAN, NOW)
    assert result is not None and result.status == "urgent"


def test_a_critical_phrase_still_raises_urgent_before_any_confirmation():
    from hlidac import wizard

    fever = Finding(
        link_id="fever",
        state="present",
        code="yes",
        source=QuoteSource(kind="quote", message_id="m1", quote="nemůžu dýchat"),
        extractor_version="extract-v1",
    )
    message = InboundMessage(
        message_id="m1", checkin_id="c1", patient_id="p1", session_account_id="acc-1",
        reported_by="patient", received_at=T0, kind="text", text="mám horečku a nemůžu dýchat",
    )  # fmt: skip
    partial = checkin(findings=[fever], messages=[message], status="in_progress")
    held = wizard.for_assessment(PLAN, partial)
    assert held.findings == []  # the model's reading waits for its button…
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
    process = make_process(
        store, StaticContextSource({"p1": context()}), plans=[PLAN], base_url="http://localhost:8000"
    )
    result = process(held, PLAN, NOW)
    assert result is not None and result.status == "urgent"  # …the deterministic scan does not
