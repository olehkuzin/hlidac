"""One check-in: the conversation so far. Saved after every answer, so nothing is lost."""

from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from hlidac.schemas._base import Count, Id, LinkId, NonEmpty, Strict, Version
from hlidac.schemas.findings import Finding
from hlidac.schemas.messages import InboundMessage, ReportedBy, Transcript

# Urgency is not a check-in status: it lives in the Assessment.
CheckinStatus = Literal["in_progress", "completed", "processing_failed"]

# The full conversation in order (D46). The app's steps: a question, an answer shown back for
# confirmation, the result screen. The patient's: a written (or dictated) message, a button
# answer, a confirmation, sending the report.
ChatEvent = Literal["question", "confirmation", "result", "text", "answer", "confirmed", "sent"]
APP_EVENTS = frozenset({"question", "confirmation", "result"})


class ChatEntry(Strict):
    """One step of the conversation, exactly as shown or sent: approved texts on the app's side,
    the patient's own words or the approved option text on theirs."""

    at: AwareDatetime
    event: ChatEvent
    text: NonEmpty
    link_id: LinkId | None = None
    message_id: Id | None = None
    via: Literal["keyboard", "voice"] | None = None


class Checkin(Strict):
    checkin_id: Id
    patient_id: Id
    questionnaire_url: NonEmpty
    questionnaire_version: Version
    reported_by: ReportedBy
    started_at: AwareDatetime
    updated_at: AwareDatetime
    status: CheckinStatus = "in_progress"
    messages: list[InboundMessage] = Field(default_factory=list)
    transcripts: list[Transcript] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)  # the current merged view, one per item
    # Text messages the model extracted (WP8); with any, only the missing items are asked (WP5).
    extracted_message_ids: list[Id] = Field(default_factory=list)
    pending_link_id: LinkId | None = None  # the question on screen now
    clarification_turns: Count = 0
    failure: NonEmpty | None = None  # shown in Unresolved; never hidden
    history: list[ChatEntry] = Field(default_factory=list)  # every step in order (D46)

    @model_validator(mode="after")
    def _consistent(self) -> "Checkin":
        if self.updated_at < self.started_at:
            raise ValueError("updated_at can't be before started_at")
        if any(
            m.checkin_id != self.checkin_id or m.patient_id != self.patient_id
            for m in self.messages
        ):
            raise ValueError("every message belongs to this check-in and patient")
        texts = {m.message_id for m in self.messages if m.kind == "text"}
        if not set(self.extracted_message_ids) <= texts:
            raise ValueError("only this check-in's text messages are extracted")
        if len(self.extracted_message_ids) != len(set(self.extracted_message_ids)):
            raise ValueError("a message is extracted once")
        link_ids = [finding.link_id for finding in self.findings]
        if len(link_ids) != len(set(link_ids)):
            raise ValueError("at most one finding per item")
        ids = {m.message_id for m in self.messages}
        if any(e.message_id is not None and e.message_id not in ids for e in self.history):
            raise ValueError("the history refers only to this check-in's messages")
        if (self.status == "processing_failed") != (self.failure is not None):
            raise ValueError("a failed check-in says why, and only a failed one")
        return self
