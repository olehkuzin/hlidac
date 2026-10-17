"""The rules engine (WP4): grade → urgent → enough information → routine review. Pure.

`assess()` turns the findings of a check-in, the approved mapping, the versioned ruleset
(`data/plans/rules-demo-v1.yaml`) and the treatment context into an `Assessment`. The clock is
passed in (`now`); nothing here reads the system time, the network or IRIS. The same inputs always
give the same assessment, and `replay()` re-derives a stored one to prove it.

The LLM never gets here: findings come in already validated, and only these rules set the status
(principle 1). Uncertainty never lowers urgency (principle 2):
- `not_mentioned` and `unknown` are never read as `absent`; a missing required or red-flag item
  can't end as `no_escalation`;
- a number the patient typed counts as soon as it is typed, so it can raise urgency before it is
  confirmed, but it never closes anything;
- an urgent rule fires on whatever is known, however much unrelated information is missing.

Order of outcomes, most urgent first: urgent > routine_review > insufficient_information >
no_escalation. A routine-review reason therefore wins over missing information (the system-design
§6 order asks first, but the review reason is already known, so the nurse need not wait for it).
Advice cards (WP6) are chosen after this and only for `no_escalation`; this module never picks one.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from string import Formatter
from typing import Annotated, Literal

import yaml
from pydantic import Field, model_validator

from hlidac import ctcae, wizard
from hlidac.content import DATA_DIR, read_yaml
from hlidac.ctcae import CtcaeMapping, Rating, Treatment
from hlidac.schemas import (
    Assessment,
    AssessmentStatus,
    CtcaeGrade,
    Finding,
    MonitoringPlan,
    PatientContext,
    Strict,
    TriggeredRule,
)
from hlidac.schemas._base import Id, NonEmpty, Version

ENGINE_VERSION = "1.0"
RULES_FILE = DATA_DIR / "plans" / "rules-demo-v1.yaml"
DEMO_LABEL = "DEMO"

RuleKind = Literal[
    "ukons_red",
    "ukons_amber",
    "red_flag_present",
    "grade_needs_assessment",
    "free_text_reported",
    "missing_items",
    "group_symptom_reported",
]
SEVERITY: dict[str, int] = {
    "no_escalation": 0,
    "insufficient_information": 1,
    "routine_review": 2,
    "urgent": 3,
}
RATING_RANK: dict[str, int] = {"green": 0, "amber": 1, "red": 2}
# The placeholders each kind's Czech `reason_cs` may use.
REASON_FIELDS: dict[str, frozenset[str]] = {
    "ukons_red": frozenset({"symptom", "ukons_grade"}),
    "ukons_amber": frozenset({"count", "items", "hours"}),
    "red_flag_present": frozenset({"items"}),
    "grade_needs_assessment": frozenset({"items"}),
    "free_text_reported": frozenset({"items"}),
    "missing_items": frozenset({"items"}),
    "group_symptom_reported": frozenset({"items"}),
}


# --- the ruleset file ------------------------------------------------------------------------


class Rule(Strict):
    rule_id: Id
    version: Version
    kind: RuleKind
    outcome: AssessmentStatus
    source_ref: NonEmpty
    reason_cs: NonEmpty
    min_count: Annotated[int, Field(ge=1)] | None = None  # ukons_amber only
    max_count: Annotated[int, Field(ge=1)] | None = None
    review_within_hours: Annotated[int, Field(ge=1)] | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "Rule":
        used = {name for _, name, _, _ in Formatter().parse(self.reason_cs) if name}
        if not used <= REASON_FIELDS[self.kind]:
            raise ValueError(f"{self.rule_id}: reason_cs uses a placeholder its kind doesn't have")
        if (self.kind == "ukons_amber") != (self.min_count is not None):
            raise ValueError(f"{self.rule_id}: min_count belongs to ukons_amber, and only to it")
        if self.max_count is not None and (self.min_count or 1) > self.max_count:
            raise ValueError(f"{self.rule_id}: max_count is below min_count")
        if self.review_within_hours is not None and self.outcome != "routine_review":
            raise ValueError(f"{self.rule_id}: only a routine review has a deadline")
        if self.kind == "ukons_red" and self.outcome != "urgent":
            raise ValueError(f"{self.rule_id}: a red UKONS rating is urgent")
        if self.kind == "missing_items" and self.outcome != "insufficient_information":
            raise ValueError(f"{self.rule_id}: missing items are insufficient information")
        return self


class MappingRef(Strict):
    content_id: Id
    version: Version


class Ruleset(Strict):
    content_id: Id
    version: Version
    approval: NonEmpty  # "DEMO — čeká na schválení FN Motol" until a clinician approves
    approved_by: Id | None
    questionnaire: ctcae.QuestionnaireRef
    # The illness-group questionnaires (D50) the same rules apply to.
    also_questionnaires: list[ctcae.QuestionnaireRef] = Field(default_factory=list)
    mapping: MappingRef
    sact_window_days: Annotated[int, Field(ge=1)]
    sact_window_source: NonEmpty
    rules: Annotated[list[Rule], Field(min_length=1)]

    @model_validator(mode="after")
    def _consistent(self) -> "Ruleset":
        if self.approved_by is None and DEMO_LABEL not in self.approval:
            raise ValueError("an unapproved ruleset is labelled DEMO")
        ids = [rule.rule_id for rule in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("rule ids are unique")
        if {rule.kind for rule in self.rules} != set(REASON_FIELDS):
            raise ValueError("a ruleset holds every rule kind, so no safety rule is dropped")
        if not any(r.kind == "ukons_red" and r.outcome == "urgent" for r in self.rules):
            raise ValueError("a red UKONS rating is an urgent rule")
        return self


def read_rules(path: Path = RULES_FILE) -> Ruleset:
    """The versioned ruleset. Raises ValueError if the file is missing, malformed or unsafe."""
    try:
        return Ruleset.model_validate(read_yaml(path))
    except (ValueError, OSError, yaml.YAMLError) as error:
        raise ValueError(f"{path.name}: {error}") from error


def check_content(plan: MonitoringPlan, mapping: CtcaeMapping, rules: Ruleset) -> None:
    """Raise ValueError unless the plan, mapping and ruleset belong together."""
    ctcae.check_against_plan(mapping, plan)
    refs = [rules.questionnaire, *rules.also_questionnaires]
    if (plan.questionnaire_url, plan.questionnaire_version) not in {(r.url, r.version) for r in refs}:
        raise ValueError("the ruleset is for another Questionnaire version")
    if (rules.mapping.content_id, rules.mapping.version) != (mapping.content_id, mapping.version):
        raise ValueError("the ruleset is for another mapping version")
    if plan.rule_ids != [rule.rule_id for rule in rules.rules]:
        raise ValueError("the plan names other rules than the ruleset holds")
    if plan.mapping_ids != [term.mapping_id for term in mapping.mappings]:
        raise ValueError("the plan names other mappings than the mapping holds")


# --- UKONS ratings and treatment context -----------------------------------------------------


@dataclass(frozen=True)
class UkonsRating:
    symptom: str
    page: int
    rating: Rating
    ukons_grade: int | None
    ours: bool
    treatment: Treatment | None
    evidence: tuple[str, ...]


def is_off_sact(context: PatientContext | None, now: datetime, window_days: int) -> bool:
    """Positive evidence that the patient is outside the SACT window: the last dose is known, is
    older than the window, and the record says the patient is not immunocompromised. Anything
    unknown (no record, no dose, a dose date in the future, immunocompromised unknown) is False,
    so the stricter "on SACT" row applies."""
    if context is None or context.last_sact_dose_at is None:
        return False
    if context.immunocompromised is not False:
        return False
    return now - context.last_sact_dose_at > timedelta(days=window_days)


def days_since_dose(context: PatientContext | None, now: datetime) -> int | None:
    if context is None or context.last_sact_dose_at is None or context.last_sact_dose_at > now:
        return None
    return (now - context.last_sact_dose_at).days


def ukons_ratings(
    findings: Mapping[str, Finding], mapping: CtcaeMapping, off_sact: bool
) -> list[UkonsRating]:
    """One rating per UKONS row that has a matching band, in mapping order. Of several matching
    bands the worst rating wins (uncertainty never lowers urgency)."""
    ratings: list[UkonsRating] = []
    for row in mapping.ukons_ratings:
        if row.treatment is not None and (row.treatment == "off_sact") != off_sact:
            continue
        hits = [band for band in row.bands if ctcae.matches(band.when, findings)]
        if not hits:
            continue
        best = max(hits, key=lambda b: (RATING_RANK[b.rating], b.ukons_grade or 0))
        evidence = tuple(
            ctcae.describe(link, findings) for link in ctcae.condition_links(best.when)
        )
        ratings.append(
            UkonsRating(
                symptom=row.ukons_symptom,
                page=row.page,
                rating=best.rating,
                ukons_grade=best.ukons_grade,
                ours=best.ours,
                treatment=row.treatment,
                evidence=evidence,
            )
        )
    return ratings


# --- missing information ---------------------------------------------------------------------


def missing_link_ids(plan: MonitoringPlan, findings: Mapping[str, Finding]) -> list[str]:
    """Enabled required and red-flag items with no answer that settles them: nothing said,
    "Nevím", or a consequential "no" the patient hasn't confirmed (G5)."""
    missing: list[str] = []
    for item in plan.items:
        if item.type == "text" or not (item.required or item.red_flag):
            continue
        if not wizard.is_enabled(item, dict(findings)):
            continue
        found = findings.get(item.link_id)
        unsettled = found is None or found.state in ("not_mentioned", "unknown")
        if found is not None and found.state == "absent":
            unsettled = wizard.needs_confirmation(item, found)
        if unsettled:
            missing.append(item.link_id)
    return missing


# --- the rules -------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Facts:
    plan: MonitoringPlan
    findings: Mapping[str, Finding]
    grades: Sequence[CtcaeGrade]
    ratings: Sequence[UkonsRating]
    missing: Sequence[str]
    off_sact: bool
    sact_days: int | None
    graded: frozenset[str] = frozenset()  # link ids the CTCAE mapping grades


def _fire(
    rule: Rule, inputs: dict[str, str | int | float | bool | None], **reason: object
) -> TriggeredRule:
    text = rule.reason_cs.format(**{key: str(value) for key, value in reason.items()})
    return TriggeredRule(
        rule_id=rule.rule_id,
        rule_version=rule.version,
        outcome=rule.outcome,
        inputs=inputs,
        reason=text,
    )


def _ukons_red(rule: Rule, facts: _Facts) -> list[TriggeredRule]:
    fired = []
    for r in (r for r in facts.ratings if r.rating == "red"):
        inputs: dict[str, str | int | float | bool | None] = {
            "ukons_symptom": r.symptom,
            "ukons_page": r.page,
            "ukons_grade": r.ukons_grade,
            "reported": "; ".join(r.evidence),
            "our_reading": r.ours,
            "days_since_dose": facts.sact_days if r.treatment else None,
            "ukons_row_context": r.treatment,
        }
        grade = r.ukons_grade if r.ukons_grade is not None else "–"
        fired.append(_fire(rule, inputs, symptom=r.symptom, ukons_grade=grade))
    return fired


def _ukons_amber(rule: Rule, facts: _Facts) -> list[TriggeredRule]:
    amber = [r.symptom for r in facts.ratings if r.rating == "amber"]
    low, high = rule.min_count or 1, rule.max_count
    if len(amber) < low or (high is not None and len(amber) > high):
        return []
    inputs: dict[str, str | int | float | bool | None] = {
        "amber_count": len(amber),
        "symptoms": "; ".join(amber),
        "review_within_hours": rule.review_within_hours,
    }
    return [_fire(rule, inputs, count=len(amber), items="; ".join(amber),
                  hours=rule.review_within_hours or "")]  # fmt: skip


def _red_flag_present(rule: Rule, facts: _Facts) -> list[TriggeredRule]:
    present = [
        i.link_id
        for i in facts.plan.items
        if i.red_flag and (f := facts.findings.get(i.link_id)) is not None and f.state == "present"
    ]
    if not present:
        return []
    return [_fire(rule, {"items": ", ".join(present)}, items=", ".join(present))]


def _grade_needs_assessment(rule: Rule, facts: _Facts) -> list[TriggeredRule]:
    open_grades = [f"{g.ctcae_term} ≥ {g.at_least}" for g in facts.grades if g.at_least]
    if not open_grades:
        return []
    items = ", ".join(open_grades)
    return [_fire(rule, {"grades": items}, items=items)]


def _free_text_reported(rule: Rule, facts: _Facts) -> list[TriggeredRule]:
    said = [
        i.link_id
        for i in facts.plan.items
        if i.type == "text" and (f := facts.findings.get(i.link_id)) is not None
        and f.state == "present"
    ]  # fmt: skip
    if not said:
        return []
    return [_fire(rule, {"items": ", ".join(said)}, items=", ".join(said))]


def _missing_items(rule: Rule, facts: _Facts) -> list[TriggeredRule]:
    if not facts.missing:
        return []
    items = ", ".join(facts.missing)
    return [_fire(rule, {"items": items}, items=items)]


def _group_symptom_reported(rule: Rule, facts: _Facts) -> list[TriggeredRule]:
    """A "yes" to a top-level symptom item the CTCAE mapping doesn't grade and that isn't a red
    flag (the illness-group items, D50): the nurse reviews it, so an ungraded symptom never ends
    as no_escalation. "Nevím" or no answer is caught by missing_items instead."""
    said = [
        i.link_id
        for i in facts.plan.items
        if i.type == "choice" and not i.red_flag and not i.enable_when
        and i.link_id not in facts.graded and any(o.code == "yes" for o in i.options)
        and (f := facts.findings.get(i.link_id)) is not None and f.state == "present"
    ]  # fmt: skip
    if not said:
        return []
    return [_fire(rule, {"items": ", ".join(said)}, items=", ".join(said))]


_KINDS: dict[str, Callable[[Rule, _Facts], list[TriggeredRule]]] = {
    "ukons_red": _ukons_red,
    "ukons_amber": _ukons_amber,
    "red_flag_present": _red_flag_present,
    "grade_needs_assessment": _grade_needs_assessment,
    "free_text_reported": _free_text_reported,
    "missing_items": _missing_items,
    "group_symptom_reported": _group_symptom_reported,
}


# --- the assessment --------------------------------------------------------------------------


def assess(
    *,
    assessment_id: str,
    checkin_id: str,
    patient_id: str,
    findings: Sequence[Finding],
    plan: MonitoringPlan,
    mapping: CtcaeMapping,
    rules: Ruleset,
    context: PatientContext | None,
    now: datetime,
) -> Assessment:
    """Grade, apply the rules and return the verdict. Raises ValueError if the content doesn't fit
    together (a visible failure, never a quiet default). `context` is None if the hospital record
    couldn't be read; the stricter fever row then applies."""
    check_content(plan, mapping, rules)
    by_link = ctcae.index_findings(findings)
    off_sact = is_off_sact(context, now, rules.sact_window_days)
    facts = _Facts(
        plan=plan,
        findings=by_link,
        grades=ctcae.grade(findings, mapping, checkin_id),
        ratings=ukons_ratings(by_link, mapping, off_sact),
        missing=missing_link_ids(plan, by_link),
        off_sact=off_sact,
        sact_days=days_since_dose(context, now),
        graded=frozenset(term.link_id for term in mapping.mappings),
    )
    triggered = [t for rule in rules.rules for t in _KINDS[rule.kind](rule, facts)]
    status: AssessmentStatus = max(
        (t.outcome for t in triggered), key=SEVERITY.__getitem__, default="no_escalation"
    )
    return Assessment(
        assessment_id=assessment_id,
        checkin_id=checkin_id,
        patient_id=patient_id,
        status=status,
        findings=list(findings),
        context=context,
        grades=list(facts.grades),
        triggered_rules=triggered,
        missing_link_ids=list(facts.missing),
        questionnaire_url=plan.questionnaire_url,
        questionnaire_version=plan.questionnaire_version,
        ruleset_version=rules.version,
        mapping_version=mapping.version,
        assessed_at=now,
        engine_version=ENGINE_VERSION,
    )


def replay(
    assessment: Assessment, plan: MonitoringPlan, mapping: CtcaeMapping, rules: Ruleset
) -> Assessment:
    """Re-derive a stored assessment from the snapshots it holds (principle 7). Equal to the
    original if the engine, ruleset and mapping are the ones it recorded; raises ValueError if the
    versions on hand differ, because the result would not be a replay."""
    recorded = (assessment.ruleset_version, assessment.mapping_version, assessment.engine_version)
    if recorded != (rules.version, mapping.version, ENGINE_VERSION):
        raise ValueError("replay needs the ruleset, mapping and engine versions it recorded")
    return assess(
        assessment_id=assessment.assessment_id,
        checkin_id=assessment.checkin_id,
        patient_id=assessment.patient_id,
        findings=assessment.findings,
        plan=plan,
        mapping=mapping,
        rules=rules,
        context=assessment.context,
        now=assessment.assessed_at,
    )


# --- grouping --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Grouping:
    """How a new assessment relates to the patient's open ones (spec §9). `kind`:
    - `new`: no open assessment, so a new alert;
    - `grouped`: the same symptoms at the same or a lower grade and status, triggered by rules the
      open one triggered too, so it joins `with_id`;
    - `worsened`: a higher status, a higher grade, a new symptom or a newly triggered rule, so
      the alert is renewed."""

    kind: Literal["new", "grouped", "worsened"]
    with_id: str | None
    reasons: tuple[str, ...]


def _grade_numbers(assessment: Assessment) -> dict[str, int]:
    return {g.ctcae_term: g.grade or g.at_least or 0 for g in assessment.grades}


def group_with_open(current: Assessment, open_assessments: Sequence[Assessment]) -> Grouping:
    """Compare `current` with the patient's latest open assessment (the caller passes those whose
    alert or task is still open). It only groups; it never lowers a status."""
    mine = [a for a in open_assessments if a.patient_id == current.patient_id]
    if not mine:
        return Grouping("new", None, ())
    previous = max(mine, key=lambda a: a.assessed_at)
    reasons: list[str] = []
    if SEVERITY[current.status] > SEVERITY[previous.status]:
        reasons.append(f"status {previous.status} → {current.status}")
    before = _grade_numbers(previous)
    for term, number in _grade_numbers(current).items():
        if term not in before:
            reasons.append(f"{term}: new, grade {number}")
        elif number > before[term]:
            reasons.append(f"{term}: grade {before[term]} → {number}")
    earlier_rules = {rule.rule_id for rule in previous.triggered_rules}
    for rule_id in sorted({rule.rule_id for rule in current.triggered_rules} - earlier_rules):
        reasons.append(f"rule {rule_id}: new")
    return Grouping("worsened" if reasons else "grouped", previous.assessment_id, tuple(reasons))
