"""Provisional CTCAE grades. Only the approved mapping proposes them; only a clinician confirms."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from hlidac.schemas._base import GradeValue, Id, LinkId, NonEmpty, Strict, Version

GradeStatus = Literal["provisional", "confirmed", "corrected"]


class CtcaeGrade(Strict):
    grade_id: Id
    link_id: LinkId
    ctcae_term: NonEmpty
    ctcae_version: Version
    grade: GradeValue | None = None
    # "At least grade N" when a criterion isn't patient-reportable or an attribute is missing.
    at_least: GradeValue | None = None
    needs_clinical_assessment: bool = False
    basis: Annotated[list[NonEmpty], Field(min_length=1)]  # matched criteria and finding attributes
    mapping_id: Id
    mapping_version: Version
    status: GradeStatus = "provisional"
    confirmed_by: Id | None = None
    confirmed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "CtcaeGrade":
        if (self.grade is None) == (self.at_least is None):
            raise ValueError("a grade is either exact or 'at least'")
        if self.at_least is not None and not self.needs_clinical_assessment:
            raise ValueError("an 'at least' grade needs clinical assessment")
        reviewed = self.confirmed_by is not None or self.confirmed_at is not None
        if self.status == "provisional":
            if reviewed:
                raise ValueError("a provisional grade has no reviewer")
        else:
            if self.confirmed_by is None or self.confirmed_at is None:
                raise ValueError("a confirmed or corrected grade names the clinician and the time")
            if self.grade is None:
                raise ValueError("a clinician confirms an exact grade")
        return self


class GradeReview(Strict):
    """A clinician's confirmation or correction. The engine's grade stays in its assessment."""

    review_id: Id
    assessment_id: Id
    grade: CtcaeGrade
    note: NonEmpty | None = None

    @model_validator(mode="after")
    def _reviewed(self) -> "GradeReview":
        if self.grade.status == "provisional":
            raise ValueError("a review holds a confirmed or corrected grade")
        if self.grade.status == "corrected" and self.note is None:
            raise ValueError("a correction says why")
        return self
