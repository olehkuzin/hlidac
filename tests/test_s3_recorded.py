"""S3: real Azure replies (recorded Sat 17 Oct 2026, `tests/fixtures/s3/`) to three team-written
messages, replayed through the validation, the merge, clarification and the rules engine. No live
model, no patient data."""

import json
from datetime import timedelta
from pathlib import Path

import pytest
from factories import T0, checkin

from hlidac import assessment, clarification, extraction, wizard
from hlidac.ctcae import read_mapping
from hlidac.plans import load_plans
from hlidac.schemas import InboundMessage, PatientContext

FIXTURE = Path(__file__).parent / "fixtures" / "s3" / "recorded-replies.json"
RECORDINGS = json.loads(FIXTURE.read_text(encoding="utf-8"))["recordings"]
PLAN = next(iter(load_plans().values()))
MAPPING = read_mapping()
RULES = assessment.read_rules()
ATTRS = clarification.grading_attributes(MAPPING)
# Invented context: a SACT dose 10 days ago (demo scenario 2).
CONTEXT = PatientContext(
    patient_id="p1", last_sact_dose_at=T0 - timedelta(days=10), source_refs=["TEST"], read_at=T0
)


def run(recording):
    message = InboundMessage(
        message_id="m1", checkin_id="c1", patient_id="p1", session_account_id="a1",
        reported_by="patient", received_at=T0, kind="text", text=recording["message"],
    )  # fmt: skip
    raw = json.dumps(recording["reply"], ensure_ascii=False)
    result = extraction.validate_reply(raw, PLAN, "m1", [recording["message"], recording["sent"]])
    state = checkin(
        messages=[message],
        findings=extraction.merge([], result, PLAN),
        extracted_message_ids=["m1"],
    )
    return result, state


def assess(state):
    return assessment.assess(
        assessment_id="as1", checkin_id="c1", patient_id="p1", findings=state.findings,
        plan=PLAN, mapping=MAPPING, rules=RULES, context=CONTEXT, now=T0,
    )  # fmt: skip


def label(recording):
    return f"{recording['scenario']}-{recording['deployment']}-run{recording['run']}"


@pytest.mark.parametrize("recording", RECORDINGS, ids=label)
def test_every_recorded_reply_is_valid_and_quotes_verbatim(recording):
    result, _ = run(recording)
    for finding in result.findings:
        assert finding.source.quote.casefold() in recording["message"].casefold()


@pytest.mark.parametrize("recording", RECORDINGS, ids=label)
def test_the_rules_decide_the_same_on_every_model_and_run(recording):
    _, state = run(recording)
    found = {f.link_id: (f.state, f.code) for f in state.findings}
    asked = [i.link_id for i in clarification.questions(PLAN, state, ATTRS)]
    if recording["scenario"] == "mild":
        assert found["nausea"] == ("present", "yes")
        assert found["nausea_intake"] == ("present", "intake_usual")
        assert found["fever"] == ("absent", "no") and found["chills"] == ("absent", "no")
        # quoted red-flag denials are confirmed in the closing summary before they count (G5, D58)
        assert "fever" not in asked and "chills" not in asked
        assert {"fever", "chills"} <= {f.link_id for f in wizard.to_confirm(PLAN, state)}
    elif recording["scenario"] == "fever":
        assert found["fever"] == ("present", "yes")
        temperature = next(f for f in state.findings if f.link_id == "temperature")
        assert temperature.value.value == 38.6
        # urgent at once, before any confirmation or question
        assert assess(state).status == "urgent"
    else:  # ambiguous: "zvracela … nevím, jestli mám teplotu"
        assert found["vomiting"] == ("present", "yes")
        assert found["fever"][0] == "unknown"  # "Nevím" is never absent
        assert "nausea_intake" not in found  # nausea itself wasn't said: asked, not guessed
        # one targeted question about what was reported, besides the red flags
        assert "vomiting_episodes" in asked and "fever" in asked
        assert assess(state).status != "no_escalation"
