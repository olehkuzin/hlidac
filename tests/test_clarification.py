"""WP5: targeted clarification on the demo Questionnaire and mapping. Hand-made check-ins; no
patient data."""

from factories import T0, checkin

from hlidac import assessment, clarification, extraction, wizard
from hlidac.ctcae import read_mapping
from hlidac.plans import load_plans
from hlidac.schemas import (
    DONT_KNOW,
    Finding,
    InboundMessage,
    Quantity,
    QuoteSource,
)

PLAN = next(iter(load_plans().values()))
ATTRS = clarification.grading_attributes(read_mapping())


def text(message_id="m1", body="TEST zpráva"):
    return InboundMessage(
        message_id=message_id,
        checkin_id="c1",
        patient_id="p1",
        session_account_id="a1",
        reported_by="patient",
        received_at=T0,
        kind="text",
        text=body,
    )


def answer_message(message_id, link_id, code):
    button = wizard.button_answer(PLAN.item(link_id), code)
    message = InboundMessage(
        message_id=message_id,
        checkin_id="c1",
        patient_id="p1",
        session_account_id="a1",
        reported_by="patient",
        received_at=T0,
        kind="answer",
        answer=button,
    )
    return message, wizard.finding_from_answer(button, message_id)


def said(link_id, state="present", code=None, value=None):
    return Finding(
        link_id=link_id,
        state=state,
        code=code,
        value=Quantity(value=value, unit="Cel") if value is not None else None,
        source=QuoteSource(kind="quote", message_id="m1", quote="TEST citace"),
        extractor_version=extraction.EXTRACTOR_VERSION,
    )


def written(*findings):
    return checkin(messages=[text()], findings=list(findings), extracted_message_ids=["m1"])


def answer(state, *answers):
    """Answer by buttons, in order, as the patient app does."""
    for n, (link_id, code) in enumerate(answers):
        message, finding = answer_message(f"b{n}", link_id, code)
        if wizard.needs_confirmation(PLAN.item(link_id), finding):
            finding = finding.evolve(confirmed=True)
        state = state.evolve(
            messages=[*state.messages, message],
            findings=wizard.with_finding(state.findings, finding),
        )
    return state


def links(items):
    return [item.link_id for item in items]


def test_the_grading_attributes_come_from_the_mapping():
    assert ATTRS["fever"] == {"temperature"}
    # Vomiting's v5.0 grade reads no attribute; its episodes are asked as a follow-up (enableWhen).
    assert ATTRS["vomiting"] == frozenset()
    assert ATTRS["nausea"] == {"nausea_intake"}
    assert ATTRS["diarrhoea"] == {"diarrhoea_stools"}


def test_without_written_text_every_enabled_item_is_asked_in_order():
    state = checkin()
    assert clarification.questions(PLAN, state, ATTRS) == wizard.question_items(PLAN, state)
    assert clarification.next_item(PLAN, state, ATTRS) == wizard.next_item(PLAN, state)


def test_after_writing_only_the_missing_items_are_asked_red_flags_first():
    # "Dneska jsem zvracela." — vomiting reported, the number of episodes not said.
    state = written(said("vomiting", code="yes"))
    assert links(clarification.questions(PLAN, state, ATTRS)) == [
        "fever", "chills", "unwell", "diarrhoea_blood",  # red flags, never skipped (G3)
        "vomiting_episodes",  # the follow-up of what was reported
        "nausea", "diarrhoea",  # required items nobody talked about, within the turns
    ]  # fmt: skip
    assert "temperature" not in links(clarification.questions(PLAN, state, ATTRS))


def test_a_reported_fever_asks_for_the_temperature():
    state = written(said("fever", code="yes"))
    asked = links(clarification.questions(PLAN, state, ATTRS))
    assert asked.index("temperature") < asked.index("nausea")


def test_quoted_red_flags_and_numbers_are_shown_back_without_using_turns():
    state = written(
        said("fever", code="yes"), said("temperature", value=38.6), said("chills", "absent", "no")
    )
    asked = links(clarification.questions(PLAN, state, ATTRS))
    # reported values wait for their button before they can raise urgency, so they come first
    # (D49); the quoted denial waits for the closing summary instead (D58)
    assert asked[:3] == ["fever", "temperature", "unwell"] and "chills" not in asked
    assert "chills" in links(wizard.to_confirm(PLAN, state))
    # the three turns still go to the required items
    assert [i for i in asked if i in ("nausea", "vomiting", "diarrhoea")] == [
        "nausea", "vomiting", "diarrhoea"
    ]  # fmt: skip


def test_a_quoted_denial_of_a_red_flag_waits_for_the_closing_summary():
    """D58: not asked again one by one; it counts only once the summary is confirmed."""
    state = written(said("fever", "absent", "no"))
    assert clarification.next_item(PLAN, state, ATTRS).link_id == "chills"
    assert links(wizard.to_confirm(PLAN, state)) == ["fever"]
    assert "fever" not in links(wizard.for_assessment(PLAN, state).findings)
    confirmed = state.evolve(findings=[said("fever", "absent", "no").evolve(confirmed=True)])
    assert "fever" in links(wizard.for_assessment(PLAN, confirmed).findings)


def test_a_quoted_non_red_flag_answer_is_not_asked_again():
    state = written(said("nausea", "absent", "no"))
    assert "nausea" not in links(clarification.questions(PLAN, state, ATTRS))


def test_turns_are_bounded_and_the_rest_stays_missing():
    state = written(said("vomiting", code="yes"))
    state = answer(
        state,
        ("fever", "no"), ("chills", "no"), ("unwell", "no"), ("diarrhoea_blood", "no"),
        ("vomiting_episodes", "episodes_1_2"), ("nausea", "yes"), ("diarrhoea", "no"),
    )  # fmt: skip
    assert clarification.turns_used(PLAN, state) == PLAN.max_clarification_turns == 3
    # nausea "yes" enables its intake question, but the turns are used up
    assert clarification.next_item(PLAN, state, ATTRS) is None
    assert not wizard.is_done(PLAN.item("nausea_intake"), state)
    # so the report can't end as no_escalation: the engine sees the item as missing
    findings = {f.link_id: f for f in state.findings}
    assert "nausea_intake" in assessment.missing_link_ids(PLAN, findings)


def test_red_flag_answers_never_use_turns():
    state = answer(
        written(), ("fever", "no"), ("chills", "no"), ("unwell", "no"), ("diarrhoea_blood", "no")
    )
    assert clarification.turns_used(PLAN, state) == 0


def test_dont_know_uses_a_turn_and_closes_the_question():
    state = answer(written(said("vomiting", code="yes")), ("vomiting_episodes", DONT_KNOW))
    assert clarification.turns_used(PLAN, state) == 1
    episodes = next(f for f in state.findings if f.link_id == "vomiting_episodes")
    assert episodes.state == "unknown"
    assert "vomiting_episodes" not in [
        i.link_id
        for i in clarification.questions(PLAN, state, ATTRS)
        if not wizard.is_done(i, state)
    ]


def test_answers_before_the_text_use_no_turns():
    state = answer(checkin(messages=[]), ("nausea", "no"))
    state = state.evolve(messages=[*state.messages, text()], extracted_message_ids=["m1"])
    assert clarification.turns_used(PLAN, state) == 0


def test_answered_questions_stay_in_the_list_so_back_and_progress_work():
    state = answer(written(said("vomiting", code="yes")), ("fever", "no"))
    items = clarification.questions(PLAN, state, ATTRS)
    assert items[0].link_id == "fever"
    assert clarification.previous_item(PLAN, state, "chills", ATTRS).link_id == "fever"


def _walk(state, choose):
    """Run the questions as the patient app does; return (link_id, position, total, back) per
    screen. A quoted red flag or number is confirmed on its screen; the rest is answered."""
    screens = []
    while (item := clarification.next_item(PLAN, state, ATTRS)) is not None:
        items = clarification.questions(PLAN, state, ATTRS)
        back = clarification.previous_item(PLAN, state, item.link_id, ATTRS)
        screens.append((item.link_id, items.index(item) + 1, len(items), back and back.link_id))
        found = next((f for f in state.findings if f.link_id == item.link_id), None)
        if found is not None and isinstance(found.source, QuoteSource):
            state = state.evolve(
                findings=wizard.with_finding(state.findings, found.evolve(confirmed=True))
            )
        else:
            state = answer(state, (item.link_id, choose(item)))
    return screens


def _assert_steady(screens):
    for n, (_, position, _, back) in enumerate(screens):
        assert position == n + 1  # the counter never jumps or goes back
        assert back == (screens[n - 1][0] if n else None)  # "Zpět" = the screen before


def test_confirmed_quotes_keep_their_place_so_progress_and_back_stay_steady():
    state = written(said("fever", code="yes"), said("chills", state="absent", code="no"))
    screens = _walk(state, lambda i: "no" if i.red_flag else (
        i.options[0].code if i.type == "choice" else DONT_KNOW))  # fmt: skip
    # D54: the confirmed fever waits for its thermometer reading, asked right after it; the
    # quoted denial of chills is confirmed in the closing summary (D58), not on its own screen
    assert [s[0] for s in screens][:3] == ["fever", "temperature", "unwell"]
    _assert_steady(screens)


def test_a_follow_up_opened_later_comes_after_the_questions_already_asked():
    state = written(said("fever", code="yes"))
    screens = _walk(state, lambda i: "no" if i.red_flag else (
        i.options[0].code if i.type == "choice" else DONT_KNOW))  # fmt: skip
    links_asked = [s[0] for s in screens]
    assert links_asked.index("nausea") < links_asked.index("nausea_intake")
    _assert_steady(screens)


# --- D56: at least three questions, and the patient's previous report ------------------------


def test_the_chat_asks_at_least_three_questions_after_free_text():
    # Everything required was said and needs no confirmation: before D56 nothing more was asked.
    calm = written(
        *(said(link, "absent") for link in ("nausea", "vomiting", "diarrhoea")),
        *(said(link, "absent").evolve(confirmed=True)
          for link in ("fever", "chills", "unwell", "diarrhoea_blood")),
    )  # fmt: skip
    asked = clarification.questions(PLAN, calm, ATTRS)
    assert len(asked) >= clarification.MIN_QUESTIONS


def test_the_minimum_adds_open_items_after_the_wanted_ones(monkeypatch):
    state = written(said("vomiting", "present", "yes"))
    monkeypatch.setattr(clarification, "MIN_QUESTIONS", 0)
    few = links(clarification.questions(PLAN, state, ATTRS))
    monkeypatch.setattr(clarification, "MIN_QUESTIONS", len(few) + 1)
    more = links(clarification.questions(PLAN, state, ATTRS))
    assert more[: len(few)] == few and len(more) == len(few) + 1


def test_a_symptom_from_the_previous_report_is_asked_again_early():
    state = written()
    without = links(clarification.questions(PLAN, state, ATTRS))
    with_history = links(clarification.questions(PLAN, state, ATTRS, frozenset({"diarrhoea"})))
    assert with_history.index("diarrhoea") < with_history.index("nausea")
    assert without.index("nausea") < without.index("diarrhoea")


def test_reported_before_reads_the_latest_sent_report_only():
    def report(cid, status, at, *found):
        return checkin(checkin_id=cid, status=status, updated_at=T0 + at, findings=list(found))

    from datetime import timedelta

    older = report("c0", "completed", timedelta(0), said("nausea", "present", "yes"))
    latest = report(
        "c1", "completed", timedelta(days=1),
        said("diarrhoea", "present", "yes"), said("vomiting", "absent"), said("other", "present"),
    )  # fmt: skip
    open_one = report("c2", "in_progress", timedelta(days=2), said("fever", "present", "yes"))
    assert clarification.reported_before(PLAN, [older, latest, open_one]) == {"diarrhoea"}
    assert clarification.reported_before(PLAN, [open_one]) == frozenset()
