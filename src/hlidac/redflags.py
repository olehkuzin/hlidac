"""Deterministic red-flag phrase scan (WP22, two levels since D56). Checks a check-in's free-text
body against the phrases in ``data/plans/redflags-demo-v1.yaml``:

- an ``urgent`` phrase (truly critical: bleeding, breathing, chest pain, collapse, unable to drink
  …) bumps the assessment to ``urgent`` at once;
- a ``review`` phrase (worrying, decided by the questions: "mám horečku", "je mi hůř" …) makes it
  at least ``routine_review``, never "no escalation".

Matching ignores case and diacritics and starts at a word boundary (a phrase may be a stem).

Pure: no IRIS, network or clock. Only the three public functions are exported;
none of them reads files or patient data directly — that is done by ``load_files``
and ``raise_on_hit``. A hit can only **raise** urgency (PRINCIPLES.md principle 2);
nothing here changes an urgent verdict to routine, nor does it lower any CTCAE grade.

Every phrase comes from cited public oncology guidance, labelled DEMO — čeká na
schválení FN Motol, and lives in ``data/plans/redflags-demo-v1.yaml``."""

import functools
import re
import unicodedata
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import Field, model_validator

from hlidac.content import DATA_DIR, read_yaml
from hlidac.schemas import Assessment, Strict, TriggeredRule
from hlidac.schemas._base import Id, NonEmpty, Version

DEMO_LABEL = "DEMO"


class RedFlagPhrase(Strict):
    """One phrase line and its source."""

    phrase: NonEmpty  # Czech expression from the approved list
    level: Literal["urgent", "review"]  # D56: critical at once, or a nurse review at least
    source_ref: NonEmpty  # Where this phrase originates (public guidance or team note)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

PHRASES_DIR = DATA_DIR / "plans"
_RED_FILE_NAME = "redflags-demo-v1.yaml"


class RedFlagDefinition(Strict):
    """Parsed red-flag file (one version per content_id in practice)."""

    content_id: Id
    version: Version
    approval: NonEmpty
    approved_by: Id | None  # null until a clinician approves
    phrases: Annotated[list[RedFlagPhrase], Field(min_length=1)]

    @model_validator(mode="after")
    def _consistent(self) -> "RedFlagDefinition":
        if self.approved_by is None and DEMO_LABEL not in self.approval:
            raise ValueError("an unapproved red-flag list is labelled DEMO")
        return self


@functools.lru_cache(maxsize=1)
def load_files(directory: Path = PHRASES_DIR) -> RedFlagDefinition:
    """Every red-flag file in ``directory`` (currently only ``redflags-demo-v1.yaml``)."""
    path = directory / _RED_FILE_NAME
    try:
        raw = read_yaml(path)
        return RedFlagDefinition.model_validate(raw)
    except (ValueError, OSError, yaml.YAMLError) as error:
        raise ValueError(f"{path.name}: {error}") from error


@functools.cache
def _default_phrases() -> list[RedFlagPhrase]:
    """All approved phrases, cached so repeated checks do not re-read YAML."""
    return load_files().phrases


# ---------------------------------------------------------------------------
# Scanning logic  -----------------------------------------------------------
# ---------------------------------------------------------------------------


def fold(text: str) -> str:
    """Lower case without diacritics: "Nemůžu DÝCHAT" -> "nemuzu dychat"."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


@functools.cache
def _pattern(phrase: str) -> re.Pattern[str]:
    return re.compile(r"(?<!\w)" + re.escape(fold(phrase)))


def check(text: str | None) -> RedFlagPhrase | None:
    """The first matching phrase, an ``urgent`` one before any ``review`` one, or ``None``.
    ``None`` or empty text always returns ``None`` — absence is not a trigger."""

    if text is None or not text.strip():
        return None

    folded = fold(text)
    hits = [p for p in _default_phrases() if _pattern(p.phrase).search(folded)]
    return next((p for p in hits if p.level == "urgent"), hits[0] if hits else None)


RANK = {"no_escalation": 0, "insufficient_information": 1, "routine_review": 2, "urgent": 3}
REVIEW_RULE = "concern-phrase"


def raise_on_hit(assessment: Assessment, text: str | None) -> Assessment:
    """Return a new assessment raised by a phrase hit; the original is unmodified.

    A hit never **lowers** urgency (PRINCIPLES.md principle 2). An ``urgent`` phrase makes any
    verdict ``urgent``; a ``review`` phrase makes ``no_escalation`` or
    ``insufficient_information`` a ``routine_review`` and leaves anything higher as it is."""

    match = check(text)
    if match is None:
        return assessment
    if match.level == "review":
        return _raise_to_review(assessment, match)

    # Bump status to urgent (if not already) and clear any advice card (urgent overrides advice).
    # Keep original triggered_rules so the nurse can still see what other rules fired.
    rule = TriggeredRule(
        rule_id="red-flag-phrase",
        rule_version=load_files().version,
        outcome="urgent",
        inputs={"matched_phrase": match.phrase},
        reason=f"Pacient použil varovnou frázi v popisu: {match.phrase}",
    )
    already = any(
        r.rule_id == "red-flag-phrase" and r.inputs.get("matched_phrase") == match.phrase
        for r in assessment.triggered_rules
    )
    triggered = list(assessment.triggered_rules) if already else [*assessment.triggered_rules, rule]
    return assessment.evolve(
        status="urgent",
        triggered_rules=triggered,
        advice_card_id=None,
        advice_card_version=None,
    )


def _raise_to_review(assessment: Assessment, match: RedFlagPhrase) -> Assessment:
    rule = TriggeredRule(
        rule_id=REVIEW_RULE,
        rule_version=load_files().version,
        outcome="routine_review",
        inputs={"matched_phrase": match.phrase},
        reason=f"Pacient použil znepokojivou frázi v popisu: {match.phrase} – posoudí sestra",
    )
    already = any(
        r.rule_id == REVIEW_RULE and r.inputs.get("matched_phrase") == match.phrase
        for r in assessment.triggered_rules
    )
    triggered = list(assessment.triggered_rules) if already else [*assessment.triggered_rules, rule]
    if RANK[assessment.status] >= RANK["routine_review"]:
        return assessment.evolve(triggered_rules=triggered)
    return assessment.evolve(
        status="routine_review",
        triggered_rules=triggered,
        advice_card_id=None,  # a nurse looks first; self-care advice waits for her
        advice_card_version=None,
    )
