"""WP8: free-text extraction (G1, G2). No live model: replies are recorded or hand-written JSON, and
every message is team-written TEST text with no patient data."""

import json

import pytest
from factories import T0, answered, ui_plan

from hlidac import extraction
from hlidac.extraction import ExtractionFailed, InvalidReply, merge, validate_reply
from hlidac.models import ModelCallFailed, ModelReply
from hlidac.plans import load_plans
from hlidac.schemas import DONT_KNOW, ExtractionResult, InboundMessage, QuoteSource

PLAN = ui_plan()
DEMO = next(iter(load_plans().values()))


def reply(**items) -> str:
    """A reply naming every TEST item; unnamed ones are not_mentioned."""
    blank = {
        "fever": {"state": "not_mentioned", "option": None, "quote": None},
        "temperature": {"state": "not_mentioned", "value": None, "quote": None},
        "nausea": {"state": "not_mentioned", "option": None, "quote": None},
        "note": {"state": "not_mentioned", "quote": None},
    }
    return json.dumps(blank | items, ensure_ascii=False)


def message(text: str, message_id: str = "m1") -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        checkin_id="c1",
        patient_id="p1",
        session_account_id="a1",
        reported_by="patient",
        received_at=T0,
        kind="text",
        text=text,
    )


def check(raw: str, text: str) -> ExtractionResult:
    return validate_reply(raw, PLAN, "m1", [text])


# --- what the model gets ------------------------------------------------------------------------


def test_the_schema_is_strict_and_names_every_item():
    schema = extraction.reply_schema(DEMO)
    assert schema["required"] == [item.link_id for item in DEMO.items]
    assert schema["additionalProperties"] is False
    for item in DEMO.items:
        part = schema["properties"][item.link_id]
        assert part["additionalProperties"] is False and part["required"] == list(
            part["properties"]
        )
        if item.type == "choice":
            assert part["properties"]["option"]["enum"] == [o.code for o in item.options] + [None]


def test_the_instructions_carry_only_approved_items_and_no_patient_data():
    from hlidac.models import pseudonymise

    text = extraction.instructions(DEMO)
    for item in DEMO.items:
        assert item.text_cs in text
    # the gateway refuses instructions that the masking would change
    assert pseudonymise(text) == text
    schema = json.dumps(extraction.reply_schema(DEMO), ensure_ascii=False, sort_keys=True)
    assert pseudonymise(schema) == schema


# --- the quote check (G2) -----------------------------------------------------------------------


def test_a_quoted_report_becomes_findings_with_their_quotes():
    text = "Teplotu mám 38,6 a je mi trochu špatně od žaludku."
    result = check(
        reply(
            fever={"state": "present", "option": None, "quote": "Teplotu mám 38,6"},
            temperature={"state": "present", "value": 38.6, "quote": "Teplotu mám 38,6"},
            nausea={"state": "present", "option": "mild", "quote": "trochu špatně od žaludku"},
        ),
        text,
    )
    found = {f.link_id: f for f in result.findings}
    assert (found["fever"].state, found["fever"].code) == ("present", "yes")
    assert found["temperature"].value.value == 38.6 and found["temperature"].value.unit == "Cel"
    assert found["nausea"].code == "mild"
    for finding in result.findings:
        assert isinstance(finding.source, QuoteSource) and finding.source.message_id == "m1"
        assert finding.extractor_version == extraction.EXTRACTOR_VERSION
        assert not finding.confirmed  # a quote never arrives confirmed (G5)
    assert result.uncertainties == []


def test_a_fabricated_quote_is_dropped_and_named():
    result = check(
        reply(fever={"state": "absent", "option": "no", "quote": "horečku nemám"}),
        "Je mi špatně.",
    )
    assert result.findings == []
    assert result.uncertainties == ["fever: the quote is not in the message"]


def test_a_quote_may_differ_only_in_case_and_spacing():
    result = check(
        reply(fever={"state": "absent", "option": "no", "quote": "horečku   nemám"}),
        "Horečku nemám.",
    )
    assert [f.state for f in result.findings] == ["absent"]


def test_a_number_that_is_not_in_its_quote_is_dropped():
    result = check(
        reply(temperature={"state": "present", "value": 39.5, "quote": "mám teplotu"}),
        "Asi mám teplotu.",
    )
    assert result.findings == []
    assert result.uncertainties == ["temperature: the number is not in the quote"]


def test_a_finding_without_a_quote_is_dropped():
    result = check(reply(nausea={"state": "present", "option": "mild", "quote": None}), "Nic.")
    assert result.findings == [] and result.uncertainties == ["nausea: no quote"]


def test_dont_know_is_unknown_never_absent():
    text = "Nevím, jestli mám horečku, neměřila jsem."
    for option, state in ((DONT_KNOW, "absent"), (None, "unknown")):
        result = check(
            reply(fever={"state": state, "option": option, "quote": "Nevím, jestli mám horečku"}),
            text,
        )
        [finding] = result.findings
        assert finding.state == "unknown" and finding.code is None


def test_not_mentioned_makes_no_finding():
    assert check(reply(), "Dobrý den.").findings == []


def test_a_present_answer_without_a_band_is_dropped_so_the_item_is_asked():
    text = "Je mi špatně od žaludku."
    result = check(
        reply(nausea={"state": "present", "option": None, "quote": "špatně od žaludku"}), text
    )
    assert result.findings == [] and result.uncertainties == ["nausea: no answer option"]


def test_a_denial_must_fit_the_item():
    result = check(
        reply(fever={"state": "absent", "option": "yes", "quote": "horečku nemám"}),
        "Horečku nemám.",
    )
    assert result.findings == []


def test_free_text_counts_only_when_something_was_said():
    text = "Bolí mě koleno."
    said = check(reply(note={"state": "present", "quote": "Bolí mě koleno"}), text)
    assert [(f.link_id, f.state, f.code) for f in said.findings] == [("note", "present", None)]
    assert check(reply(note={"state": "absent", "quote": "Bolí mě koleno"}), text).findings == []


def test_a_quote_from_the_masked_text_the_model_saw_is_accepted():
    masked = "Tady [JMÉNO], horečku nemám."
    result = validate_reply(
        reply(fever={"state": "absent", "option": None, "quote": "[JMÉNO], horečku nemám"}),
        PLAN,
        "m1",
        ["Tady TEST-jméno, horečku nemám.", masked],
    )
    assert [f.code for f in result.findings] == ["no"]


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        json.dumps({"fever": {"state": "present", "option": None, "quote": "x"}}),  # items missing
        reply(extra={"state": "present", "quote": "x"}),  # an unknown item
        reply(fever={"state": "present", "option": "maybe", "quote": "x"}),  # option not offered
        reply(fever={"state": "bad", "option": None, "quote": "x"}),  # state outside the enum
        reply(fever={"state": "present", "option": None, "quote": "x", "grade": 3}),  # extra field
        reply(temperature={"state": "present", "value": "38", "quote": "38"}),  # a string number
    ],
)
def test_a_reply_that_breaks_the_schema_is_invalid(raw):
    with pytest.raises(InvalidReply):
        check(raw, "x 38")


# --- the bounded retry (G1) ---------------------------------------------------------------------


class Gateway:
    """Stands in for ModelGateway.extract_json with recorded replies (or failures)."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def extract_json(self, **kw):
        self.calls.append(kw)
        answer = self.replies.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return ModelReply(text=answer, model="test-model", model_version=None)


TEXT = "Horečku nemám."
DENIAL = reply(fever={"state": "absent", "option": "no", "quote": "Horečku nemám"})


def test_an_invalid_reply_is_retried_once():
    gateway = Gateway("not json", DENIAL)
    result = extraction.extract(gateway, PLAN, message(TEXT))
    assert [f.state for f in result.findings] == ["absent"] and len(gateway.calls) == 2


@pytest.mark.parametrize(
    "failures", [("not json", "[]"), (ModelCallFailed("x"), ModelCallFailed("y"))]
)
def test_two_failures_end_as_a_visible_failure_never_success(failures):
    gateway = Gateway(*failures, DENIAL)
    with pytest.raises(ExtractionFailed):
        extraction.extract(gateway, PLAN, message(TEXT))
    assert len(gateway.calls) == extraction.MAX_ATTEMPTS


def test_the_call_carries_only_the_message_and_approved_content():
    gateway = Gateway(DENIAL)
    extraction.extract(gateway, PLAN, message(TEXT, "m7"), known_names=["TEST Jméno"])
    [call] = gateway.calls
    assert call["patient_text"] == TEXT
    assert call["instructions"] == extraction.instructions(PLAN)
    assert call["schema"] == extraction.reply_schema(PLAN)
    assert call["target"] == "message:m7" and call["known_names"] == ["TEST Jméno"]
    assert set(call) == {
        "instructions", "patient_text", "schema", "schema_name", "target", "known_names"
    }  # fmt: skip


def test_the_quote_check_uses_the_masked_text_too():
    text = "Tady TEST Jméno, horečku nemám."
    gateway = Gateway(
        reply(fever={"state": "absent", "option": None, "quote": "[JMÉNO] [JMÉNO], horečku nemám"})
    )
    result = extraction.extract(gateway, PLAN, message(text), known_names=["TEST Jméno"])
    assert [f.state for f in result.findings] == ["absent"]


def test_recorded_demo_reply_validates_against_the_demo_questionnaire():
    """A reply in the shape the deployment returns for the demo Questionnaire."""
    text = "Dneska jsem třikrát zvracela a skoro nic nepila. Horečku nemám."
    items = {item.link_id: item for item in DEMO.items}
    raw = {}
    for link_id, item in items.items():
        empty = {"state": "not_mentioned", "quote": None}
        if item.type == "choice":
            empty["option"] = None
        if item.type == "quantity":
            empty["value"] = None
        raw[link_id] = empty
    raw["vomiting"] = {"state": "present", "option": "yes", "quote": "třikrát zvracela"}
    raw["vomiting_episodes"] = {
        "state": "present", "option": "episodes_3_5", "quote": "třikrát zvracela"
    }  # fmt: skip
    raw["fever"] = {"state": "absent", "option": "no", "quote": "Horečku nemám"}
    result = validate_reply(json.dumps(raw, ensure_ascii=False), DEMO, "m1", [text])
    assert {(f.link_id, f.code) for f in result.findings} == {
        ("vomiting", "yes"), ("vomiting_episodes", "episodes_3_5"), ("fever", "no")
    }  # fmt: skip


# --- merging (raise-only) -----------------------------------------------------------------------


def quoted(link_id, state, code=None, quote="TEST"):
    return extraction.Finding(
        link_id=link_id,
        state=state,
        code=code,
        source=QuoteSource(kind="quote", message_id="m1", quote=quote),
        extractor_version=extraction.EXTRACTOR_VERSION,
    )


def test_merge_fills_items_nobody_answered():
    result = ExtractionResult(findings=[quoted("nausea", "present", "mild")])
    assert [f.code for f in merge([], result, PLAN)] == ["mild"]


def test_merge_never_overwrites_a_button_answer_with_a_denial():
    button = answered("nausea", code="mild")
    result = ExtractionResult(findings=[quoted("nausea", "absent", "no")])
    assert merge([button], result, PLAN) == [button]


def test_merge_never_turns_dont_know_into_absent():
    dont_know = answered("fever", code=DONT_KNOW, state="unknown")
    result = ExtractionResult(findings=[quoted("fever", "absent", "no")])
    assert merge([dont_know], result, PLAN) == [dont_know]


def test_merge_lets_a_quote_raise_a_denial_to_present():
    denied = answered("fever", code="no", state="absent")
    result = ExtractionResult(findings=[quoted("fever", "present", "yes")])
    assert [f.state for f in merge([denied], result, PLAN)] == ["present"]


def test_merge_leaves_out_a_follow_up_whose_parent_is_not_yes():
    from hlidac.schemas import Quantity

    temperature = extraction.Finding(
        link_id="temperature",
        state="present",
        value=Quantity(value=38.0, unit="Cel"),
        source=QuoteSource(kind="quote", message_id="m1", quote="38"),
        extractor_version=extraction.EXTRACTOR_VERSION,
    )
    alone = ExtractionResult(findings=[temperature])
    assert merge([], alone, PLAN) == []
    with_fever = ExtractionResult(findings=[quoted("fever", "present", "yes"), temperature])
    assert {f.link_id for f in merge([], with_fever, PLAN)} == {"fever", "temperature"}


def test_an_unread_message_fills_the_free_text_item_raise_only():
    sent = message("TEST bolí mě záda", "m7")
    kept = extraction.keep_unread([answered("fever", code="no", state="absent")], PLAN, sent)
    [note] = [f for f in kept if f.link_id == "note"]
    assert note.state == "present" and note.source.message_id == "m7"
    assert [f.link_id for f in kept] == ["fever", "note"]  # the buttons stay as they are
    said = quoted("note", "present", quote="TEST bolí")
    assert extraction.keep_unread([said], PLAN, sent) == [said]  # already present: unchanged
    with pytest.raises(ValueError, match="free-text item"):
        extraction.keep_unread([], PLAN.evolve(items=PLAN.items[:-1]), sent)
