"""What the patient app needs from the other work packages, passed to `create_app` (WP9).

- `plans`: the monitoring plans by Questionnaire url and version (WP1 loads them from
  `data/questionnaires/`). Without a plan the patient can't start a check-in, and the home page
  says so.
- `process`: runs after every saved answer and when the patient sends the report: extraction (WP8),
  grading (WP3) and assessment (WP4). It stores the assessment and, for an urgent one, the alert row
  before it returns (urgent never waits), and returns the assessment, or None while nothing can
  assess yet. It gets the check-in without unconfirmed "Ne" answers to consequential items
  (`wizard.for_assessment`), so those never close a red flag. If it raises, the check-in becomes
  `processing_failed` and stays visible.
- `advice_cards`: approved cards by id and version (WP6); the result screen shows their text as is.
- `extract`: reads a text message into findings through the model gateway (WP8,
  `extraction.extract`); it gets the message, the plan and the names to mask. None (no model
  configured) or a failure: the text is kept for the nurse and every question is asked with
  buttons.
- `transcribe`: turns a recording (the page's 16 kHz mono WAV) into text in this process (WP17,
  `transcription.MlxWhisper` through `models.VoiceGateway`); it gets the audio and the audit
  target. None: no voice button is shown.
- `context`: reads the patient's treatment context (the hospital record in IRIS, WP14) for the
  patient's own context bar ("day N after the last dose"). None: no bar. Never sent to a model.
- `grading_attributes`: per symptom item, the items its CTCAE grade reads (from the mapping), so
  clarification (WP5) asks them when the symptom is reported.

The defaults are empty: until those work packages land, the app stores check-ins and says plainly
that nobody has assessed them yet.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from hlidac.schemas import (
    AdviceCard,
    Assessment,
    Checkin,
    ExtractionResult,
    MonitoringPlan,
    PatientContext,
)

Process = Callable[[Checkin, MonitoringPlan, datetime], Assessment | None]
# (message, plan, names to mask[, asked=the chat's last question]) -> findings
Extract = Callable[..., ExtractionResult]
Transcribe = Callable[[bytes, str], str]
ReadContext = Callable[[str], PatientContext | None]


@dataclass(frozen=True)
class Pipeline:
    plans: dict[tuple[str, str], MonitoringPlan] = field(default_factory=dict)
    process: Process | None = None
    advice_cards: dict[tuple[str, str], AdviceCard] = field(default_factory=dict)
    extract: Extract | None = None
    grading_attributes: Mapping[str, frozenset[str]] = field(default_factory=dict)
    transcribe: Transcribe | None = None
    context: ReadContext | None = None

    def plan(self, url: str, version: str) -> MonitoringPlan | None:
        return self.plans.get((url, version))

    def card(self, card_id: str, version: str) -> AdviceCard | None:
        return self.advice_cards.get((card_id, version))
