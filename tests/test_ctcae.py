"""WP3: provisional CTCAE grading from the mapping. One test per demo symptom and grade."""

import pytest
import yaml
from demo_helpers import MAPPING, PLAN, finding, findings
from pydantic import ValidationError

from hlidac import ctcae
from hlidac.content import DATA_DIR
from hlidac.schemas import Finding

ON = {"fever": None, "chills": None, "nausea": None, "vomiting": None, "diarrhoea": None}


def graded(base=None, **answers) -> dict[str, "ctcae.CtcaeGrade"]:
    found = findings(base if base is not None else {}, **answers)
    return {g.ctcae_term: g for g in ctcae.grade(found, MAPPING, "c1")}


def only(term, **answers):
    return graded(**answers)[term]


# --- one test per symptom and grade ----------------------------------------------------------


@pytest.mark.parametrize(
    ("celsius", "expected"),
    [(38.0, 1), (38.4, 1), (39.0, 1), (39.05, 2), (40.0, 2)],
)
def test_fever_exact_grades_follow_the_temperature(celsius, expected):
    g = only("Fever", fever="yes", temperature=celsius)
    assert (g.grade, g.at_least, g.needs_clinical_assessment) == (expected, None, False)


@pytest.mark.parametrize("celsius", [40.1, 41.5])
def test_fever_over_40_is_at_least_3_because_duration_is_not_asked(celsius):
    g = only("Fever", fever="yes", temperature=celsius)
    assert (g.grade, g.at_least, g.needs_clinical_assessment) == (None, 3, True)


def test_a_measured_temperature_below_38_is_no_ctcae_fever():
    assert "Fever" not in graded(fever="yes", temperature=37.2)


def test_fever_yes_without_a_number_is_at_least_1():
    g = only("Fever", fever="yes")
    assert (g.grade, g.at_least, g.needs_clinical_assessment) == (None, 1, True)


@pytest.mark.parametrize(("severity", "expected"), [("shivering", 1), ("whole_body", 2)])
def test_chills_are_at_least_1_or_2_because_grade_3_is_not_asked(severity, expected):
    g = only("Chills", chills="yes", chills_severity=severity)
    assert (g.grade, g.at_least, g.needs_clinical_assessment) == (None, expected, True)


@pytest.mark.parametrize(
    ("intake", "grade", "at_least"),
    [
        ("intake_usual", 1, None),
        ("intake_slightly_less", None, 2),
        ("intake_decreased", None, 2),
        ("intake_minimal", 3, None),
    ],
)
def test_nausea_follows_oral_intake(intake, grade, at_least):
    g = only("Nausea", nausea="yes", nausea_intake=intake)
    assert (g.grade, g.at_least, g.needs_clinical_assessment) == (grade, at_least, bool(at_least))


@pytest.mark.parametrize("episodes", ["episodes_1_2", "episodes_3_5", "episodes_6_10"])
def test_vomiting_is_always_at_least_1_in_v5_the_count_does_not_set_a_grade(episodes):
    g = only("Vomiting", vomiting="yes", vomiting_episodes=episodes)
    assert (g.grade, g.at_least, g.needs_clinical_assessment) == (None, 1, True)


@pytest.mark.parametrize(
    ("stools", "at_least"),
    [("stools_up_to_3", 1), ("stools_4_6", 2), ("stools_7_9", 3), ("stools_10_plus", 3)],
)
def test_diarrhoea_follows_stools_over_baseline(stools, at_least):
    g = only("Diarrhea", diarrhoea="yes", diarrhoea_stools=stools)
    assert (g.grade, g.at_least, g.needs_clinical_assessment) == (None, at_least, True)


# --- a missing attribute never lowers the grade ----------------------------------------------

FOLLOW_UPS = [
    ("Fever", "fever", "temperature"),
    ("Chills", "chills", "chills_severity"),
    ("Nausea", "nausea", "nausea_intake"),
    ("Diarrhea", "diarrhoea", "diarrhoea_stools"),
]


@pytest.mark.parametrize(("term", "parent", "follow_up"), FOLLOW_UPS)
@pytest.mark.parametrize("how", ["missing", "dont_know"])
def test_symptom_yes_with_an_unsettled_follow_up_still_gets_an_at_least_grade(
    term, parent, follow_up, how
):
    found = findings({}, **{parent: "yes"})
    if how == "dont_know":
        found.append(finding(follow_up, "dont_know"))
    result = {g.ctcae_term: g for g in ctcae.grade(found, MAPPING, "c1")}[term]
    assert result.at_least is not None and result.needs_clinical_assessment
    assert result.grade is None


def test_an_unconfirmed_number_cannot_settle_an_exact_grade_but_still_raises():
    exact = only("Fever", fever="yes", temperature=38.5)
    assert exact.grade == 1
    found = findings({}, fever="yes") + [finding("temperature", 38.5, confirmed=False)]
    unconfirmed = {g.ctcae_term: g for g in ctcae.grade(found, MAPPING, "c1")}["Fever"]
    assert (unconfirmed.grade, unconfirmed.at_least) == (None, 1)
    found = findings({}, fever="yes") + [finding("temperature", 41.0, confirmed=False)]
    raised = {g.ctcae_term: g for g in ctcae.grade(found, MAPPING, "c1")}["Fever"]
    assert raised.at_least == 3


def test_the_highest_matching_grade_wins():
    g = only("Fever", fever="yes", temperature=40.5)
    assert g.at_least == 3  # not the "fever without a number" proposal


def test_not_asked_and_dont_know_give_no_grade_never_a_lower_one():
    assert graded(fever="dont_know", nausea="no") == {}
    assert graded(fever="no", vomiting="no") == {}


def test_a_quoted_statement_without_an_option_code_counts_as_yes():
    said = Finding.model_validate(
        {
            "link_id": "fever",
            "state": "present",
            "source": {"kind": "quote", "message_id": "m9", "quote": "mám horečku"},
            "extractor_version": "extract-v1",
        }
    )
    g = {g.ctcae_term: g for g in ctcae.grade([said], MAPPING, "c1")}["Fever"]
    assert g.at_least == 1


# --- what a grade carries --------------------------------------------------------------------


def test_a_grade_is_provisional_and_traceable():
    g = only("Fever", fever="yes", temperature=38.4)
    official = yaml.safe_load((DATA_DIR / "ctcae" / "ctcae-v5.0.yaml").read_text())
    wording = next(t for t in official["terms"] if t["term"] == "Fever")["grades"]["1"]
    assert g.status == "provisional" and g.confirmed_by is None
    assert g.link_id == "fever" and g.ctcae_version == "5.0"
    assert (g.mapping_id, g.mapping_version) == ("ctcae-fever", "1")
    assert wording in g.basis[0]  # the official text, verbatim
    assert any("temperature: 38.4 Cel" in line for line in g.basis)
    assert any("DEMO" in line for line in g.basis)


def test_an_at_least_grade_says_why():
    g = only("Vomiting", vomiting="yes", vomiting_episodes="episodes_1_2")
    assert any(line.startswith("At least, not exact:") for line in g.basis)


def test_grading_is_deterministic_and_ids_depend_on_the_check_in():
    found = findings({}, nausea="yes", nausea_intake="intake_usual")
    assert ctcae.grade(found, MAPPING, "c1") == ctcae.grade(found, MAPPING, "c1")
    other = ctcae.grade(found, MAPPING, "c2")
    assert other[0].grade_id != ctcae.grade(found, MAPPING, "c1")[0].grade_id


def test_grades_follow_the_mapping_order_not_the_findings_order():
    a = findings({}, nausea="yes", nausea_intake="intake_usual", fever="yes")
    result = ctcae.grade(list(reversed(a)), MAPPING, "c1")
    assert [g.ctcae_term for g in result] == ["Fever", "Nausea"]


# --- the mapping file ------------------------------------------------------------------------


def test_the_mapping_loads_and_fits_the_demo_questionnaire():
    ctcae.check_against_plan(MAPPING, PLAN)
    assert MAPPING.version == "1" and "DEMO" in MAPPING.approval


def test_a_typo_in_an_answer_code_is_caught_against_the_plan():
    raw = MAPPING.model_dump(by_alias=True)
    raw["mappings"][2]["proposals"][0]["when"]["answer"]["codes"] = ["intake_usal"]
    with pytest.raises(ValueError, match="answer code"):
        ctcae.check_against_plan(ctcae.CtcaeMapping.model_validate(raw), PLAN)


def test_a_criterion_with_no_official_grade_is_rejected():
    raw = MAPPING.model_dump(by_alias=True)
    proposal = raw["mappings"][2]["proposals"][2]  # Nausea grade 3
    proposal["grade"] = 4
    proposal["criterion"]["grade"] = 4
    with pytest.raises(ValidationError, match="no grade 4"):
        ctcae.CtcaeMapping.model_validate(raw)


def test_a_wrong_meddra_code_is_rejected():
    raw = MAPPING.model_dump(by_alias=True)
    raw["mappings"][0]["meddra_code"] = "99999999"
    with pytest.raises(ValidationError, match="official CTCAE term"):
        ctcae.CtcaeMapping.model_validate(raw)


def test_an_unapproved_mapping_must_be_labelled_demo():
    raw = MAPPING.model_dump(by_alias=True)
    raw["approval"] = "approved"
    with pytest.raises(ValidationError, match="DEMO"):
        ctcae.CtcaeMapping.model_validate(raw)


def test_a_condition_is_exactly_one_kind():
    with pytest.raises(ValidationError):
        ctcae.Condition.model_validate(
            {"no_value": "a", "answer": {"link_id": "a", "codes": ["x"]}}
        )
    with pytest.raises(ValidationError):
        ctcae.Condition.model_validate({})
    with pytest.raises(ValidationError):
        ctcae.Condition.model_validate({"value": {"link_id": "t", "unit": "Cel"}})


def test_unknown_keys_in_the_mapping_are_rejected():
    raw = MAPPING.model_dump(by_alias=True)
    raw["mappings"][0]["proposals"][0]["extra"] = 1
    with pytest.raises(ValidationError):
        ctcae.CtcaeMapping.model_validate(raw)
