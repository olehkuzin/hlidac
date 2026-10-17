"""The monitoring plan (from the FHIR Questionnaire), its assignment and the treatment context."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from hlidac.schemas._base import DONT_KNOW, GradeValue, Id, LinkId, NonEmpty, Strict, Version

# Yes/no questions are `choice` items with "Ano", "Ne" and "Nevím".
ItemType = Literal["choice", "quantity", "text"]


class AnswerOption(Strict):
    code: Id
    text_cs: NonEmpty  # approved Czech wording


class EnableWhen(Strict):
    link_id: LinkId
    answer_code: Id


class PlanItem(Strict):
    link_id: LinkId
    text_cs: NonEmpty  # approved Czech wording
    type: ItemType
    required: bool
    red_flag: bool = False
    ctcae_term: NonEmpty | None = None
    options: list[AnswerOption] = Field(default_factory=list)
    unit: NonEmpty | None = None  # UCUM, quantity items only
    enable_when: list[EnableWhen] = Field(default_factory=list)

    @model_validator(mode="after")
    def _answerable(self) -> "PlanItem":
        codes = [option.code for option in self.options]
        if len(codes) != len(set(codes)):
            raise ValueError("answer option codes are unique")
        if self.type == "choice" and DONT_KNOW not in codes:
            raise ValueError('every choice item offers "Nevím"')
        if self.type != "choice" and self.options:
            raise ValueError("only choice items have answer options")
        if (self.type == "quantity") != (self.unit is not None):
            raise ValueError("quantity items, and only they, have a unit")
        if self.red_flag and self.type != "choice":
            raise ValueError("red-flag items are choice items, so they can be asked with buttons")
        return self


class MonitoringPlan(Strict):
    questionnaire_url: NonEmpty
    questionnaire_version: Version
    title_cs: NonEmpty
    items: Annotated[list[PlanItem], Field(min_length=1)]
    rule_ids: list[Id]
    mapping_ids: list[Id]
    advice_card_ids: list[Id]
    max_clarification_turns: Annotated[int, Field(ge=1, le=10)]
    source_ref: NonEmpty
    approved_by: Id | None = None  # None in the demo: "DEMO — čeká na schválení FN Motol"
    # A reported symptom whose urgency depends on a measurement (D54): while the report is open,
    # its "yes" waits for that one number item (e.g. fever -> temperature), which is asked next.
    measured_by: dict[LinkId, LinkId] = Field(default_factory=dict)
    # D55: a confirmed reading at or above this (in the measurement item's unit) answers the
    # symptom's own question, which is then not asked (e.g. fever at 38.0 °C). Below it, the
    # question is still asked: a normal reading never stands in for "no".
    answered_by_reading: dict[LinkId, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _items_consistent(self) -> "MonitoringPlan":
        by_id = {item.link_id: item for item in self.items}
        if len(by_id) != len(self.items):
            raise ValueError("link ids are unique")
        for item in self.items:
            for condition in item.enable_when:
                target = by_id.get(condition.link_id)
                if target is None or condition.answer_code not in {o.code for o in target.options}:
                    raise ValueError(f"{item.link_id} depends on an unknown item or answer")
        for symptom, measure in self.measured_by.items():
            if (s := by_id.get(symptom)) is None or s.type != "choice":
                raise ValueError(f"measured_by: {symptom} is not a choice item")
            if (m := by_id.get(measure)) is None or m.type != "quantity" or m.enable_when:
                raise ValueError(f"measured_by: {measure} is not an always-asked quantity item")
        for symptom in self.answered_by_reading:
            if symptom not in self.measured_by:
                raise ValueError(f"answered_by_reading: {symptom} has no measured_by item")
        return self

    def item(self, link_id: str) -> PlanItem:
        return next(item for item in self.items if item.link_id == link_id)


class AccessibilityProfile(Strict):
    text_size: Literal["normal", "large", "x-large"] = "normal"
    high_contrast: bool = False
    screen_reader: bool = False
    voice_first: bool = False
    simplified: bool = False


class PatientPlan(Strict):
    patient_id: Id  # the hospital's FHIR Patient id
    questionnaire_url: NonEmpty
    questionnaire_version: Version
    care_team_id: Id
    accessibility: AccessibilityProfile = AccessibilityProfile()
    approved_by: Id | None = None
    active_from: AwareDatetime


class PatientContext(Strict):
    """Treatment context from the hospital record (read only). It feeds the rules, never a model."""

    patient_id: Id
    last_sact_dose_at: AwareDatetime | None = None
    regimen: NonEmpty | None = None
    # True or False only when the record says so; None means unknown (never assumed False).
    immunocompromised: bool | None = None
    baseline_grades: dict[NonEmpty, GradeValue] = Field(default_factory=dict)  # term -> grade
    source_refs: list[NonEmpty]  # FHIR references read, e.g. "MedicationAdministration/123"
    read_at: AwareDatetime
