"""Provisional CTCAE grading from the approved mapping (WP3). Pure: no IRIS, network or clock.

The mapping (`data/ctcae/mapping-demo-v1.yaml`) says which answers support which CTCAE grade; the
official grade wording is read from the NCI file it names and quoted verbatim in each grade's basis.
`grade()` only evaluates the mapping. No model and no free text sets a grade (principle 1), a
missing or "Nevím" attribute gives "at least N, needs clinical assessment" and never a lower grade
(principle 2), and every grade stays `provisional` until a clinician confirms it (G8).

The conditions (`answer`, `value`, `no_value`, `all`) are shared with the UKONS rating rows that the
rules engine (`assessment.py`) reads from the same mapping, so both use `matches()`.
"""

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import Field, model_validator

from hlidac.content import DATA_DIR, read_yaml
from hlidac.schemas import DONT_KNOW, NO, YES, CtcaeGrade, Finding, MonitoringPlan, Strict
from hlidac.schemas._base import GradeValue, Id, LinkId, NonEmpty, Version

MAPPING_FILE = DATA_DIR / "ctcae" / "mapping-demo-v1.yaml"
DEMO_LABEL = "DEMO"
Rating = Literal["green", "amber", "red"]
Treatment = Literal["on_sact", "off_sact"]


# --- the mapping file ------------------------------------------------------------------------


class AnswerIs(Strict):
    link_id: LinkId
    codes: Annotated[list[Id], Field(min_length=1)]


class ValueIs(Strict):
    link_id: LinkId
    unit: NonEmpty
    gt: float | None = None
    ge: float | None = None
    lt: float | None = None
    le: float | None = None

    @model_validator(mode="after")
    def _bounded(self) -> "ValueIs":
        if self.gt is not None and self.ge is not None:
            raise ValueError("a lower bound is gt or ge, not both")
        if self.lt is not None and self.le is not None:
            raise ValueError("an upper bound is lt or le, not both")
        if all(b is None for b in (self.gt, self.ge, self.lt, self.le)):
            raise ValueError("a value condition has at least one bound")
        return self

    def holds(self, number: float) -> bool:
        return (
            (self.gt is None or number > self.gt)
            and (self.ge is None or number >= self.ge)
            and (self.lt is None or number < self.lt)
            and (self.le is None or number <= self.le)
        )


class Condition(Strict):
    """Exactly one of: `answer`, `value`, `no_value`, `all`."""

    answer: AnswerIs | None = None
    value: ValueIs | None = None
    no_value: LinkId | None = None
    all_: Annotated[list["Condition"], Field(min_length=1)] | None = Field(
        default=None, alias="all"
    )

    @model_validator(mode="after")
    def _one_kind(self) -> "Condition":
        if sum(x is not None for x in (self.answer, self.value, self.no_value, self.all_)) != 1:
            raise ValueError("a condition is exactly one of answer, value, no_value, all")
        return self


class Criterion(Strict):
    term: NonEmpty
    grade: GradeValue


class Proposal(Strict):
    when: Condition
    grade: GradeValue | None = None
    at_least: GradeValue | None = None
    criterion: Criterion
    why_at_least: NonEmpty | None = None
    note: NonEmpty | None = None

    @model_validator(mode="after")
    def _exact_or_at_least(self) -> "Proposal":
        if (self.grade is None) == (self.at_least is None):
            raise ValueError("a proposal is either an exact grade or 'at least'")
        if self.at_least is not None and self.why_at_least is None:
            raise ValueError("an 'at least' proposal says why")
        if self.criterion.grade != (self.grade or self.at_least):
            raise ValueError("the cited criterion is the proposed grade's")
        return self

    @property
    def number(self) -> int:
        return self.grade or self.at_least or 0


class TermMapping(Strict):
    mapping_id: Id
    ctcae_term: NonEmpty
    meddra_code: NonEmpty
    link_id: LinkId
    proposals: Annotated[list[Proposal], Field(min_length=1)]
    not_graded: NonEmpty | None = None


class UkonsBand(Strict):
    when: Condition
    rating: Rating
    ukons_grade: Annotated[int, Field(ge=0, le=5)] | None = None
    ours: bool = False  # our reading where UKONS is silent (needs Motol's approval)


class UkonsRow(Strict):
    """One row of the UKONS assessment tool. Cited by name, grade and page only (licence, D32)."""

    ukons_symptom: NonEmpty
    page: int
    treatment: Treatment | None = None  # None: always applies
    applies_when: NonEmpty | None = None
    bands: Annotated[list[UkonsBand], Field(min_length=1)]
    notes: NonEmpty | None = None


class SourceRef(Strict):
    version: Version
    file: NonEmpty


class QuestionnaireRef(Strict):
    url: NonEmpty
    version: Version


class ReadingRule(Strict):
    text: NonEmpty
    source: NonEmpty


class TermDefinition(Strict):
    """The official wording of one CTCAE term, read from the NCI file the mapping names."""

    meddra_code: NonEmpty
    grades: dict[str, NonEmpty]  # "1".."5" -> verbatim text; "-" means no such grade


class CtcaeMapping(Strict):
    content_id: Id
    version: Version
    approval: NonEmpty  # "DEMO — čeká na schválení FN Motol" until a clinician approves
    approved_by: Id | None
    questionnaire: QuestionnaireRef
    # The illness-group questionnaires (D50) that reuse the core items verbatim.
    also_questionnaires: list[QuestionnaireRef] = Field(default_factory=list)
    ctcae: SourceRef
    ukons: SourceRef
    reading_rules: list[ReadingRule]
    mappings: Annotated[list[TermMapping], Field(min_length=1)]
    ukons_ratings: list[UkonsRow]
    definitions: dict[NonEmpty, TermDefinition]  # added by `load_mapping`, not in the file

    @model_validator(mode="after")
    def _consistent(self) -> "CtcaeMapping":
        if self.approved_by is None and DEMO_LABEL not in self.approval:
            raise ValueError("an unapproved mapping is labelled DEMO")
        ids = [m.mapping_id for m in self.mappings]
        links = [m.link_id for m in self.mappings]
        if len(ids) != len(set(ids)) or len(links) != len(set(links)):
            raise ValueError("mapping ids and linked items are unique")
        for term in self.mappings:
            definition = self.definitions.get(term.ctcae_term)
            if definition is None or definition.meddra_code != term.meddra_code:
                raise ValueError(f"{term.mapping_id}: not the official CTCAE term and code")
            for proposal in term.proposals:
                if proposal.criterion.term != term.ctcae_term:
                    raise ValueError(f"{term.mapping_id}: a criterion of another term")
                text = definition.grades.get(str(proposal.criterion.grade), "-")
                if text.strip() == "-":
                    raise ValueError(f"{term.mapping_id}: CTCAE has no grade {proposal.number}")
        return self


def read_mapping(path: Path = MAPPING_FILE) -> CtcaeMapping:
    """The mapping with the official wording of its terms. Raises ValueError if anything is off."""
    try:
        raw = read_yaml(path)
        ctcae_file = path.parent / raw["ctcae"]["file"]
        official = read_yaml(ctcae_file)
        if str(official["ctcae_version"]) != str(raw["ctcae"]["version"]):
            raise ValueError(f"{ctcae_file.name} is not CTCAE v{raw['ctcae']['version']}")
        wanted = {m["ctcae_term"] for m in raw["mappings"]}
        raw["definitions"] = {
            t["term"]: {"meddra_code": t["meddra_code"], "grades": t["grades"]}
            for t in official["terms"]
            if t["term"] in wanted
        }
        return CtcaeMapping.model_validate(raw)
    except (ValueError, KeyError, TypeError, OSError, yaml.YAMLError) as error:
        raise ValueError(f"{path.name}: {error}") from error


def check_against_plan(mapping: CtcaeMapping, plan: MonitoringPlan) -> None:
    """Raise ValueError unless every item, answer code and unit the mapping reads exists in the
    plan's Questionnaire, so a typo can't silently stop a condition from ever matching."""
    refs = [mapping.questionnaire, *mapping.also_questionnaires]
    if (plan.questionnaire_url, plan.questionnaire_version) not in {(r.url, r.version) for r in refs}:
        raise ValueError("the mapping is for another Questionnaire version")
    items = {item.link_id: item for item in plan.items}
    for term in mapping.mappings:
        item = items.get(term.link_id)
        if item is None or item.ctcae_term != term.ctcae_term:
            raise ValueError(f"{term.mapping_id}: the item {term.link_id} has another CTCAE term")
    rows = [p.when for t in mapping.mappings for p in t.proposals]
    rows += [b.when for row in mapping.ukons_ratings for b in row.bands]
    for when in rows:
        for leaf in _leaves(when):
            if leaf.answer is not None:
                item = items.get(leaf.answer.link_id)
                known = {o.code for o in item.options} if item else set()
                if not set(leaf.answer.codes) <= known:
                    raise ValueError(f"{leaf.answer.link_id}: an answer code the item lacks")
            elif leaf.value is not None:
                item = items.get(leaf.value.link_id)
                if item is None or item.type != "quantity" or item.unit != leaf.value.unit:
                    raise ValueError(f"{leaf.value.link_id}: not a quantity item in that unit")
            elif leaf.no_value not in items:
                raise ValueError(f"{leaf.no_value}: no such item")


# --- evaluating conditions -------------------------------------------------------------------


def _leaves(condition: Condition) -> list[Condition]:
    if condition.all_ is None:
        return [condition]
    return [leaf for sub in condition.all_ for leaf in _leaves(sub)]


def condition_links(condition: Condition) -> list[str]:
    """The items a condition reads, in order, without repeats."""
    links: list[str] = []
    for leaf in _leaves(condition):
        link = (
            leaf.answer.link_id
            if leaf.answer
            else leaf.value.link_id
            if leaf.value
            else leaf.no_value
        )
        if link is not None and link not in links:
            links.append(link)
    return links


def index_findings(findings: Sequence[Finding]) -> dict[str, Finding]:
    return {f.link_id: f for f in findings}


def is_usable(finding: Finding | None) -> bool:
    """Whether the item has an answer that can close or settle it. Not usable: nothing said,
    "Nevím", a statement with no chosen option, and a number the patient hasn't confirmed yet (G5:
    such a number may raise a grade or rating, but never closes anything)."""
    return (
        finding is not None
        and finding.state in ("present", "absent")
        and (finding.code is not None or finding.value is not None)
        and finding.code != DONT_KNOW
        and (finding.value is None or finding.confirmed)
    )


def _chose(finding: Finding | None, codes: Sequence[str]) -> bool:
    if finding is None or finding.state in ("not_mentioned", "unknown"):
        return False
    if finding.code is not None:
        return finding.code in codes
    # A quoted statement has no option code: "I have a fever" is yes, a quoted denial is no.
    return (finding.state == "present" and YES in codes) or (
        finding.state == "absent" and NO in codes
    )


def matches(condition: Condition, findings: Mapping[str, Finding]) -> bool:
    """Whether the findings satisfy the condition. A number counts as soon as it is typed."""
    if condition.all_ is not None:
        return all(matches(sub, findings) for sub in condition.all_)
    if condition.answer is not None:
        return _chose(findings.get(condition.answer.link_id), condition.answer.codes)
    if condition.value is not None:
        found = findings.get(condition.value.link_id)
        return (
            found is not None
            and found.state == "present"
            and found.value is not None
            and found.value.unit == condition.value.unit
            and condition.value.holds(found.value.value)
        )
    return not is_usable(findings.get(condition.no_value or ""))


def describe(link_id: str, findings: Mapping[str, Finding]) -> str:
    """One line on what the patient said for an item, for a clinician's basis."""
    found = findings.get(link_id)
    if found is None or found.state == "not_mentioned":
        return f"{link_id}: not answered"
    if found.value is not None:
        text = f"{link_id}: {found.value.value:g} {found.value.unit}"
        return text if found.confirmed else f"{text} (not yet confirmed)"
    return f"{link_id}: {found.code or found.state}"


# --- grading ---------------------------------------------------------------------------------


def _grade_id(checkin_id: str, mapping: CtcaeMapping, term: TermMapping) -> str:
    key = f"{checkin_id}|{mapping.content_id}|{mapping.version}|{term.mapping_id}"
    return f"grade-{hashlib.sha256(key.encode()).hexdigest()[:20]}"


def _basis(
    mapping: CtcaeMapping, term: TermMapping, proposal: Proposal, findings: Mapping[str, Finding]
) -> list[str]:
    wording = mapping.definitions[term.ctcae_term].grades[str(proposal.criterion.grade)]
    head = f"CTCAE v{mapping.ctcae.version} {term.ctcae_term} grade {proposal.number} (NCI)"
    basis = [f"{head}: {wording}"]
    basis += [f"Reported: {describe(link, findings)}" for link in condition_links(proposal.when)]
    if proposal.why_at_least:
        basis.append(f"At least, not exact: {proposal.why_at_least}")
    if proposal.note:
        basis.append(proposal.note)
    basis.append(f"Mapping {mapping.content_id} v{mapping.version} ({mapping.approval})")
    return basis


def grade(findings: Sequence[Finding], mapping: CtcaeMapping, checkin_id: str) -> list[CtcaeGrade]:
    """The provisional grade for each mapped term the findings support, in mapping order.

    Of several matching proposals the highest number wins, and "at least N" beats an exact N (it
    asks for clinical assessment). A term the findings don't support gets no grade; that is not a
    finding of "no symptom", and the rules engine handles unanswered and "Nevím" items separately.
    """
    found = index_findings(findings)
    grades: list[CtcaeGrade] = []
    for term in mapping.mappings:
        hits = [p for p in term.proposals if matches(p.when, found)]
        if not hits:
            continue
        best = max(hits, key=lambda p: (p.number, p.at_least is not None))
        grades.append(
            CtcaeGrade(
                grade_id=_grade_id(checkin_id, mapping, term),
                link_id=term.link_id,
                ctcae_term=term.ctcae_term,
                ctcae_version=mapping.ctcae.version,
                grade=best.grade,
                at_least=best.at_least,
                needs_clinical_assessment=best.at_least is not None,
                basis=_basis(mapping, term, best, found),
                mapping_id=term.mapping_id,
                mapping_version=mapping.version,
            )
        )
    return grades
