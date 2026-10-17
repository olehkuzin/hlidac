"""WP6: approved advice cards and the deterministic selection. Findings are invented."""

import pytest
from demo_helpers import MAPPING, NOW, PLAN, RULES, context, findings, quoted_text
from factories import T0, assessment, grade
from patient_helpers import make_app
from security.auth_helpers import PATIENT_A, csrf, login, new_client

from hlidac.advice import (
    ADVICE_DIR,
    CardFile,
    check_cards,
    load_card_files,
    load_cards,
    select_card,
)
from hlidac.assessment import assess
from hlidac.content import read_yaml
from hlidac.schemas import AdviceCard

CARDS = load_cards()
NAUSEA = ("nausea-mild", "1")
MILD_NAUSEA = {"nausea": "yes", "nausea_intake": "intake_usual"}


def run(base=None, /, **answers):
    """The real engine on the demo plan, as processing.py will call it."""
    return assess(
        assessment_id="a1",
        checkin_id="c1",
        patient_id="p1",
        findings=findings(base, **answers),
        plan=PLAN,
        mapping=MAPPING,
        rules=RULES,
        context=context(),
        now=NOW,
    )


# --- content ---------------------------------------------------------------------------------


def test_cards_load_and_fit_the_demo_plan():
    assert NAUSEA in CARDS
    check_cards(PLAN, CARDS)
    assert set(PLAN.advice_card_ids) <= {card_id for card_id, _ in CARDS}


def test_every_card_is_sourced_and_labelled_demo():
    files = load_card_files()
    assert files
    for file in files:
        assert file.card.approved_by is None
        assert "DEMO — čeká na schválení FN Motol" in file.approval
        assert all(s.url.startswith("https://") and s.section and s.supports for s in file.sources)
        assert file.card.source_url in {s.url for s in file.sources}


def test_patient_text_has_no_grades_or_jargon():
    for card in CARDS.values():
        for text in (card.text_cs, card.easy_read_cs, card.follow_up_cs):
            assert text is None or ("CTCAE" not in text and "stupe" not in text.lower())


def test_an_unlabelled_card_is_rejected():
    raw = read_yaml(ADVICE_DIR / "nausea-mild.yaml")
    with pytest.raises(ValueError, match="DEMO"):
        CardFile.model_validate(raw | {"approval": "schváleno"})
    with pytest.raises(ValueError, match="source_url"):
        CardFile.model_validate(raw | {"card": raw["card"] | {"source_url": "https://x.cz"}})
    with pytest.raises(ValueError):
        CardFile.model_validate(raw | {"sources": []})


def test_check_fails_loudly_on_an_unknown_item_or_card():
    card = CARDS[NAUSEA]
    bad = card.evolve(excluded_if_present=["no_such_item"])
    with pytest.raises(ValueError, match="no_such_item"):
        check_cards(PLAN, {NAUSEA: bad})
    ungraded = card.evolve(applies_when=[{"link_id": "unwell", "grades": [1]}])
    with pytest.raises(ValueError, match="CTCAE"):
        check_cards(PLAN, {NAUSEA: ungraded})
    with pytest.raises(ValueError, match="unknown advice card"):
        check_cards(PLAN, {})
    with pytest.raises(ValueError, match="one version"):
        check_cards(PLAN, {NAUSEA: card, ("nausea-mild", "2"): card.evolve(version="2")})


def test_two_versions_of_one_card_are_rejected_at_load(tmp_path):
    source = (ADVICE_DIR / "nausea-mild.yaml").read_text(encoding="utf-8")
    (tmp_path / "a.yaml").write_text(source, encoding="utf-8")
    (tmp_path / "b.yaml").write_text(source.replace('version: "1"', 'version: "2"'), "utf-8")
    with pytest.raises(ValueError, match="more than once"):
        load_cards(tmp_path)


# --- selection on the real engine --------------------------------------------------------------


def test_mild_nausea_with_everything_else_denied_gets_the_card():
    result = run(**MILD_NAUSEA)
    assert result.status == "no_escalation"
    assert select_card(result, PLAN, CARDS) == NAUSEA
    assert select_card(result, PLAN) == NAUSEA  # the approved files are the default
    # The selection fits the Assessment contract.
    result.evolve(advice_card_id=NAUSEA[0], advice_card_version=NAUSEA[1])


def test_selection_is_deterministic():
    result = run(**MILD_NAUSEA)
    assert {select_card(result, PLAN, CARDS) for _ in range(5)} == {NAUSEA}


def test_no_symptoms_gets_no_card():
    """No alert alone never selects advice (feature-description §8)."""
    result = run()
    assert result.status == "no_escalation"
    assert select_card(result, PLAN, CARDS) is None


@pytest.mark.parametrize("intake", ["intake_slightly_less", "intake_decreased", "intake_minimal"])
def test_more_than_mild_nausea_gets_no_card(intake):
    assert select_card(run(nausea="yes", nausea_intake=intake), PLAN, CARDS) is None


def test_urgent_overrides_advice():
    result = run(**MILD_NAUSEA, fever="yes", temperature=38.6)
    assert result.status == "urgent"
    assert select_card(result, PLAN, CARDS) is None


@pytest.mark.parametrize(
    "answers",
    [
        {"vomiting": "yes", "vomiting_episodes": "episodes_1_2"},  # a related symptom present
        {"diarrhoea": "yes", "diarrhoea_stools": "stools_up_to_3"},
        {"diarrhoea_blood": "yes"},  # a red flag present
    ],
)
def test_exclusions_on_the_real_engine(answers):
    assert select_card(run(**MILD_NAUSEA, **answers), PLAN, CARDS) is None


@pytest.mark.parametrize("link_id", ["nausea_intake", "vomiting", "fever"])
def test_dont_know_never_selects_a_card(link_id):
    result = run(**(MILD_NAUSEA | {link_id: "dont_know"}))
    assert select_card(result, PLAN, CARDS) is None


def test_free_text_report_gets_no_card():
    result = run(**MILD_NAUSEA)
    with_text = assess(
        assessment_id="a1", checkin_id="c1", patient_id="p1",
        findings=[*result.findings, quoted_text()], plan=PLAN, mapping=MAPPING, rules=RULES,
        context=context(), now=NOW,
    )  # fmt: skip
    assert with_text.status == "routine_review"
    assert select_card(with_text, PLAN, CARDS) is None


# --- selection on hand-built assessments (the guards, even if the engine changed) --------------


def _no_escalation(**changes):
    base = run(**MILD_NAUSEA)
    return base.evolve(**changes)


@pytest.mark.parametrize("state", ["unknown", "not_mentioned"])
@pytest.mark.parametrize("link_id", ["vomiting", "diarrhoea", "fever", "unwell"])
def test_unknown_or_not_mentioned_exclusion_or_red_flag_excludes(link_id, state):
    base = run(**MILD_NAUSEA)
    kept = [f for f in base.findings if f.link_id != link_id]
    if state == "unknown":
        replaced = findings(MILD_NAUSEA | {link_id: "dont_know"})
        kept.append(next(f for f in replaced if f.link_id == link_id))
    assert select_card(base.evolve(findings=kept), PLAN, CARDS) is None


def test_an_at_least_grade_never_matches():
    base = run(**MILD_NAUSEA)
    at_least = grade(link_id="nausea", grade=None, at_least=1, needs_clinical_assessment=True)
    changed = base.model_construct(**(dict(base) | {"grades": [at_least]}))
    assert select_card(changed, PLAN, CARDS) is None


def test_a_card_needs_the_condition_finding_present():
    base = run(**MILD_NAUSEA)
    kept = [f for f in base.findings if f.link_id != "nausea"]
    assert select_card(base.evolve(findings=kept), PLAN, CARDS) is None


@pytest.mark.parametrize("status", ["urgent", "routine_review", "insufficient_information"])
def test_only_no_escalation_gets_a_card(status):
    base = run(**MILD_NAUSEA)
    changed = base.model_construct(**(dict(base) | {"status": status}))
    assert select_card(changed, PLAN, CARDS) is None


def test_another_plans_assessment_is_an_error():
    with pytest.raises(ValueError, match="another plan"):
        select_card(_no_escalation(questionnaire_version="2"), PLAN, CARDS)


def test_a_card_the_plan_does_not_permit_is_never_selected():
    plan = PLAN.evolve(advice_card_ids=[])
    assert select_card(run(**MILD_NAUSEA), plan, CARDS) is None


# --- the result screen ----------------------------------------------------------------------


def test_result_screen_shows_the_approved_card_text():
    card: AdviceCard = CARDS[NAUSEA]

    def with_card(store):
        def process(checkin, plan, now):
            if checkin.status != "completed":
                return None
            result = assessment(
                checkin_id=checkin.checkin_id,
                patient_id=checkin.patient_id,
                findings=list(checkin.findings),
                grades=[],
                questionnaire_url=plan.questionnaire_url,
                questionnaire_version=plan.questionnaire_version,
                advice_card_id=card.card_id,
                advice_card_version=card.version,
                assessed_at=T0,
            )
            store.add_assessment(result)
            return result

        return process

    app, _, mailbox = make_app(process=with_card, advice_cards=CARDS)
    client = new_client(app)
    login(client, mailbox, PATIENT_A.email)
    response = client.post("/pacient/hlaseni", data={"csrf_token": csrf(client, "/pacient")})
    base = response.headers["location"]
    for link_id, answer in (("fever", "no"), ("nausea", "no")):
        path = f"{base}/otazka/{link_id}"
        client.post(path, data={"csrf_token": csrf(client, path), "answer": answer})
        confirm = f"{base}/potvrdit/{link_id}"
        if client.get(confirm, follow_redirects=False).status_code == 200:
            client.post(confirm, data={"csrf_token": csrf(client, confirm)})
    summary = f"{base}/odeslat"
    client.post(summary, data={"csrf_token": csrf(client, summary)})
    page = client.get(f"{base}/vysledek").text
    assert card.text_cs in page
    assert card.follow_up_cs in page
    assert card.source_url in page
    assert "CTCAE" not in page
