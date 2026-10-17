"""Findings: what the patient reported per Questionnaire item, with the evidence behind it."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from hlidac.schemas._base import (
    DONT_KNOW,
    TYPED_VALUE,
    Id,
    LinkId,
    NonEmpty,
    Scalar,
    Strict,
    Version,
)

# `absent` needs an explicit denial; `not_mentioned` and `unknown` never count as absent.
FindingState = Literal["present", "absent", "not_mentioned", "unknown"]


class Quantity(Strict):
    value: float
    unit: NonEmpty  # UCUM, e.g. "Cel"


class QuoteSource(Strict):
    """A verbatim quote from a free-text or transcribed message (guard G2)."""

    kind: Literal["quote"]
    message_id: Id
    quote: NonEmpty


class AnswerSource(Strict):
    """A button answer, or a number typed into a quantity item (`code` TYPED_VALUE + `value`)."""

    kind: Literal["answer"]
    message_id: Id
    link_id: LinkId
    code: Id
    value: Quantity | None = None

    @model_validator(mode="after")
    def _value_only_when_typed(self) -> "AnswerSource":
        if (self.code == TYPED_VALUE) != (self.value is not None):
            raise ValueError("a typed answer carries its value, and only a typed answer does")
        return self


class MessageSource(Strict):
    """A whole free-text message that no model has read (extraction off or failed): kept on the
    Questionnaire's free-text item, so the rules send it to a nurse to read (principle 2)."""

    kind: Literal["message"]
    message_id: Id


Source = Annotated[QuoteSource | AnswerSource | MessageSource, Field(discriminator="kind")]


class Finding(Strict):
    link_id: LinkId
    state: FindingState
    value: Quantity | None = None
    code: Id | None = None  # the chosen answer option, if any
    # Grading attributes, named as in the CTCAE mapping (e.g. episodes_per_day).
    attributes: dict[LinkId, Scalar] = Field(default_factory=dict)
    onset_text: NonEmpty | None = None
    source: Source | None = None
    extractor_version: Version | None = None  # set when the model extracted it from a quote
    confirmed: bool = False

    @model_validator(mode="after")
    def _evidence_matches_state(self) -> "Finding":
        if self.state == "not_mentioned":
            if self.source or self.value or self.code or self.attributes or self.confirmed:
                raise ValueError("a not_mentioned finding carries no source, value or attributes")
            return self
        if self.source is None:
            raise ValueError(f"a {self.state} finding needs a quote or a button answer")
        if self.state == "unknown" and (self.value or self.attributes):
            raise ValueError("an unknown finding carries no value or attributes")
        if isinstance(self.source, MessageSource):
            if self.state != "present" or self.value or self.code or self.attributes:
                raise ValueError("an unread message is a present finding with no value or code")
            if self.extractor_version is not None or self.confirmed:
                raise ValueError("an unread message is neither extracted nor confirmed")
            return self
        if isinstance(self.source, AnswerSource):
            if self.source.link_id != self.link_id:
                raise ValueError("the button answer belongs to another item")
            if self.code != self.source.code:
                raise ValueError("the finding's code must be the button's code")
            if self.value != self.source.value:
                raise ValueError("the finding's value must be the typed value")
            if self.extractor_version is not None:
                raise ValueError("a button answer is not extracted by a model")
        elif self.extractor_version is None:
            raise ValueError("a quoted finding records the extractor version")
        if (self.code == DONT_KNOW) != (self.state == "unknown" and self.code is not None):
            raise ValueError('"Nevím" always means unknown')
        return self


class ExtractionResult(Strict):
    """The validated result of one extraction; at most one finding per item."""

    findings: list[Finding]
    contradictions: list[NonEmpty] = Field(default_factory=list)
    uncertainties: list[NonEmpty] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_finding_per_item(self) -> "ExtractionResult":
        link_ids = [finding.link_id for finding in self.findings]
        if len(link_ids) != len(set(link_ids)):
            raise ValueError("at most one finding per Questionnaire item")
        return self
