"""Monitoring plans (WP1): the FHIR R4 `Questionnaire` plus its plan sidecar → `MonitoringPlan`.

A plan is two files:
- `data/questionnaires/<name>.json`: a FHIR R4 Questionnaire in our subset (flat items of type
  `choice`, `quantity` or `text`; CTCAE term in `item.code`, red flag and unit as extensions,
  conditional items by `enableWhen` with "=" on an answer coding);
- `data/plans/<name>.plan.yaml`: the sidecar with the rule, mapping and advice-card ids, the
  clarification limit, the source and the approval.

`check_questionnaire` accepts only that subset and rejects anything it doesn't understand (an
unknown key or extension URL could otherwise drop a red flag silently). The conversion functions
are pure; only `read_questionnaire`, `load_plan` and `load_plans` read files. The Questionnaire
reaches IRIS only through the outbox (`enqueue_questionnaire`), never at startup.

CLI: `uv run python -m hlidac.plans check` loads every plan and prints counts only.
"""

import argparse
import copy
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import Field, StringConstraints, model_validator

from hlidac.content import DATA_DIR, read_yaml
from hlidac.fhir_guard import TAG_CODE, TAG_SYSTEM, check_bundle
from hlidac.schemas import (
    AnswerOption,
    EnableWhen,
    FhirTransactionJob,
    MonitoringPlan,
    OutboxItem,
    PlanItem,
    Strict,
)
from hlidac.schemas._base import Id, LinkId, NonEmpty
from hlidac.store import Store

IDENTIFIER_SYSTEM = "urn:hlidac:questionnaire"
ANSWER_SYSTEM = "urn:hlidac:answer"
SYMPTOM_SYSTEM = "urn:hlidac:symptom"
# The only code systems in `item.code`; a typo is rejected, never silently ignored.
CTCAE_SYSTEM = re.compile(r"urn:hlidac:ctcae:v[0-9]+\.[0-9]+")  # fullmatch, e.g. ...:v5.0
RED_FLAG_URL = "urn:hlidac:extension:red-flag"
UNIT_URL = "http://hl7.org/fhir/StructureDefinition/questionnaire-unit"
UCUM = "http://unitsofmeasure.org"
DEMO_LABEL = "DEMO"

# R4 value sets (hl7.fhir.r4.core 4.0.1).
R4_STATUS = frozenset({"draft", "active", "retired", "unknown"})
R4_ITEM_TYPES = frozenset(
    {
        "group", "display", "boolean", "decimal", "integer", "date", "dateTime", "time",
        "string", "text", "url", "choice", "open-choice", "attachment", "reference", "quantity",
    }
)  # fmt: skip
R4_OPERATORS = frozenset({"exists", "=", "!=", ">", "<", ">=", "<="})
R4_ANSWER_OPTION_VALUES = frozenset(
    {"valueInteger", "valueDate", "valueTime", "valueString", "valueCoding", "valueReference"}
)
R4_ENABLE_WHEN_ANSWERS = frozenset(
    {
        "answerBoolean", "answerDecimal", "answerInteger", "answerDate", "answerDateTime",
        "answerTime", "answerString", "answerCoding", "answerQuantity", "answerReference",
    }
)  # fmt: skip
SUPPORTED_ITEM_TYPES = frozenset({"choice", "quantity", "text"})

# The R4 elements we use; anything else is rejected, not ignored.
RESOURCE_KEYS = frozenset(
    {
        "resourceType", "meta", "language", "url", "identifier", "version", "name", "title",
        "status", "experimental", "subjectType", "date", "publisher", "description", "purpose",
        "copyright", "approvalDate", "lastReviewDate", "item",
    }
)  # fmt: skip
ITEM_KEYS = frozenset(
    {
        "linkId", "text", "type", "required", "code", "extension", "answerOption", "enableWhen",
        "enableBehavior",
    }
)  # fmt: skip
META_KEYS = frozenset({"tag"})
CODING_KEYS = frozenset({"system", "version", "code", "display"})
IDENTIFIER_KEYS = frozenset({"use", "system", "value"})

# Used with fullmatch: `match` with `$` would also accept a trailing newline.
_LINK_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")  # schemas._base.LinkId
_IDENTIFIER_VALUE = re.compile(r"[A-Za-z0-9._:-]{1,64}")  # fhir_guard._VALUE


class PlanSidecar(Strict):
    """`data/plans/<name>.plan.yaml`: what the Questionnaire itself doesn't say."""

    questionnaire: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._-]+\.json$")]
    max_clarification_turns: int
    mapping_ids: list[Id]
    rule_ids: list[Id]
    advice_card_ids: list[Id]
    source_ref: NonEmpty
    approved_by: Id | None
    approval: NonEmpty  # "DEMO — čeká na schválení FN Motol" until a clinician approves
    measured_by: dict[LinkId, LinkId] = Field(default_factory=dict)  # MonitoringPlan (D54)
    answered_by_reading: dict[LinkId, float] = Field(default_factory=dict)  # MonitoringPlan (D55)

    @model_validator(mode="after")
    def _labelled(self) -> "PlanSidecar":
        if self.approved_by is None and DEMO_LABEL not in self.approval:
            raise ValueError("an unapproved plan is labelled DEMO")
        return self


# --- the Questionnaire subset -------------------------------------------------------------


def _require(condition: object, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _only_keys(obj: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(obj) - allowed)
    _require(not unknown, f"{where}: unsupported element(s) {', '.join(unknown)}")


def _non_empty_str(value: object) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _check_coding(coding: object, where: str) -> dict[str, Any]:
    _require(isinstance(coding, dict), f"{where}: a Coding is an object")
    assert isinstance(coding, dict)
    _only_keys(coding, CODING_KEYS, where)
    for key in CODING_KEYS & set(coding):
        _require(_non_empty_str(coding[key]), f"{where}: {key} is a non-empty string")
    return coding


def _one_value(obj: dict[str, Any], r4_choices: frozenset[str], where: str) -> str:
    """The single value[x] / answer[x] key, which R4 requires to be exactly one."""
    present = [key for key in obj if key in r4_choices]
    _require(len(present) == 1, f"{where}: exactly one of {'/'.join(sorted(r4_choices))}")
    return present[0]


def _check_meta(q: dict[str, Any]) -> None:
    meta = q.get("meta")
    _require(isinstance(meta, dict), "meta: required, with the hlidac tag")
    _only_keys(meta, META_KEYS, "meta")
    tags = meta.get("tag")
    _require(isinstance(tags, list), "meta.tag: a list")
    for n, tag in enumerate(tags):
        _check_coding(tag, f"meta.tag[{n}]")
    _require(
        any(t.get("system") == TAG_SYSTEM and t.get("code") == TAG_CODE for t in tags),
        f"meta.tag: carries {TAG_SYSTEM}|{TAG_CODE}",
    )


def our_identifier(q: dict[str, Any]) -> str:
    """The value of the Questionnaire's one identifier in `urn:hlidac:questionnaire`."""
    identifiers = q.get("identifier")
    _require(isinstance(identifiers, list), "identifier: a list")
    ours = []
    for n, identifier in enumerate(identifiers):
        where = f"identifier[{n}]"
        _require(isinstance(identifier, dict), f"{where}: an object")
        _only_keys(identifier, IDENTIFIER_KEYS, where)
        if identifier.get("system") == IDENTIFIER_SYSTEM:
            ours.append(identifier.get("value"))
    _require(len(ours) == 1, f"identifier: exactly one in {IDENTIFIER_SYSTEM}")
    value = ours[0]
    _require(
        isinstance(value, str) and _IDENTIFIER_VALUE.fullmatch(value),
        "identifier: our value is plain ([A-Za-z0-9._:-], at most 64)",
    )
    return value


def _check_extensions(item: dict[str, Any], where: str) -> None:
    extensions = item.get("extension", [])
    _require(isinstance(extensions, list), f"{where}.extension: a list")
    seen: set[str] = set()
    for n, extension in enumerate(extensions):
        at = f"{where}.extension[{n}]"
        _require(isinstance(extension, dict), f"{at}: an object")
        url = extension.get("url")
        _require(url in {RED_FLAG_URL, UNIT_URL}, f"{at}: unsupported extension {url!r}")
        _require(url not in seen, f"{at}: {url} appears once")
        seen.add(url)
        value_keys = [k for k in extension if k != "url"]
        _require(len(value_keys) == 1, f"{at}: exactly one value[x]")
        if url == RED_FLAG_URL:
            _require(
                value_keys == ["valueBoolean"] and isinstance(extension["valueBoolean"], bool),
                f"{at}: the red flag is a valueBoolean",
            )
        else:
            _require(value_keys == ["valueCoding"], f"{at}: the unit is a valueCoding")
            coding = _check_coding(extension["valueCoding"], f"{at}.valueCoding")
            _require(
                coding.get("system") == UCUM and "code" in coding,
                f"{at}: the unit is a UCUM code",
            )


def _is_ctcae(coding: dict[str, Any]) -> bool:
    system = coding.get("system")
    return isinstance(system, str) and CTCAE_SYSTEM.fullmatch(system) is not None


def _check_codes(item: dict[str, Any], where: str) -> str | None:
    """Only our symptom and CTCAE systems; returns the item's CTCAE system (with its version)."""
    codes = item.get("code", [])
    _require(isinstance(codes, list), f"{where}.code: a list")
    ctcae: list[str] = []
    for n, coding in enumerate(codes):
        at = f"{where}.code[{n}]"
        coding = _check_coding(coding, at)
        system = coding.get("system")
        _require(
            system == SYMPTOM_SYSTEM or _is_ctcae(coding),
            f"{at}: system {system!r} is not {SYMPTOM_SYSTEM} or urn:hlidac:ctcae:v<major>.<minor>",
        )
        _require("code" in coding, f"{at}: a code")
        if _is_ctcae(coding):
            _require("display" in coding, f"{at}: a CTCAE coding names its term")
            ctcae.append(system)
    _require(len(ctcae) <= 1, f"{where}.code: at most one CTCAE term")
    return ctcae[0] if ctcae else None


def _check_answer_options(item: dict[str, Any], where: str) -> None:
    options = item.get("answerOption", [])
    _require(isinstance(options, list), f"{where}.answerOption: a list")
    for n, option in enumerate(options):
        at = f"{where}.answerOption[{n}]"
        _require(isinstance(option, dict), f"{at}: an object")
        _only_keys(option, R4_ANSWER_OPTION_VALUES, at)
        key = _one_value(option, R4_ANSWER_OPTION_VALUES, at)
        _require(key == "valueCoding", f"{at}: we use valueCoding")
        coding = _check_coding(option[key], f"{at}.valueCoding")
        _require(
            coding.get("system") == ANSWER_SYSTEM and "code" in coding and "display" in coding,
            f"{at}: a {ANSWER_SYSTEM} coding with code and display",
        )


def _check_enable_when(item: dict[str, Any], where: str, earlier: set[str]) -> None:
    conditions = item.get("enableWhen", [])
    _require(isinstance(conditions, list), f"{where}.enableWhen: a list")
    for n, condition in enumerate(conditions):
        at = f"{where}.enableWhen[{n}]"
        _require(isinstance(condition, dict), f"{at}: an object")
        _only_keys(condition, R4_ENABLE_WHEN_ANSWERS | {"question", "operator"}, at)
        operator = condition.get("operator")
        _require(operator in R4_OPERATORS, f"{at}: operator {operator!r} is not an R4 operator")
        _require(operator == "=", f"{at}: only the operator '=' is supported")
        key = _one_value(condition, R4_ENABLE_WHEN_ANSWERS, at)
        _require(key == "answerCoding", f"{at}: we use answerCoding")
        coding = _check_coding(condition[key], f"{at}.answerCoding")
        _require(
            coding.get("system") == ANSWER_SYSTEM and "code" in coding,
            f"{at}: a {ANSWER_SYSTEM} coding with a code",
        )
        _require(
            condition.get("question") in earlier, f"{at}: the question is an earlier item's linkId"
        )
    behaviour = item.get("enableBehavior")
    if behaviour is not None or len(conditions) > 1:
        _require(
            behaviour in {"all", "any"},
            f"{where}: enableBehavior 'all' is required with more than one condition",
        )
        _require(behaviour == "all", f"{where}: only enableBehavior 'all' is supported")


def _check_item(item: object, n: int, earlier: set[str]) -> tuple[str, str | None]:
    where = f"item[{n}]"
    _require(isinstance(item, dict), f"{where}: an object")
    assert isinstance(item, dict)
    link_id = item.get("linkId")
    _require(
        isinstance(link_id, str) and _LINK_ID.fullmatch(link_id),
        f"{where}: linkId matches [A-Za-z0-9._-], at most 64",
    )
    where = f"item[{n}] ({link_id})"
    _require(link_id not in earlier, f"{where}: linkIds are unique")
    if "item" in item:
        raise ValueError(f"{where}: nested items (groups) are not supported in v1")
    _only_keys(item, ITEM_KEYS, where)
    item_type = item.get("type")
    _require(item_type in R4_ITEM_TYPES, f"{where}: type {item_type!r} is not an R4 item type")
    _require(
        item_type in SUPPORTED_ITEM_TYPES,
        f"{where}: type {item_type!r} is not supported (only choice, quantity, text)",
    )
    _require(_non_empty_str(item.get("text")), f"{where}: text is the approved Czech wording")
    _require(isinstance(item.get("required"), bool), f"{where}: required is true or false")
    ctcae_system = _check_codes(item, where)
    _check_extensions(item, where)
    _check_answer_options(item, where)
    _check_enable_when(item, where, earlier)
    return link_id, ctcae_system


def check_questionnaire(q: dict[str, Any]) -> None:
    """Raise ValueError unless `q` is a structurally valid R4 Questionnaire in our subset."""
    _require(isinstance(q, dict), "a Questionnaire is a JSON object")
    _require(q.get("resourceType") == "Questionnaire", "resourceType is Questionnaire")
    _only_keys(q, RESOURCE_KEYS, "Questionnaire")
    status = q.get("status")
    _require(status in R4_STATUS, f"status {status!r} is not draft/active/retired/unknown")
    _require(_non_empty_str(q.get("url")), "url: required")
    version = q.get("version")
    _require(
        _non_empty_str(version) and len(version) <= 32, "version: required, at most 32 characters"
    )
    _require(_non_empty_str(q.get("title")), "title: required (Czech)")
    _require(q.get("language") == "cs", "language: cs")
    for key in ("name", "publisher", "description", "purpose", "copyright", "date"):
        if key in q:
            _require(_non_empty_str(q[key]), f"{key}: a non-empty string")
    if "experimental" in q:
        _require(isinstance(q["experimental"], bool), "experimental: true or false")
    if "subjectType" in q:
        _require(q["subjectType"] == ["Patient"], "subjectType: [Patient]")
    _check_meta(q)
    our_identifier(q)
    items = q.get("item")
    _require(isinstance(items, list) and items, "item: a non-empty list")
    earlier: set[str] = set()
    ctcae_systems: set[str] = set()
    for n, item in enumerate(items):
        link_id, ctcae_system = _check_item(item, n, earlier)
        earlier.add(link_id)
        if ctcae_system is not None:
            ctcae_systems.add(ctcae_system)
    _require(
        len(ctcae_systems) <= 1,
        f"item.code: one CTCAE version per Questionnaire, not {', '.join(sorted(ctcae_systems))}",
    )


# --- Questionnaire → MonitoringPlan --------------------------------------------------------


def _plan_item(item: dict[str, Any]) -> PlanItem:
    ctcae = [c["display"] for c in item.get("code", []) if _is_ctcae(c)]
    extensions = {e["url"]: e for e in item.get("extension", [])}
    unit = extensions.get(UNIT_URL)
    red_flag = extensions.get(RED_FLAG_URL)
    return PlanItem(
        link_id=item["linkId"],
        text_cs=item["text"],
        type=item["type"],
        required=item["required"],
        red_flag=red_flag["valueBoolean"] if red_flag else False,
        ctcae_term=ctcae[0] if ctcae else None,
        options=[
            AnswerOption(code=o["valueCoding"]["code"], text_cs=o["valueCoding"]["display"])
            for o in item.get("answerOption", [])
        ],
        unit=unit["valueCoding"]["code"] if unit else None,
        enable_when=[
            EnableWhen(link_id=c["question"], answer_code=c["answerCoding"]["code"])
            for c in item.get("enableWhen", [])
        ],
    )


def plan_from_questionnaire(q: dict[str, Any], sidecar: PlanSidecar) -> MonitoringPlan:
    """Pure. Raises ValueError (or pydantic's ValidationError, a ValueError) on any problem."""
    check_questionnaire(q)
    if sidecar.approved_by is None:
        _require(q["status"] == "draft", "an unapproved Questionnaire has status draft")
        _require(DEMO_LABEL in q.get("description", ""), "an unapproved Questionnaire says DEMO")
    return MonitoringPlan(
        questionnaire_url=q["url"],
        questionnaire_version=q["version"],
        title_cs=q["title"],
        items=[_plan_item(item) for item in q["item"]],
        rule_ids=sidecar.rule_ids,
        mapping_ids=sidecar.mapping_ids,
        advice_card_ids=sidecar.advice_card_ids,
        max_clarification_turns=sidecar.max_clarification_turns,
        source_ref=sidecar.source_ref,
        approved_by=sidecar.approved_by,
        measured_by=sidecar.measured_by,
        answered_by_reading=sidecar.answered_by_reading,
    )


# --- files ---------------------------------------------------------------------------------


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result, f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def read_questionnaire(path: Path) -> dict[str, Any]:
    """Read and check one Questionnaire file (duplicate JSON keys are an error)."""
    try:
        q = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys)
        check_questionnaire(q)
    except ValueError as error:
        raise ValueError(f"{path.name}: {error}") from error
    return q


def read_sidecar(plan_file: Path) -> PlanSidecar:
    """Read one plan sidecar (duplicate YAML keys are an error)."""
    try:
        raw = read_yaml(plan_file)
        return PlanSidecar.model_validate(raw)
    except (ValueError, yaml.YAMLError) as error:
        raise ValueError(f"{plan_file.name}: {error}") from error


def load_plan(plan_file: Path, questionnaire_dir: Path | None = None) -> MonitoringPlan:
    """One plan. The sidecar names a file in `questionnaire_dir` (by default the
    `questionnaires/` folder next to the sidecar's folder, i.e. `data/questionnaires/`)."""
    sidecar = read_sidecar(plan_file)
    folder = questionnaire_dir or plan_file.parent.parent / "questionnaires"
    q = read_questionnaire(folder / sidecar.questionnaire)
    try:
        return plan_from_questionnaire(q, sidecar)
    except ValueError as error:
        raise ValueError(f"{plan_file.name}: {error}") from error


def load_plans(data_dir: Path = DATA_DIR) -> dict[tuple[str, str], MonitoringPlan]:
    """Every `data/plans/*.plan.yaml`, keyed by (Questionnaire url, version) as `Pipeline.plans`
    expects. No plans folder or no plans: an empty dict."""
    plans: dict[tuple[str, str], MonitoringPlan] = {}
    for plan_file in sorted((data_dir / "plans").glob("*.plan.yaml")):
        plan = load_plan(plan_file, data_dir / "questionnaires")
        key = (plan.questionnaire_url, plan.questionnaire_version)
        _require(key not in plans, f"{plan_file.name}: another plan already uses {key[0]}|{key[1]}")
        plans[key] = plan
    return plans


# --- write-back through the outbox -----------------------------------------------------------


def questionnaire_bundle(q: dict[str, Any]) -> dict[str, Any]:
    """A transaction Bundle that writes the Questionnaire by a conditional PUT on our identifier,
    so writing it again updates the same resource and never duplicates it."""
    check_questionnaire(q)
    value = our_identifier(q)
    bundle = {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [
            {
                "resource": copy.deepcopy(q),
                "request": {
                    "method": "PUT",
                    "url": f"Questionnaire?identifier={IDENTIFIER_SYSTEM}|{value}",
                },
            }
        ],
    }
    check_bundle(bundle)
    return bundle


def questionnaire_key(q: dict[str, Any]) -> str:
    """The outbox idempotency key: one write per Questionnaire url and version. A changed text
    needs a new version; the same version is never written twice."""
    return f"questionnaire:{q['url']}|{q['version']}"


def enqueue_questionnaire(store: Store, q: dict[str, Any], now: datetime) -> bool:
    """Put the Questionnaire's write in the outbox. False if it is already there (a no-op)."""
    key = questionnaire_key(q)
    job = FhirTransactionJob.of(questionnaire_bundle(q))
    outbox_id = "questionnaire-" + hashlib.sha256(key.encode()).hexdigest()[:32]
    item = OutboxItem(outbox_id=outbox_id, idempotency_key=key, created_at=now, job=job)
    return store.enqueue(item)


# --- CLI -----------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hlidac.plans")
    parser.add_argument("command", choices=["check"])
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    args = parser.parse_args(argv)
    try:
        plans = load_plans(args.data_dir)
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f"invalid: {error}", file=sys.stderr)
        return 1
    items = [item for plan in plans.values() for item in plan.items]
    print(
        f"plans: {len(plans)}, items: {len(items)}, "
        f"red flags: {sum(i.red_flag for i in items)}, "
        f"with CTCAE term: {sum(i.ctcae_term is not None for i in items)}, "
        f"conditional: {sum(bool(i.enable_when) for i in items)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
