"""Free-text extraction (WP8, guards G1 and G2): the patient's message → `ExtractionResult`.

The LLM reads, the rules decide (principle 1). The model only fills a strict JSON schema built from
the patient's Questionnaire: per item, whether the message mentions it (`present`, `absent`,
`unknown` or `not_mentioned`), the chosen answer option or the number, and a verbatim quote. It
never sets a grade, a status or any text a patient sees.

- The call goes only through the gateway (`models.ModelGateway.extract_json`), which pseudonymises
  the message and allows only the approved endpoints (D5, G6). The request carries the approved
  Questionnaire items and the current message, nothing else: no records, ids or dates.
- The reply is validated strictly. Invalid JSON or a reply that breaks the schema is retried once
  (`MAX_ATTEMPTS`), then `ExtractionFailed`: the patient gets the full button path instead, and the
  failure is in the audit. Never a silent success.
- Quote check (G2): every finding needs a quote that is in the message (or in the masked text the
  model saw); a number must appear in its quote; an answer option must be one the item offers. A
  finding that fails is dropped and named in `uncertainties`, so the item is asked with buttons.
- A message no model read (extraction off or failed) is kept whole on the plan's free-text item
  (`keep_unread`), so the rules send the report to a nurse to read; the buttons can't close it.
- `not_mentioned` makes no finding. "Nevím" in the message is `unknown`, never `absent`.
- `merge` only raises: a quote fills an item nobody answered yet, or turns a non-`present` answer
  into `present`. It never overwrites a button answer with a denial or a "don't know".

Red-flag denials and quoted numbers are not trusted to close anything: `wizard.needs_confirmation`
asks the patient to confirm them with a button (G3, G5) before they count.

`validate_reply`, `merge`, `reply_schema` and `instructions` are pure; `extract` makes the call.
"""

import json
import math
import re
import unicodedata
from collections.abc import Iterable, Sequence
from typing import Any, Literal

from pydantic import ValidationError

from hlidac import wizard
from hlidac.models import ModelCallFailed, ModelGateway, pseudonymise
from hlidac.schemas import (
    DONT_KNOW,
    NO,
    YES,
    ExtractionResult,
    Finding,
    InboundMessage,
    MessageSource,
    MonitoringPlan,
    PlanItem,
    Quantity,
    QuoteSource,
    Strict,
)

EXTRACTOR_VERSION = "extract-v1"
SCHEMA_NAME = "symptom_report"
MAX_ATTEMPTS = 2  # one retry after an invalid reply; then a visible failure

ReplyState = Literal["present", "absent", "unknown", "not_mentioned"]


class ExtractionFailed(RuntimeError):
    """No valid extraction after the bounded retries; the call(s) are in the audit."""


class InvalidReply(ValueError):
    """The model's reply doesn't fit the schema (retried, then ExtractionFailed)."""


# --- what the model gets ------------------------------------------------------------------------

_STATES = ["present", "absent", "unknown", "not_mentioned"]
_QUOTE = {"type": ["string", "null"]}


def reply_schema(plan: MonitoringPlan) -> dict[str, Any]:
    """The strict JSON schema of the reply: one required property per Questionnaire item."""
    properties: dict[str, Any] = {}
    for item in plan.items:
        fields: dict[str, Any] = {"state": {"type": "string", "enum": _STATES}}
        if item.type == "choice":
            codes = [o.code for o in item.options]
            fields["option"] = {"type": ["string", "null"], "enum": [*codes, None]}
        elif item.type == "quantity":
            fields["value"] = {"type": ["number", "null"]}
        fields["quote"] = _QUOTE
        properties[item.link_id] = {
            "type": "object",
            "properties": fields,
            "required": list(fields),
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _describe(item: PlanItem) -> str:
    line = f'- {item.link_id} ({item.type}): "{item.text_cs}"'
    if item.options:
        options = "; ".join(f'{o.code} = "{o.text_cs}"' for o in item.options)
        line += f"\n  options: {options}"
    if item.unit:
        line += f"\n  unit: {item.unit} (UCUM; {wizard.unit_label(item.unit)})"
    return line


def instructions(plan: MonitoringPlan, asked: PlanItem | None = None) -> str:
    """The task and the approved Questionnaire items. Public content only (the gateway refuses
    instructions that look like patient data). `asked` is the approved question the chat showed
    last, so a short reply ("asi čtyřikrát") can be read as its answer."""
    items = "\n".join(_describe(item) for item in plan.items)
    if asked is not None:
        items += (
            f'\n\nThe message replies to the question for item {asked.link_id}: "{asked.text_cs}". '
            "A short reply answers that item; the same rules apply (quote it verbatim)."
        )
    return f"""You read one message in Czech from a patient on cancer treatment and fill in a
symptom questionnaire from it. You only record what the message says. You never judge severity,
never give advice and never add anything the patient did not say.

For every item below return:
- state: "present" if the message says the patient has it or answers it; "absent" only if the
  message explicitly denies it (e.g. "horečku nemám"); "unknown" if the patient says they don't
  know (e.g. "nevím", "neměřila jsem"); otherwise "not_mentioned". Never guess: anything unclear
  is "not_mentioned".
- option (choice items): the answer option code that the message clearly supports, else null. For
  yes/no items use "yes" with present and "no" with absent. Use "dont_know" only with unknown.
- value (quantity items): the number exactly as written in the message, else null.
- quote: the shortest exact, verbatim part of the message (copied character for character, no
  changes) that supports the answer; null only for not_mentioned.

Item "other" (if present) is for any other complaint the patient describes that no item covers.
Parts like [JMÉNO], [TELEFON] or [ADRESA] are masked personal data; ignore them.

Items ({plan.questionnaire_url} version {plan.questionnaire_version}):
{items}
"""


# --- validating the reply -----------------------------------------------------------------------


class _ChoiceReply(Strict):
    state: ReplyState
    option: str | None
    quote: str | None


class _QuantityReply(Strict):
    state: ReplyState
    value: float | None
    quote: str | None


class _TextReply(Strict):
    state: ReplyState
    quote: str | None


_REPLY_MODELS = {"choice": _ChoiceReply, "quantity": _QuantityReply, "text": _TextReply}
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def _norm(text: str) -> str:
    """For the quote check only: NFC, one space for any whitespace, case-insensitive."""
    return " ".join(unicodedata.normalize("NFC", text).casefold().split())


def _number_in(quote: str, value: float) -> bool:
    return any(
        math.isclose(float(n.replace(",", ".")), value, abs_tol=1e-9)
        for n in _NUMBER.findall(quote)
    )


def _parse(raw: str, plan: MonitoringPlan) -> dict[str, Strict]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise InvalidReply("not JSON") from error
    if not isinstance(data, dict) or set(data) != {item.link_id for item in plan.items}:
        raise InvalidReply("the reply must name exactly the Questionnaire's items")
    parsed: dict[str, Strict] = {}
    for item in plan.items:
        try:
            parsed[item.link_id] = _REPLY_MODELS[item.type].model_validate(data[item.link_id])
        except ValidationError as error:
            raise InvalidReply(f"{item.link_id}: does not fit the schema") from error
    for item in plan.items:
        reply = parsed[item.link_id]
        option = getattr(reply, "option", None)
        if option is not None and option not in {o.code for o in item.options}:
            raise InvalidReply(f"{item.link_id}: an option the item doesn't offer")
        value = getattr(reply, "value", None)
        if value is not None and not math.isfinite(value):
            raise InvalidReply(f"{item.link_id}: not a finite number")
    return parsed


def _finding(
    item: PlanItem, reply: Strict, message_id: str, texts: Sequence[str]
) -> tuple[Finding | None, str | None]:
    """The finding for one item, or None with the reason it was dropped (None, None: nothing
    said)."""
    state = reply.state
    if state == "not_mentioned":
        return None, None
    quote = (reply.quote or "").strip()
    if not quote:
        return None, f"{item.link_id}: no quote"
    if not any(_norm(quote) in _norm(text) for text in texts):
        return None, f"{item.link_id}: the quote is not in the message"
    option = getattr(reply, "option", None)
    value = getattr(reply, "value", None)
    if option == DONT_KNOW:
        state = "unknown"
    code: str | None = None
    quantity: Quantity | None = None
    if state == "unknown":
        pass  # a quoted "don't know" carries no option or value
    elif item.type == "choice":
        codes = {o.code for o in item.options}
        if state == "absent":  # only an item with a "no" option can be denied
            if NO not in codes or option not in (None, NO):
                return None, f"{item.link_id}: a denial that doesn't fit the item"
            code = NO
        elif YES in codes and option in (None, YES):
            code = YES
        elif option is None or option == NO:
            return None, f"{item.link_id}: no answer option"
        else:
            code = option
    elif item.type == "quantity":
        if state == "absent" or value is None:
            return None, f"{item.link_id}: no number"
        if not _number_in(quote, value):
            return None, f"{item.link_id}: the number is not in the quote"
        quantity = Quantity(value=value, unit=item.unit or "")
    elif state != "present":  # free text: only something said counts
        return None, None
    source = QuoteSource(kind="quote", message_id=message_id, quote=quote)
    finding = Finding(
        link_id=item.link_id,
        state=state,
        code=code,
        value=quantity,
        source=source,
        extractor_version=EXTRACTOR_VERSION,
    )
    return finding, None


def validate_reply(
    raw: str, plan: MonitoringPlan, message_id: str, texts: Sequence[str]
) -> ExtractionResult:
    """The model's raw reply → validated findings. `texts` are the message as written and as the
    model saw it (masked); a quote must be in one of them. Raises InvalidReply for a reply that
    breaks the schema; findings that fail the quote check are dropped and listed."""
    parsed = _parse(raw, plan)
    findings: list[Finding] = []
    dropped: list[str] = []
    for item in plan.items:
        finding, reason = _finding(item, parsed[item.link_id], message_id, texts)
        if finding is not None:
            findings.append(finding)
        if reason is not None:
            dropped.append(reason)
    return ExtractionResult(findings=findings, uncertainties=dropped)


# --- the call -----------------------------------------------------------------------------------


def extract(
    gateway: ModelGateway,
    plan: MonitoringPlan,
    message: InboundMessage,
    known_names: Iterable[str] = (),
    asked: PlanItem | None = None,
) -> ExtractionResult:
    """Extract one text message. Raises ExtractionFailed after MAX_ATTEMPTS invalid replies or
    failed calls; every attempt is in the audit (the gateway logs it)."""
    if message.kind != "text" or message.text is None:
        raise ValueError("only a text message is extracted")
    names = list(known_names)
    texts = [message.text, pseudonymise(message.text, names)]
    for _ in range(MAX_ATTEMPTS):
        try:
            reply = gateway.extract_json(
                instructions=instructions(plan, asked),
                patient_text=message.text,
                schema=reply_schema(plan),
                schema_name=SCHEMA_NAME,
                target=f"message:{message.message_id}",
                known_names=names,
            )
            return validate_reply(reply.text, plan, message.message_id, texts)
        except (ModelCallFailed, InvalidReply):
            continue
    raise ExtractionFailed(f"no valid extraction after {MAX_ATTEMPTS} attempts")


# --- merging into the check-in ------------------------------------------------------------------


def merge(
    findings: Sequence[Finding], result: ExtractionResult, plan: MonitoringPlan
) -> list[Finding]:
    """The check-in's findings with the extraction merged in, raise-only: a quote fills an item
    with no answer, or makes a non-present answer present. Findings for items not enabled (a
    follow-up whose parent isn't "yes") are left out; they are asked if the parent becomes yes."""
    merged = {f.link_id: f for f in findings}
    for new in result.findings:
        old = merged.get(new.link_id)
        fills = old is None or old.state == "not_mentioned"
        if fills or (new.state == "present" and old.state != "present"):
            merged[new.link_id] = new
    by_id = {item.link_id: item for item in plan.items}
    taken = {n.link_id for n in result.findings if merged.get(n.link_id) is n}
    return [
        f
        for f in merged.values()
        if f.link_id in by_id
        and (f.link_id not in taken or wizard.is_enabled(by_id[f.link_id], merged))
    ]


def free_text_item(plan: MonitoringPlan) -> str:
    """The plan's free-text item, where a message no model has read is kept. Raises ValueError if
    the plan has none (checked at app start, `processing.check_plans`)."""
    item = next((i for i in plan.items if i.type == "text"), None)
    if item is None:
        raise ValueError("the plan has no free-text item to keep an unread message on")
    return item.link_id


def keep_unread(
    findings: Sequence[Finding], plan: MonitoringPlan, message: InboundMessage
) -> list[Finding]:
    """A written message that no model read (extraction off or failed) still counts: it fills the
    plan's free-text item as `present`, so the `free_text_reported` rule sends the report to a
    nurse to read, whatever the buttons say (principle 2). Raise-only: a free-text item that is
    already present stays as it is."""
    if message.kind != "text":
        raise ValueError("only a text message is kept unread")
    link_id = free_text_item(plan)
    if any(f.link_id == link_id and f.state == "present" for f in findings):
        return list(findings)
    unread = Finding(
        link_id=link_id,
        state="present",
        source=MessageSource(kind="message", message_id=message.message_id),
    )
    return [*(f for f in findings if f.link_id != link_id), unread]
