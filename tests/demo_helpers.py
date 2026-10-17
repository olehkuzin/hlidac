"""Findings and contexts for the engine tests, built the way the wizard builds them. Invented."""

from datetime import timedelta

from factories import T0

from hlidac import wizard
from hlidac.assessment import read_rules
from hlidac.ctcae import read_mapping
from hlidac.plans import load_plans
from hlidac.schemas import Finding, MonitoringPlan, PatientContext, QuoteSource

PLAN: MonitoringPlan = next(iter(load_plans().values()))
MAPPING = read_mapping()
RULES = read_rules()
NOW = T0
ALL_CLEAR = {
    "fever": "no",
    "chills": "no",
    "unwell": "no",
    "nausea": "no",
    "vomiting": "no",
    "diarrhoea": "no",
    "diarrhoea_blood": "no",
}


def finding(link_id: str, answer: str | float, confirmed: bool = True) -> Finding:
    """A button answer to the demo Questionnaire: an option code, or a typed number."""
    item = PLAN.item(link_id)
    if isinstance(answer, str):
        built = wizard.finding_from_answer(wizard.button_answer(item, answer), "m1")
    else:
        built = wizard.finding_from_answer(
            wizard.button_answer(item, "typed_value", float(answer)), "m1"
        )
    return built.evolve(confirmed=confirmed)


def findings(base: dict | None = None, /, **answers: str | float) -> list[Finding]:
    """`ALL_CLEAR` (or `base`) plus these answers; a value of None removes the item."""
    merged = {**(ALL_CLEAR if base is None else base), **answers}
    return [finding(link, answer) for link, answer in merged.items() if answer is not None]


def quoted_text(link_id: str = "other", quote: str = "bolí mě záda") -> Finding:
    source = QuoteSource(kind="quote", message_id="m9", quote=quote)
    return Finding(link_id=link_id, state="present", source=source, extractor_version="extract-v1")


def context(
    days_since_dose: int | None = 10, immunocompromised: bool | None = None
) -> PatientContext:
    dose = None if days_since_dose is None else NOW - timedelta(days=days_since_dose)
    return PatientContext(
        patient_id="p1",
        last_sact_dose_at=dose,
        immunocompromised=immunocompromised,
        source_refs=["MedicationAdministration/test"],
        read_at=NOW,
    )

