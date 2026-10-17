"""The rules engine's verdict on a check-in, with everything needed to replay it."""

from typing import Literal

from pydantic import AwareDatetime, model_validator

from hlidac.schemas._base import Id, LinkId, NonEmpty, Strict, Version
from hlidac.schemas.findings import Finding
from hlidac.schemas.grading import CtcaeGrade
from hlidac.schemas.plans import PatientContext

# Workflow urgency, not a diagnosis (D6).
AssessmentStatus = Literal["urgent", "routine_review", "insufficient_information", "no_escalation"]
RuleInput = str | int | float | bool | None


class TriggeredRule(Strict):
    rule_id: Id
    rule_version: Version
    outcome: AssessmentStatus
    inputs: dict[NonEmpty, RuleInput]
    reason: NonEmpty  # Czech, for clinicians; never shown to the patient


class Assessment(Strict):
    assessment_id: Id
    checkin_id: Id
    patient_id: Id
    status: AssessmentStatus
    # Snapshots of exactly what the engine saw, so the assessment can be replayed (principle 7).
    findings: list[Finding]
    context: PatientContext | None  # None if the hospital context couldn't be read
    grades: list[CtcaeGrade]
    triggered_rules: list[TriggeredRule]
    missing_link_ids: list[LinkId]  # required or red-flag items still not answered
    advice_card_id: Id | None = None
    advice_card_version: Version | None = None
    questionnaire_url: NonEmpty
    questionnaire_version: Version
    ruleset_version: Version
    mapping_version: Version
    assessed_at: AwareDatetime
    engine_version: Version

    @model_validator(mode="after")
    def _safe(self) -> "Assessment":
        outcomes = {rule.outcome for rule in self.triggered_rules}
        if ("urgent" in outcomes) != (self.status == "urgent"):
            raise ValueError("an urgent rule makes the assessment urgent, and only it does")
        if self.status == "no_escalation":
            if self.missing_link_ids:
                raise ValueError("missing items never end as no_escalation")
            if outcomes - {"no_escalation"}:
                raise ValueError("a rule that escalated can't end as no_escalation")
            if any(grade.needs_clinical_assessment for grade in self.grades):
                raise ValueError(
                    "a grade that needs clinical assessment can't end as no_escalation"
                )
        if self.advice_card_id is not None and self.status != "no_escalation":
            raise ValueError("advice cards only follow no_escalation; urgent overrides advice")
        if (self.advice_card_id is None) != (self.advice_card_version is None):
            raise ValueError("an advice card is referenced with its version")
        link_ids = [finding.link_id for finding in self.findings]
        if len(link_ids) != len(set(link_ids)):
            raise ValueError("at most one finding per item")
        if self.context is not None and self.context.patient_id != self.patient_id:
            raise ValueError("the context belongs to the assessed patient")
        if any(grade.status != "provisional" for grade in self.grades):
            raise ValueError("the engine works on provisional grades; clinicians confirm later")
        return self
