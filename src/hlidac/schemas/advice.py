"""Approved advice cards. The app shows their text as written; nothing rephrases it."""

from datetime import date
from typing import Annotated

from pydantic import Field

from hlidac.schemas._base import GradeValue, Id, LinkId, NonEmpty, Strict, Version


class CardCondition(Strict):
    link_id: LinkId
    grades: Annotated[list[GradeValue], Field(min_length=1)]


class AdviceCard(Strict):
    card_id: Id
    version: Version
    text_cs: NonEmpty
    easy_read_cs: NonEmpty | None = None
    follow_up_cs: NonEmpty | None = None
    applies_when: Annotated[list[CardCondition], Field(min_length=1)]
    excluded_if_present: list[LinkId] = Field(default_factory=list)
    required_link_ids: list[LinkId] = Field(default_factory=list)
    source_url: NonEmpty
    approved_by: Id | None = None  # None in the demo: "DEMO — čeká na schválení FN Motol"
    review_date: date | None = None
