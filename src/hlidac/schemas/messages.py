"""What the patient sends: text, a voice recording or a button answer, and its transcript."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from hlidac.schemas._base import TYPED_VALUE, Id, LinkId, NonEmpty, Strict
from hlidac.schemas.findings import Quantity

ReportedBy = Literal["patient", "caregiver"]
MessageKind = Literal["text", "voice", "answer"]
TranscriptReview = Literal["pending", "confirmed", "corrected", "rejected"]


class ButtonAnswer(Strict):
    """A large-button answer to one Questionnaire item; `code` may be DONT_KNOW. A number typed
    into a quantity item has the code TYPED_VALUE and carries its `value`."""

    link_id: LinkId
    code: Id
    value: Quantity | None = None

    @model_validator(mode="after")
    def _value_only_when_typed(self) -> "ButtonAnswer":
        if (self.code == TYPED_VALUE) != (self.value is not None):
            raise ValueError("a typed answer carries its value, and only a typed answer does")
        return self


class InboundMessage(Strict):
    message_id: Id
    checkin_id: Id
    patient_id: Id
    session_account_id: Id
    reported_by: ReportedBy
    received_at: AwareDatetime
    kind: MessageKind
    text: NonEmpty | None = None
    audio_ref: Id | None = None
    answer: ButtonAnswer | None = None

    @model_validator(mode="after")
    def _payload_matches_kind(self) -> "InboundMessage":
        payloads = {
            "text": self.text is not None,
            "voice": self.audio_ref is not None,
            "answer": self.answer is not None,
        }
        if [kind for kind, present in payloads.items() if present] != [self.kind]:
            raise ValueError(f"a {self.kind} message carries exactly its own payload")
        return self


class Transcript(Strict):
    """Local Whisper output. It can raise urgency at once, closes an item only when confirmed."""

    message_id: Id
    text: str
    model: NonEmpty
    model_version: NonEmpty | None = None
    confidence: Annotated[float, Field(ge=0, le=1)] | None = None
    review: TranscriptReview = "pending"
    corrected_text: NonEmpty | None = None

    @model_validator(mode="after")
    def _correction_only_when_corrected(self) -> "Transcript":
        if (self.review == "corrected") != (self.corrected_text is not None):
            raise ValueError(
                "corrected_text is set exactly when the patient corrected the transcript"
            )
        return self
