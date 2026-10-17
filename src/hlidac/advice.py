"""Approved advice cards (WP6): loading `data/content/advice/*.yaml` and choosing one card.

Pure: no IRIS, network or clock. Only `load_card_files` and `load_cards` read files.

A card file holds the `AdviceCard` the app shows as written, plus its sources (URL, section and the
statements the text restates) and the approval label ("DEMO — čeká na schválení FN Motol" until a
clinician approves it). `check_cards` fails loudly when a card names an item the plan lacks.

`select_card` is the deterministic selection rule (feature-description §8, system-design §6 step 5):
- only a `no_escalation` assessment gets a card; urgent always overrides advice, and a routine
  review, missing information or a failure never gets one;
- every red-flag item of the plan must be explicitly denied (`absent`);
- every `applies_when` condition needs a present finding and an exact provisional CTCAE grade in its
  list (an "at least" grade or one that needs clinical assessment never matches);
- every `required_link_ids` item must be answered (`present` or `absent`);
- every `excluded_if_present` item must be explicitly denied: present, "Nevím" (`unknown`) or no
  answer (`not_mentioned`) excludes the card, so uncertainty never selects advice;
- only cards the plan permits are considered, in `card_id` order; the first eligible one wins.
No eligible card: None, and the result screen shows the status's policy text.
"""

import functools
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import Field, StringConstraints, model_validator

from hlidac.content import DATA_DIR, read_yaml
from hlidac.schemas import AdviceCard, Assessment, Finding, MonitoringPlan, Strict
from hlidac.schemas._base import NonEmpty

ADVICE_DIR = DATA_DIR / "content" / "advice"
DEMO_LABEL = "DEMO"

Url = Annotated[str, StringConstraints(pattern=r"^https://\S+$")]
CardKey = tuple[str, str]


class CardSource(Strict):
    """Where the card's text comes from: the public document, the exact section, and the
    statements in it that the Czech text restates."""

    url: Url
    section: NonEmpty
    supports: NonEmpty


class CardFile(Strict):
    """One `data/content/advice/<card_id>.yaml`."""

    approval: NonEmpty  # "DEMO — čeká na schválení FN Motol" until a clinician approves
    sources: Annotated[list[CardSource], Field(min_length=1)]
    card: AdviceCard

    @model_validator(mode="after")
    def _sourced_and_labelled(self) -> "CardFile":
        if self.card.approved_by is None and DEMO_LABEL not in self.approval:
            raise ValueError("an unapproved card is labelled DEMO")
        if self.card.source_url not in {source.url for source in self.sources}:
            raise ValueError("the card's source_url is one of its cited sources")
        return self


# --- files ---------------------------------------------------------------------------------


def load_card_files(directory: Path = ADVICE_DIR) -> list[CardFile]:
    """Every card file, sorted by name. Raises ValueError naming the file on any problem."""
    files: list[CardFile] = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            files.append(CardFile.model_validate(read_yaml(path)))
        except (ValueError, yaml.YAMLError) as error:
            raise ValueError(f"{path.name}: {error}") from error
    return files


def load_cards(directory: Path = ADVICE_DIR) -> dict[CardKey, AdviceCard]:
    """The cards by (card_id, version), as `Pipeline.advice_cards` expects. One version per card
    id (older versions live in git history), so the selection never has to choose between two."""
    cards: dict[CardKey, AdviceCard] = {}
    for file in load_card_files(directory):
        card = file.card
        if any(card_id == card.card_id for card_id, _ in cards):
            raise ValueError(f"advice card {card.card_id!r} is defined more than once")
        cards[(card.card_id, card.version)] = card
    return cards


@functools.cache
def _default_cards() -> dict[CardKey, AdviceCard]:
    return load_cards()


# --- content check -------------------------------------------------------------------------


def check_cards(plan: MonitoringPlan, cards: Mapping[CardKey, AdviceCard]) -> None:
    """Raise ValueError unless every card the plan permits exists and every card's items exist in
    the plan (a condition item also needs a CTCAE term, or it could never have a grade)."""
    ids = [card_id for card_id, _ in cards]
    repeated = sorted({card_id for card_id in ids if ids.count(card_id) > 1})
    if repeated:
        raise ValueError(f"one version per advice card, not several: {', '.join(repeated)}")
    missing = sorted(set(plan.advice_card_ids) - set(ids))
    if missing:
        raise ValueError(f"the plan permits unknown advice card(s): {', '.join(missing)}")
    items = {item.link_id: item for item in plan.items}
    for card in cards.values():
        where = f"advice card {card.card_id} v{card.version}"
        named = (
            [c.link_id for c in card.applies_when]
            + card.required_link_ids
            + card.excluded_if_present
        )
        unknown = sorted(set(named) - set(items))
        if unknown:
            raise ValueError(f"{where}: no such item in the plan: {', '.join(unknown)}")
        for condition in card.applies_when:
            if items[condition.link_id].ctcae_term is None:
                raise ValueError(f"{where}: {condition.link_id} has no CTCAE term to grade")


# --- selection -----------------------------------------------------------------------------


def _absent(findings: Mapping[str, Finding], link_id: str) -> bool:
    finding = findings.get(link_id)
    return finding is not None and finding.state == "absent"


def _eligible(card: AdviceCard, assessment: Assessment, findings: Mapping[str, Finding]) -> bool:
    grades = {grade.link_id: grade for grade in assessment.grades}
    for condition in card.applies_when:
        finding = findings.get(condition.link_id)
        grade = grades.get(condition.link_id)
        if finding is None or finding.state != "present" or grade is None:
            return False
        if grade.grade is None or grade.needs_clinical_assessment:
            return False  # "at least N" never matches
        if grade.grade not in condition.grades:
            return False
    for link_id in card.required_link_ids:
        finding = findings.get(link_id)
        if finding is None or finding.state not in ("present", "absent"):
            return False
    return all(_absent(findings, link_id) for link_id in card.excluded_if_present)


def select_card(
    assessment: Assessment,
    plan: MonitoringPlan,
    cards: Mapping[CardKey, AdviceCard] | None = None,
) -> tuple[str, str] | None:
    """The (card_id, version) of the first eligible card the plan permits, or None. `cards`
    defaults to the approved files in `data/content/advice/`. Raises ValueError if the assessment
    belongs to another plan or the plan permits a card that isn't loaded."""
    if assessment.status != "no_escalation":
        return None  # urgent overrides advice; review and missing information get none either
    if (assessment.questionnaire_url, assessment.questionnaire_version) != (
        plan.questionnaire_url,
        plan.questionnaire_version,
    ):
        raise ValueError("the assessment belongs to another plan")
    if assessment.missing_link_ids:
        return None
    cards = _default_cards() if cards is None else cards
    check_cards(plan, cards)
    findings = {finding.link_id: finding for finding in assessment.findings}
    if not all(_absent(findings, item.link_id) for item in plan.items if item.red_flag):
        return None  # a red flag present, unknown or unanswered never gets self-care advice
    by_id = {card_id: (card_id, version) for card_id, version in cards}
    for card_id in sorted(set(plan.advice_card_ids)):
        key = by_id[card_id]
        if _eligible(cards[key], assessment, findings):
            return key
    return None
