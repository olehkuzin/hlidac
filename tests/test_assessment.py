"""WP4: the rules engine. One test per rule, plus the safety properties of principle 2."""

import re
from datetime import timedelta
from pathlib import Path

import pytest
from demo_helpers import (
    ALL_CLEAR,
    MAPPING,
    NOW,
    PLAN,
    RULES,
    context,
    finding,
    findings,
    quoted_text,
)
from pydantic import ValidationError

from hlidac import assessment as engine
from hlidac.schemas import Assessment


def run(found, ctx="default", **kw) -> Assessment:
    return engine.assess(
        assessment_id=kw.pop("assessment_id", "a1"),
        checkin_id="c1",
        patient_id="p1",
        findings=found,
        plan=PLAN,
        mapping=MAPPING,
        rules=RULES,
        context=context() if ctx == "default" else ctx,
        now=NOW,
    )


def fired(a: Assessment) -> set[str]:
    return {t.rule_id for t in a.triggered_rules}


# --- no_escalation ---------------------------------------------------------------------------


def test_everything_denied_is_no_escalation():
    a = run(findings())
    assert a.status == "no_escalation" and a.triggered_rules == [] and a.missing_link_ids == []


def test_a_mild_nausea_with_usual_intake_is_no_escalation_with_an_exact_grade():
    a = run(findings(nausea="yes", nausea_intake="intake_usual"))
    assert a.status == "no_escalation"
    assert [(g.ctcae_term, g.grade) for g in a.grades] == [("Nausea", 1)]


# --- ukons-red (urgent) ----------------------------------------------------------------------


def test_fever_on_day_10_after_a_dose_is_urgent_at_once():
    a = run(findings(fever="yes", temperature=38.4))
    assert a.status == "urgent" and fired(a) >= {"ukons-red"}
    rule = next(t for t in a.triggered_rules if t.rule_id == "ukons-red")
    assert rule.inputs["ukons_symptom"] == "5. Fever on SACT"
    assert rule.inputs["days_since_dose"] == 10
    assert "5. Fever on SACT" in rule.reason and "červené" in rule.reason
    assert a.advice_card_id is None


@pytest.mark.parametrize(
    "answers",
    [
        {"fever": "yes"},  # reported fever is red on SACT, whatever the reading
        {"fever": "yes", "temperature": 37.5},
        {"fever": "yes", "temperature": 35.9},
        {"unwell": "yes"},
        {"vomiting": "yes", "vomiting_episodes": "episodes_6_10"},
        {"vomiting": "yes", "vomiting_episodes": "episodes_over_10"},
        {"diarrhoea": "yes", "diarrhoea_stools": "stools_7_9"},
        {"diarrhoea": "yes", "diarrhoea_stools": "stools_10_plus"},
        {"diarrhoea_blood": "yes"},
        {"nausea": "yes", "nausea_intake": "intake_minimal"},
    ],
)
def test_every_red_ukons_band_is_urgent(answers):
    assert run(findings(**answers)).status == "urgent"


def test_the_ukons_boundary_37_4_is_not_red_but_37_45_is():
    assert run(findings(temperature=37.4, fever="no")).status == "no_escalation"
    assert run(findings(temperature=37.45, fever="no")).status == "urgent"


def test_urgent_fires_despite_missing_unrelated_items():
    a = run([finding("fever", "yes"), finding("temperature", 38.6)])
    assert a.status == "urgent"
    assert {"chills", "unwell", "nausea", "vomiting", "diarrhoea"} <= set(a.missing_link_ids)


def test_a_typed_number_raises_urgency_before_it_is_confirmed():
    found = [finding("fever", "yes"), finding("temperature", 38.6, confirmed=False)]
    assert run(found).status == "urgent"


def test_urgent_with_no_treatment_context_uses_the_stricter_row():
    a = run(findings(fever="yes", temperature=37.8), ctx=None)
    assert a.status == "urgent" and a.context is None


# --- the fever row depends on positive evidence ----------------------------------------------


def test_off_sact_row_needs_a_dose_over_56_days_ago_and_immunocompromised_false():
    off = context(days_since_dose=57, immunocompromised=False)
    a = run(findings(fever="no", temperature=37.8), ctx=off)
    assert a.status == "routine_review"  # amber on the "NOT ON SACT" row, not red
    assert fired(a) == {"ukons-one-amber"}


@pytest.mark.parametrize(
    "ctx",
    [
        context(days_since_dose=56, immunocompromised=False),  # inside the window
        context(days_since_dose=57, immunocompromised=None),  # immunocompromised unknown
        context(days_since_dose=57, immunocompromised=True),
        context(days_since_dose=None, immunocompromised=False),  # no dose on record
        None,
    ],
)
def test_without_positive_evidence_the_stricter_on_sact_row_applies(ctx):
    a = run(findings(fever="no", temperature=37.8), ctx=ctx)
    assert a.status == "urgent"


def test_a_dose_dated_in_the_future_is_not_evidence():
    future = context(days_since_dose=-3, immunocompromised=False)
    assert run(findings(fever="no", temperature=37.8), ctx=future).status == "urgent"


def test_off_sact_red_band_and_no_band_cases():
    off = context(days_since_dose=100, immunocompromised=False)
    assert run(findings(fever="yes", temperature=38.5), ctx=off).status == "urgent"
    # "unwell" has no band on the off-SACT row: never green, the nurse sees it
    a = run(findings(unwell="yes"), ctx=off)
    assert a.status == "routine_review" and "red-flag-present" in fired(a)


# --- ukons amber rules -----------------------------------------------------------------------


def test_one_amber_rating_is_a_routine_review_within_24_hours():
    a = run(findings(vomiting="yes", vomiting_episodes="episodes_3_5"))
    assert a.status == "routine_review"
    rule = next(t for t in a.triggered_rules if t.rule_id == "ukons-one-amber")
    assert rule.inputs["review_within_hours"] == 24 and "24" in rule.reason


def test_two_amber_ratings_escalate_to_urgent():
    a = run(
        findings(
            vomiting="yes",
            vomiting_episodes="episodes_3_5",
            nausea="yes",
            nausea_intake="intake_decreased",
        )
    )
    assert a.status == "urgent" and "ukons-two-amber" in fired(a)
    assert "ukons-one-amber" not in fired(a)


def test_diarrhoea_yes_with_dont_know_is_never_green():
    a = run(findings(diarrhoea="yes", diarrhoea_stools="dont_know"))
    assert a.status == "routine_review" and "ukons-one-amber" in fired(a)


def test_a_slightly_reduced_intake_is_green_in_ukons_but_still_reviewed_by_ctcae():
    a = run(findings(nausea="yes", nausea_intake="intake_slightly_less"))
    assert a.status == "routine_review" and fired(a) == {"grade-needs-assessment"}


# --- the other routine rules -----------------------------------------------------------------


def test_red_flag_present_without_a_ukons_rating_is_reviewed():
    a = run(findings(chills="yes", chills_severity="shivering"))
    assert a.status == "routine_review" and "red-flag-present" in fired(a)


def test_grade_needing_clinical_assessment_is_reviewed():
    a = run(findings(vomiting="yes", vomiting_episodes="episodes_1_2"))
    assert a.status == "routine_review" and fired(a) == {"grade-needs-assessment"}


def test_free_text_can_only_raise_to_a_review():
    a = run(findings() + [quoted_text()])
    assert a.status == "routine_review" and fired(a) == {"free-text-reported"}


def test_routine_review_wins_over_missing_information():
    a = run(
        findings(ALL_CLEAR | {"chills": None}, vomiting="yes", vomiting_episodes="episodes_1_2")
    )
    assert a.status == "routine_review" and a.missing_link_ids == ["chills"]
    assert "missing-items" in fired(a)


# --- missing information: not_mentioned and unknown never count as absent --------------------


def test_nothing_reported_is_insufficient_information_never_no_escalation():
    a = run([])
    assert a.status == "insufficient_information"
    assert set(a.missing_link_ids) == {
        "fever", "chills", "unwell", "nausea", "vomiting", "diarrhoea", "diarrhoea_blood"
    }  # fmt: skip


@pytest.mark.parametrize("link", sorted(ALL_CLEAR))
def test_one_missing_item_blocks_no_escalation(link):
    a = run(findings(**{link: None}))
    assert a.status == "insufficient_information" and a.missing_link_ids == [link]


@pytest.mark.parametrize("link", ["fever", "chills", "nausea", "diarrhoea_blood"])
def test_dont_know_is_unknown_not_absent(link):
    a = run(findings(**{link: "dont_know"}))
    assert a.status == "insufficient_information" and a.missing_link_ids == [link]


def test_a_follow_up_of_a_denied_symptom_is_not_asked_or_missing():
    a = run(findings())
    assert "chills_severity" not in a.missing_link_ids


def test_a_follow_up_dont_know_keeps_the_symptom_in_the_nurses_hands():
    a = run(findings(chills="yes", chills_severity="dont_know"))
    assert a.status == "routine_review"
    assert [(g.ctcae_term, g.at_least) for g in a.grades] == [("Chills", 1)]


def test_an_unconfirmed_no_to_a_red_flag_does_not_close_it():
    a = run(findings(fever=None) + [finding("fever", "no", confirmed=False)])
    assert "fever" in a.missing_link_ids and a.status == "insufficient_information"


# --- determinism, replay, traceability ------------------------------------------------------


def test_the_same_inputs_give_the_same_assessment():
    found = findings(fever="yes", temperature=38.4)
    assert run(found) == run(found)
    assert run(found).model_dump_json() == run(found).model_dump_json()


def test_the_result_does_not_depend_on_the_order_of_findings():
    found = findings(vomiting="yes", vomiting_episodes="episodes_3_5", nausea="yes")
    a, b = run(found), run(list(reversed(found)))
    for field in ("status", "grades", "triggered_rules", "missing_link_ids"):
        assert getattr(a, field) == getattr(b, field)


def test_replay_gives_back_the_stored_assessment_even_after_a_round_trip():
    original = run(findings(fever="yes", temperature=38.4))
    stored = Assessment.model_validate_json(original.model_dump_json())
    assert engine.replay(stored, PLAN, MAPPING, RULES) == original


def test_replay_refuses_other_versions():
    original = run(findings())
    newer = RULES.model_copy(update={"version": "2"})
    with pytest.raises(ValueError, match="versions it recorded"):
        engine.replay(original, PLAN, MAPPING, newer)


def test_an_assessment_records_its_versions_and_inputs():
    a = run(findings(fever="yes", temperature=38.4))
    assert (a.ruleset_version, a.mapping_version, a.engine_version) == ("1", "1", "1.0")
    assert (a.questionnaire_url, a.questionnaire_version) == (
        PLAN.questionnaire_url,
        PLAN.questionnaire_version,
    )
    assert a.assessed_at == NOW and a.context == context()
    assert all(g.status == "provisional" for g in a.grades)
    assert a.findings == findings(fever="yes", temperature=38.4)


def test_the_clock_is_the_one_passed_in():
    later = engine.assess(
        assessment_id="a2", checkin_id="c1", patient_id="p1",
        findings=findings(fever="no", temperature=37.8), plan=PLAN, mapping=MAPPING, rules=RULES,
        context=context(days_since_dose=50, immunocompromised=False), now=NOW + timedelta(days=7),
    )  # fmt: skip
    # 57 days after the dose, on the demo clock: the NOT ON SACT row applies (amber, not red)
    assert later.status == "routine_review"


# --- the content ----------------------------------------------------------------------------


def test_the_ruleset_belongs_to_the_plan_and_the_mapping():
    engine.check_content(PLAN, MAPPING, RULES)
    assert PLAN.rule_ids == [r.rule_id for r in RULES.rules]
    assert "DEMO" in RULES.approval and all(r.source_ref for r in RULES.rules)


def test_a_ruleset_cannot_drop_a_rule_kind():
    raw = RULES.model_dump()
    raw["rules"] = [r for r in raw["rules"] if r["kind"] != "missing_items"]
    with pytest.raises(ValidationError, match="every rule kind"):
        engine.Ruleset.model_validate(raw)


def test_a_red_rule_cannot_be_downgraded_in_the_file():
    raw = RULES.model_dump()
    raw["rules"][0]["outcome"] = "routine_review"
    with pytest.raises(ValidationError, match="red UKONS rating is urgent"):
        engine.Ruleset.model_validate(raw)


def test_a_reason_may_only_use_its_kinds_placeholders():
    raw = RULES.model_dump()
    raw["rules"][0]["reason_cs"] = "{items}"
    with pytest.raises(ValidationError, match="placeholder"):
        engine.Ruleset.model_validate(raw)


def test_mismatched_content_is_a_visible_failure():
    other = RULES.model_copy(update={"mapping": engine.MappingRef(content_id="x", version="1")})
    with pytest.raises(ValueError, match="another mapping"):
        engine.check_content(PLAN, MAPPING, other)


# --- grouping --------------------------------------------------------------------------------


def test_grouping_new_grouped_and_worsened():
    mild = run(findings(vomiting="yes", vomiting_episodes="episodes_1_2"), assessment_id="a1")
    assert engine.group_with_open(mild, []).kind == "new"
    again = run(findings(vomiting="yes", vomiting_episodes="episodes_1_2"), assessment_id="a2")
    same = engine.group_with_open(again, [mild])
    assert (same.kind, same.with_id) == ("grouped", "a1")
    worse = run(findings(fever="yes", temperature=38.5), assessment_id="a3")
    grouped = engine.group_with_open(worse, [mild])
    assert grouped.kind == "worsened" and any("urgent" in r for r in grouped.reasons)


def test_grouping_a_higher_grade_renews_the_alert_and_other_patients_are_ignored():
    first = run(findings(diarrhoea="yes", diarrhoea_stools="stools_4_6"), assessment_id="a1")
    worse = run(findings(diarrhoea="yes", diarrhoea_stools="stools_7_9"), assessment_id="a2")
    result = engine.group_with_open(worse, [first])
    assert result.kind == "worsened" and "Diarrhea: grade 2 → 3" in result.reasons
    stranger = first.model_copy(update={"patient_id": "p2"})
    assert engine.group_with_open(worse, [stranger]).kind == "new"


def test_grouping_a_newly_triggered_rule_renews_the_alert():
    vomiting = run(findings(vomiting="yes", vomiting_episodes="episodes_3_5"), assessment_id="a1")
    chills = run(findings(chills="yes", chills_severity="shivering"), assessment_id="a2")
    result = engine.group_with_open(chills, [vomiting])
    assert result.kind == "worsened" and any(r.startswith("rule ") for r in result.reasons)


# --- purity ----------------------------------------------------------------------------------


@pytest.mark.parametrize("module", ["ctcae.py", "assessment.py"])
def test_the_domain_modules_are_pure(module):
    source = (Path(engine.__file__).parent / module).read_text(encoding="utf-8")
    assert not re.search(
        r"datetime\.now|date\.today|time\.time|import (httpx|requests|iris)", source
    )
    assert "hlidac.iris" not in source and "hlidac.store" not in source
