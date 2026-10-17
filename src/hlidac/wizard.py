"""The check-in wizard (WP9): which Questionnaire item comes next, and what an answer records.

Pure: no store, network or clock. The button path walks the patient's Questionnaire in order, one
enabled choice or quantity item per screen, and skips items that already have an answer (nothing is asked twice).
After written text, targeted clarification (WP5, `clarification.py`) chooses the items instead;
the screens stay the same.

- A button answer is an explicit statement: "Ne" is `absent`, "Nevím" is `unknown`, any other
  option is `present`. A typed number is `present` with its value.
- Consequential answers (red-flag items and typed numbers, accessibility.md §3, guard G5) are shown
  back for confirmation. They are processed at once, so they can raise urgency, but an item only
  counts as answered once the patient confirmed it. The exception is the model's reading of the
  patient's own words (a quote): while the report is open it waits for that one button (D49).
"""

import math
import re

from hlidac.schemas import (
    DONT_KNOW,
    NO,
    TYPED_VALUE,
    AnswerSource,
    ButtonAnswer,
    Checkin,
    Finding,
    MonitoringPlan,
    PlanItem,
    Quantity,
    QuoteSource,
)

UNIT_LABELS = {"Cel": "°C"}  # UCUM code -> what the patient reads
MAX_TEXT_LENGTH = 2000
_NUMBER = re.compile(r"^[+-]?\d{1,6}([.,]\d{1,3})?$")


def is_enabled(item: PlanItem, findings: dict[str, Finding]) -> bool:
    """FHIR `enableWhen` with `enableBehavior` all: every condition's answer was given."""
    return all(
        (found := findings.get(c.link_id)) is not None and found.code == c.answer_code
        for c in item.enable_when
    )


def question_items(plan: MonitoringPlan, checkin: Checkin) -> list[PlanItem]:
    """The enabled choice and quantity items, in Questionnaire order. Free-text items are filled
    from the patient's own words (extraction, WP8), never asked one by one."""
    findings = {f.link_id: f for f in checkin.findings}
    return [i for i in plan.items if i.type != "text" and is_enabled(i, findings)]


def needs_confirmation(item: PlanItem, finding: Finding) -> bool:
    if finding.confirmed or finding.code == DONT_KNOW:
        return False
    return item.red_flag or item.type == "quantity"


def awaits_confirmation(item: PlanItem, finding: Finding) -> bool:
    """A consequential `present` answer the model read from the patient's words, not yet confirmed
    with a button (D49). Vague words ("cítím se špatně") must not open the 155 screen on the
    model's reading alone; the patient's "Ano, je to správně" does, and so does sending the report."""
    return (
        finding.state == "present"
        and isinstance(finding.source, QuoteSource)
        and needs_confirmation(item, finding)
    )


def awaits_measurement(plan: MonitoringPlan, checkin: Checkin, link_id: str) -> bool:
    """A reported symptom whose urgency depends on a number the patient wasn't asked yet (D54):
    "Mám teplotu" waits, while the report is open, for the thermometer reading (`measured_by`).
    A confirmed number ends the wait, and so does "Nevím" (then the symptom counts as reported
    without a reading, which keeps it red); sending the report ends it too."""
    measure = plan.measured_by.get(link_id)
    if measure is None or checkin.status != "in_progress":
        return False
    found = {f.link_id: f for f in checkin.findings}
    symptom = found.get(link_id)
    reading = found.get(measure)
    return (
        symptom is not None
        and symptom.state == "present"
        and (
            reading is None
            or reading.state == "not_mentioned"
            # a typed number counts once confirmed (G5); a high one raises urgency on its own
            or (reading.value is not None and not reading.confirmed)
        )
    )


def noted_reading(plan: MonitoringPlan, checkin: Checkin, link_id: str) -> Finding | None:
    """The confirmed reading of the symptom's measurement item (`measured_by`), if there is one."""
    measure = plan.measured_by.get(link_id)
    reading = next((f for f in checkin.findings if f.link_id == measure), None)
    if reading is None or reading.state != "present" or reading.value is None:
        return None
    return reading if reading.confirmed else None


def answered_by_reading(plan: MonitoringPlan, checkin: Checkin, link_id: str) -> bool:
    """A confirmed reading high enough to answer the symptom's own question (D55, e.g. 38.5 °C
    for "horečka?"). The reading itself drives the grade and the UKONS rating, so not asking
    can't lower urgency; a lower reading never stands in for "no"."""
    threshold = plan.answered_by_reading.get(link_id)
    reading = noted_reading(plan, checkin, link_id)
    return threshold is not None and reading is not None and reading.value.value >= threshold


def is_done(item: PlanItem, checkin: Checkin) -> bool:
    """Answered, so the chat moves on (D58). A consequential answer the patient gave (a button, a
    typed number) or a denial read from their words is confirmed once, with everything else, in
    "Rozuměl jsem správně?" before sending; until then `for_assessment` keeps an unconfirmed
    denial out, so it can't close a red flag. The model's reading of a reported red flag or
    number is still confirmed at once (D49: it decides whether the urgent screen shows now), and
    a red flag the patient's words left uncertain ("nevím, jestli mám teplotu") is asked with
    buttons (G3)."""
    finding = next((f for f in checkin.findings if f.link_id == item.link_id), None)
    if finding is None or finding.state == "not_mentioned":
        return False
    if isinstance(finding.source, QuoteSource) and finding.state != "absent":
        return not needs_confirmation(item, finding)
    return True


def to_confirm(plan: MonitoringPlan, checkin: Checkin) -> list[Finding]:
    """The answers the final "Rozuměl jsem správně?" confirms (D58): every answered, enabled
    question that still needs its confirmation, exactly as the summary lists them."""
    found = {f.link_id: f for f in checkin.findings}
    return [
        found[item.link_id]
        for item in question_items(plan, checkin)
        if item.link_id in found
        and is_done(item, checkin)
        and needs_confirmation(item, found[item.link_id])
    ]


def next_item(plan: MonitoringPlan, checkin: Checkin) -> PlanItem | None:
    """The first enabled item still open, or None when every enabled item is answered."""
    return next((i for i in question_items(plan, checkin) if not is_done(i, checkin)), None)


def previous_item(plan: MonitoringPlan, checkin: Checkin, link_id: str) -> PlanItem | None:
    items = question_items(plan, checkin)
    index = next((n for n, item in enumerate(items) if item.link_id == link_id), len(items))
    return items[index - 1] if index > 0 else None


def for_assessment(plan: MonitoringPlan, checkin: Checkin) -> Checkin:
    """The check-in as the pipeline sees it: an unconfirmed `absent` answer to a consequential
    item ("Ne" before "Ano, je to správně") is left out, so it can never close a red flag. An
    unconfirmed `present` button answer stays in: it may raise urgency at once (G5). An unconfirmed
    `present` quote is left out only while the report is open (D49): its confirmation is asked
    first (`clarification.questions`), and once the report is sent it counts as it is. A reported
    symptom that has a measurement item (`measured_by`, D54) waits the same way for its number,
    asked next. The deterministic red-flag phrase scan still reads the message text at once."""
    by_id = {item.link_id: item for item in plan.items}
    open_report = checkin.status == "in_progress"

    def held(f: Finding) -> bool:
        item = by_id.get(f.link_id)
        if item is None:
            return False
        if f.state == "absent":
            return needs_confirmation(item, f)
        return open_report and (
            awaits_confirmation(item, f) or awaits_measurement(plan, checkin, f.link_id)
        )

    return checkin.evolve(findings=[f for f in checkin.findings if not held(f)])


def unit_label(unit: str | None) -> str | None:
    return UNIT_LABELS.get(unit, unit) if unit else None


def answer_text(item: PlanItem, finding: Finding) -> str:
    """The answer as the patient gave it, in the approved option text (or the typed number)."""
    if finding.value is not None:
        number = f"{finding.value.value:g}".replace(".", ",")
        return f"{number} {unit_label(finding.value.unit)}"
    option = next((o for o in item.options if o.code == finding.code), None)
    if option is not None:
        return option.text_cs
    return "Nevím" if finding.code == DONT_KNOW else ""


def parse_number(raw: str) -> float | None:
    """A number as a patient types it: "38,4" or "38.4". None if it isn't one."""
    text = raw.strip().replace(" ", "")
    if not _NUMBER.fullmatch(text):
        return None
    value = float(text.replace(",", "."))
    return value if math.isfinite(value) else None


def button_answer(item: PlanItem, code: str, number: float | None = None) -> ButtonAnswer:
    """The answer to `item`. Raises ValueError for a code the item doesn't offer."""
    if code == TYPED_VALUE:
        if item.type != "quantity" or number is None or item.unit is None:
            raise ValueError("only a quantity item takes a typed number")
        value = Quantity(value=number, unit=item.unit)
        return ButtonAnswer(link_id=item.link_id, code=code, value=value)
    allowed = {o.code for o in item.options} | ({DONT_KNOW} if item.type != "choice" else set())
    if code not in allowed:
        raise ValueError("the item doesn't offer this answer")
    return ButtonAnswer(link_id=item.link_id, code=code)


def finding_from_answer(answer: ButtonAnswer, message_id: str) -> Finding:
    if answer.code == DONT_KNOW:
        state = "unknown"
    elif answer.code == NO:
        state = "absent"
    else:
        state = "present"
    source = AnswerSource(
        kind="answer",
        message_id=message_id,
        link_id=answer.link_id,
        code=answer.code,
        value=answer.value,
    )
    return Finding(
        link_id=answer.link_id, state=state, code=answer.code, value=answer.value, source=source
    )


def with_finding(findings: list[Finding], finding: Finding) -> list[Finding]:
    """The findings with `finding` replacing any earlier one for its item (the merged view)."""
    return [f for f in findings if f.link_id != finding.link_id] + [finding]
