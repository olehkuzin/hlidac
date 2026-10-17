"""D54: "Mám teplotu" waits for the thermometer reading while the report is open; the reading
decides (UKONS v3 row 5 on SACT); no reading never lowers urgency. Invented data only."""

import itertools

import pytest
from demo_helpers import ALL_CLEAR, MAPPING, NOW, PLAN, RULES, context, finding, findings
from factories import checkin
from test_clarification import text

from hlidac import clarification, wizard
from hlidac.assessment import assess
from hlidac.plans import load_plans
from hlidac.schemas import MonitoringPlan


def _status(found, ctx=None):
    return assess(
        assessment_id="a1", checkin_id="c1", patient_id="p1", findings=found, plan=PLAN,
        mapping=MAPPING, rules=RULES, context=ctx or context(), now=NOW,
    ).status  # fmt: skip


def _seen(state):
    return {f.link_id for f in wizard.for_assessment(PLAN, state).findings}


def test_the_demo_plans_measure_fever_by_temperature():
    assert PLAN.measured_by == {"fever": "temperature"}


def test_reported_fever_waits_for_the_reading_while_the_report_is_open():
    state = checkin(findings=[finding("fever", "yes")])
    assert "fever" not in _seen(state)
    assert wizard.next_item(PLAN, state).link_id == "temperature"
    # an unconfirmed number doesn't end the wait (G5) ...
    typed = state.evolve(findings=[*state.findings, finding("temperature", 37.0, confirmed=False)])
    assert "fever" not in _seen(typed)
    # ... a confirmed one or "Nevím" does
    for reading in (finding("temperature", 37.0), finding("temperature", "dont_know")):
        done = state.evolve(findings=[*state.findings, reading])
        assert "fever" in _seen(done)


def test_a_sent_report_never_holds_the_fever_back():
    state = checkin(findings=[finding("fever", "yes")], status="completed")
    assert "fever" in _seen(state)


def test_a_high_unconfirmed_reading_is_urgent_at_once():
    state = checkin(findings=[finding("fever", "yes"), finding("temperature", 38.5, False)])
    assert _status(wizard.for_assessment(PLAN, state).findings) == "urgent"


@pytest.mark.parametrize(
    ("reading", "expected"),
    [
        (None, "urgent"),  # reported fever, no reading: stays red
        ("dont_know", "urgent"),
        (38.0, "urgent"),
        (37.5, "urgent"),
        (35.5, "urgent"),
        (37.4, "routine_review"),  # reported fever, normal reading: amber, never green
        (36.6, "routine_review"),
    ],
)
def test_on_sact_the_reading_decides(reading, expected):
    assert _status(findings(fever="yes", temperature=reading)) == expected


def test_a_normal_reading_without_reported_fever_is_still_all_clear():
    assert _status(findings(temperature=36.6)) == "no_escalation"
    assert _status(findings(ALL_CLEAR)) == "no_escalation"


def test_measured_by_must_name_a_choice_and_an_always_asked_quantity():
    data = PLAN.model_dump()
    for bad in ({"temperature": "fever"}, {"fever": "chills"}, {"nope": "temperature"}):
        with pytest.raises(ValueError, match="measured_by"):
            MonitoringPlan.model_validate({**data, "measured_by": bad})


# --- D55: a confirmed high reading answers the fever question ---------------------------------


def _asked_next(*found):
    state = checkin(messages=[text()], findings=list(found), extracted_message_ids=["m1"])
    return clarification.next_item(PLAN, state).link_id


def test_the_demo_plans_take_a_reading_of_38_as_the_fever_answer():
    assert PLAN.answered_by_reading == {"fever": 38.0}


@pytest.mark.parametrize("reading", [38.0, 39.5])
def test_a_confirmed_high_reading_answers_the_fever_question(reading):
    assert _asked_next(finding("temperature", reading)) != "fever"
    # on SACT the reading itself keeps it urgent, so not asking lowers nothing
    assert _status(findings(fever=None, temperature=reading)) == "urgent"


def test_off_sact_a_reading_of_38_is_amber_whatever_the_fever_answer():
    off_sact = context(100, immunocompromised=False)
    for fever in (None, "yes", "no", "dont_know"):
        assert _status(findings(fever=fever, temperature=38.0), off_sact) == "routine_review"


SEVERITY = ["no_escalation", "insufficient_information", "routine_review", "urgent"]


def _plan_status(plan, found, ctx):
    return assess(
        assessment_id="a1", checkin_id="c1", patient_id="p1", findings=found, plan=plan,
        mapping=MAPPING, rules=RULES, context=ctx, now=NOW,
    ).status  # fmt: skip


def test_skipping_an_answered_by_reading_question_never_lowers_urgency_in_any_plan():
    """The invariant behind D55, for every plan and threshold: from the threshold up, the outcome
    with the symptom's question skipped is at least as urgent as with any answer to it."""
    contexts = [context(), context(100, immunocompromised=False), None]
    checked = 0
    for plan in load_plans().values():
        for symptom, threshold in plan.answered_by_reading.items():
            measure = plan.measured_by[symptom]
            readings = [threshold + step / 10 for step in range(0, 31, 5)]
            for ctx, reading in itertools.product(contexts, readings):

                def level(answer):
                    found = findings(**{symptom: answer, measure: reading})  # noqa: B023
                    return SEVERITY.index(_plan_status(plan, found, ctx))  # noqa: B023

                skipped = level(None)
                answered = max(level(a) for a in ("yes", "no", "dont_know"))
                assert skipped >= answered, (plan.questionnaire_url, ctx, reading)
                checked += 1
    assert checked


@pytest.mark.parametrize("reading", [37.0, 37.9])
def test_a_lower_reading_never_stands_in_for_no(reading):
    assert _asked_next(finding("temperature", reading)) == "fever"


def test_an_unconfirmed_high_reading_answers_nothing_yet():
    assert _asked_next(finding("temperature", 38.5, confirmed=False)) == "fever"


def test_answered_by_reading_needs_a_measured_by_item():
    data = PLAN.model_dump()
    with pytest.raises(ValueError, match="answered_by_reading"):
        MonitoringPlan.model_validate({**data, "answered_by_reading": {"chills": 38.0}})
