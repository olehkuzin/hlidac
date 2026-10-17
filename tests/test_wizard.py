"""WP9: the wizard asks each enabled item once and records answers without guessing."""

import pytest
from factories import checkin, ui_plan

from hlidac import wizard
from hlidac.schemas import DONT_KNOW, TYPED_VALUE


def answer(state, link_id, code, number=None, confirmed=False):
    plan = ui_plan()
    button = wizard.button_answer(plan.item(link_id), code, number)
    finding = wizard.finding_from_answer(button, f"m-{link_id}")
    if confirmed:
        finding = finding.evolve(confirmed=True)
    return state.evolve(findings=wizard.with_finding(state.findings, finding))


def test_walks_the_enabled_questions_in_order():
    plan, state = ui_plan(), checkin()
    assert [i.link_id for i in wizard.question_items(plan, state)] == ["fever", "nausea"]
    assert wizard.next_item(plan, state).link_id == "fever"
    state = answer(state, "fever", "yes", confirmed=True)
    assert [i.link_id for i in wizard.question_items(plan, state)] == [
        "fever",
        "temperature",
        "nausea",
    ]
    assert wizard.next_item(plan, state).link_id == "temperature"


def test_free_text_items_are_never_asked_one_by_one():
    assert "note" not in [i.link_id for i in wizard.question_items(ui_plan(), checkin())]


def test_red_flag_answer_counts_only_once_confirmed():
    """D58: the chat moves on after "Ne", but the denial waits for the closing summary's
    confirmation before it can close the red flag."""
    plan = ui_plan()
    state = answer(checkin(), "fever", "no")
    assert wizard.next_item(plan, state).link_id == "nausea"  # not asked again
    assert [f.link_id for f in wizard.to_confirm(plan, state)] == ["fever"]
    assert not [f for f in wizard.for_assessment(plan, state).findings if f.link_id == "fever"]
    state = answer(state, "fever", "no", confirmed=True)
    assert wizard.to_confirm(plan, state) == []
    assert [f.link_id for f in wizard.for_assessment(plan, state).findings] == ["fever"]


def test_dont_know_needs_no_confirmation_and_is_unknown():
    state = answer(checkin(), "fever", DONT_KNOW)
    finding = state.findings[0]
    assert finding.state == "unknown"
    assert not wizard.needs_confirmation(ui_plan().item("fever"), finding)


def test_only_no_records_absent():
    states = {code: answer(checkin(), "nausea", code).findings[0].state for code in ("no", "mild")}
    assert states == {"no": "absent", "mild": "present"}


def test_typed_number_keeps_its_value_and_unit():
    state = answer(
        answer(checkin(), "fever", "yes", confirmed=True), "temperature", TYPED_VALUE, 38.4
    )
    finding = next(f for f in state.findings if f.link_id == "temperature")
    assert (finding.state, finding.value.value, finding.value.unit) == ("present", 38.4, "Cel")
    assert wizard.needs_confirmation(ui_plan().item("temperature"), finding)


def test_quantity_items_offer_dont_know():
    finding = wizard.finding_from_answer(
        wizard.button_answer(ui_plan().item("temperature"), DONT_KNOW), "m1"
    )
    assert finding.state == "unknown"


@pytest.mark.parametrize(
    ("link_id", "code", "number"),
    [
        ("fever", "maybe", None),  # not an option
        ("fever", "", None),
        ("fever", TYPED_VALUE, 38.0),  # a number for a choice item
        ("temperature", TYPED_VALUE, None),  # no number
        ("temperature", "yes", None),
    ],
)
def test_rejects_answers_the_item_does_not_offer(link_id, code, number):
    with pytest.raises(ValueError):
        wizard.button_answer(ui_plan().item(link_id), code, number)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("38,4", 38.4), ("38.4", 38.4), (" 37 ", 37.0), ("38,", None), ("abc", None), ("", None)],
)
def test_parse_number(raw, expected):
    assert wizard.parse_number(raw) == expected


def test_previous_item_follows_the_enabled_order():
    plan = ui_plan()
    state = answer(checkin(), "fever", "no", confirmed=True)
    assert wizard.previous_item(plan, state, "nausea").link_id == "fever"
    assert wizard.previous_item(plan, state, "fever") is None
