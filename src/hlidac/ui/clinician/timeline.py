"""The symptom timeline of the case view (WP24): one patient's reports by CTCAE grade, as a table.

One row per check-in (its latest assessment; the case's own assessment too, if a later one exists),
one column per CTCAE term that any row graded. A cell shows the grade a clinician confirmed or
corrected, else the system's provisional one; an empty cell means no grade was proposed, never that
the symptom was absent. Each row says how it relates to the open case: this alert, a report grouped
into it (WP18), another alert, or none. Reads only the given patient's rows; the route checks access.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from hlidac import processing
from hlidac.schemas import Alert, Assessment, CtcaeGrade
from hlidac.store import Store

Link = Literal["this", "grouped", "other", "none"]


@dataclass(frozen=True)
class Row:
    assessment: Assessment
    at: datetime
    cells: tuple[CtcaeGrade | None, ...]  # one per `Timeline.terms`
    link: Link
    other_alert: Alert | None  # set when `link == "other"`


@dataclass(frozen=True)
class Timeline:
    terms: tuple[str, ...]
    rows: tuple[Row, ...]  # oldest first

    @property
    def grouped(self) -> tuple[Row, ...]:
        return tuple(r for r in self.rows if r.link == "grouped")


def _number(grade: CtcaeGrade) -> int:
    return grade.grade or grade.at_least or 0


def _effective(store: Store, assessment: Assessment) -> list[CtcaeGrade]:
    reviews = store.list_grade_reviews(assessment.assessment_id)
    out = []
    for grade in assessment.grades:
        mine = [r.grade for r in reviews if r.grade.grade_id == grade.grade_id]
        out.append(mine[-1] if mine else grade)
    return out


def build(store: Store, alert: Alert) -> Timeline:
    assessments = store.list_assessments(alert.patient_id)  # oldest first
    latest: dict[str, Assessment] = {}
    for assessment in assessments:
        latest[assessment.checkin_id] = assessment
    chosen = {a.assessment_id: a for a in latest.values()}
    own = next((a for a in assessments if a.assessment_id == alert.assessment_id), None)
    if own is not None:
        chosen[own.assessment_id] = own
    # Which alert carries each assessment: its own, or the one it joined (one read per alert).
    patient_alerts = store.list_alerts(patient_id=alert.patient_id)
    carrier: dict[str, Alert] = {}
    for other in patient_alerts:
        for joined in processing.grouped_ids(store, other):
            carrier[joined] = other
    for other in patient_alerts:
        carrier[other.assessment_id] = other
    grades = {a.assessment_id: _effective(store, a) for a in chosen.values()}
    terms = tuple(sorted({g.ctcae_term for found in grades.values() for g in found}))
    rows = []
    for assessment in sorted(chosen.values(), key=lambda a: a.assessed_at):
        best: dict[str, CtcaeGrade] = {}
        for grade in grades[assessment.assessment_id]:
            if grade.ctcae_term not in best or _number(grade) > _number(best[grade.ctcae_term]):
                best[grade.ctcae_term] = grade
        other = carrier.get(assessment.assessment_id)
        if assessment.assessment_id == alert.assessment_id:
            link: Link = "this"
            other = None
        elif other is not None and other.alert_id == alert.alert_id:
            link = "grouped"
            other = None
        else:
            link = "other" if other is not None else "none"
        cells = tuple(best.get(term) for term in terms)
        rows.append(Row(assessment, assessment.assessed_at, cells, link, other))
    return Timeline(terms, tuple(rows))
