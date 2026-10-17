"""Approved texts of the patient screens (`data/content/patient/`), loaded as written: the result
screens (`results-v1.json`) and the conversational check-in (`chat-v1.json`)."""

from functools import cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, model_validator

from hlidac.schemas import Strict
from hlidac.schemas._base import Id, NonEmpty, Version

CONTENT_DIR = Path(__file__).resolve().parents[4] / "data" / "content" / "patient"


class Screen(Strict):
    heading: NonEmpty
    paragraphs: list[NonEmpty]


class ResultTexts(Strict):
    content_id: Id
    version: Version
    approval: NonEmpty  # "DEMO — čeká na schválení FN Motol" until approved
    source_ref: NonEmpty
    not_monitored: NonEmpty
    team_notified: NonEmpty
    team_not_notified: NonEmpty
    no_plan: Screen
    statuses: dict[NonEmpty, Screen]
    emergency_number: NonEmpty
    non_voice_route: NonEmpty | None  # open decision D19; the urgent screen says so while None

    @model_validator(mode="after")
    def _urgent_screens(self) -> "ResultTexts":
        if not {"urgent", "urgent_no_line"} <= self.statuses.keys():
            raise ValueError("the urgent screen exists with and without a team line")
        return self

    def screen(self, key: str) -> Screen:
        return self.statuses[key]


class AudioTexts(Strict):
    """Audio mode (D59): what the device says around the approved texts, and the words that count
    as yes or no to a spoken confirmation (matched exactly on the first word, no model)."""

    turn_on: NonEmpty
    turn_on_hint: NonEmpty
    resume: NonEmpty
    turn_off: NonEmpty
    stop_listening: NonEmpty
    on: NonEmpty
    off: NonEmpty
    speaking: NonEmpty
    listening: NonEmpty
    listening_short: NonEmpty
    working: NonEmpty
    options: NonEmpty
    or_: NonEmpty = Field(alias="or")
    say_number: NonEmpty
    read_back: NonEmpty
    send_question: NonEmpty
    yes_no: NonEmpty
    again: NonEmpty
    unclear: NonEmpty
    send_report: NonEmpty
    add_more: NonEmpty
    no_voice: NonEmpty
    no_mic: NonEmpty
    stopped_urgent: NonEmpty
    screen_title: NonEmpty
    state_speaking: NonEmpty
    state_listening: NonEmpty
    state_working: NonEmpty
    close: NonEmpty
    close_full: NonEmpty
    yes: NonEmpty
    no: NonEmpty
    yes_words: list[NonEmpty]
    no_words: list[NonEmpty]

    @model_validator(mode="after")
    def _yes_and_no_differ(self) -> "AudioTexts":
        if set(self.yes_words) & set(self.no_words):
            raise ValueError("a word can't mean both yes and no")
        return self


class ChatTexts(Strict):
    """Fixed texts of the messenger frame (D53): speaker names, topics, the header's treatment
    day, the voice badge, the urgent card's extras. The questions always come from the
    Questionnaire and the chat's own fixed texts (D48)."""

    content_id: Id
    version: Version
    approval: NonEmpty
    source_ref: NonEmpty
    bot_name: NonEmpty
    patient_name: NonEmpty
    greeting_question: NonEmpty
    topics_intro: NonEmpty
    topics: dict[NonEmpty, NonEmpty]  # top-level Questionnaire linkId -> short topic
    context_day: NonEmpty  # "{day}" is the whole days since the last dose in the record
    context_today: NonEmpty
    confirm_intro: NonEmpty
    your_words: NonEmpty
    understood: NonEmpty
    # D55, per measurement item: "{value}" is the noted reading, "{options}" the question's options
    reading_noted: dict[NonEmpty, NonEmpty]
    reading_retry: dict[NonEmpty, NonEmpty]
    # D56: the chat's bridge lines, said before a question (approved text, never model output)
    ack_text: NonEmpty  # after the patient's own words
    ack_answer: Annotated[list[NonEmpty], Field(min_length=1)]  # after a reply, in turn
    follow_up: NonEmpty  # "{topic}": the reported symptom the question follows up
    asked_before: NonEmpty  # the symptom was reported in the previous sent report
    history_intro: NonEmpty  # "{topics}": what the previous sent report said, on the opening
    history_intro_plain: NonEmpty  # the same when none of it has a short topic name
    # D58: the extra questions of the patient's illness group ("{topics}"), on the opening
    group_topics: NonEmpty
    # D58: one "Rozuměl jsem správně?" at the end instead of a confirmation after each answer
    summary_question: NonEmpty
    summary_intro: NonEmpty
    summary_help: NonEmpty
    summary_yes: NonEmpty  # confirms every answer and sends the report
    summary_confirmed: NonEmpty  # the patient's side of the chat after summary_yes
    summary_add: NonEmpty  # the composer's label under the summary
    summary_fix: NonEmpty
    summary_other: NonEmpty  # the free-text item's quote
    ack_added: NonEmpty  # after the patient added something under the summary
    summary_labels: dict[NonEmpty, NonEmpty]  # core linkId -> short name; else the group's label
    voice_badge: NonEmpty
    voice_privacy: NonEmpty
    saved: NonEmpty
    care_team_label: NonEmpty
    care_team_phone: NonEmpty | None  # open decision D19: no button while None
    audio: AudioTexts

    @model_validator(mode="after")
    def _reading_texts_format(self) -> "ChatTexts":
        for template in [*self.reading_noted.values(), *self.reading_retry.values()]:
            template.format(value="37 °C", options="Ano, Ne")  # a stray {x} fails at load
        return self


@cache
def result_texts() -> ResultTexts:
    raw = (CONTENT_DIR / "results-v1.json").read_text(encoding="utf-8")
    return ResultTexts.model_validate_json(raw)


@cache
def chat_texts() -> ChatTexts:
    raw = (CONTENT_DIR / "chat-v1.json").read_text(encoding="utf-8")
    return ChatTexts.model_validate_json(raw)
