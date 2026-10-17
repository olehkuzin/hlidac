"""WP1: the Questionnaire + plan sidecar loader (`hlidac.plans`). Fixtures are invented TEST
content in tests/fixtures/, with no clinical content and no patient data."""

import copy
import json
import shutil
from pathlib import Path

import pytest
from factories import T0
from pydantic import ValidationError

from hlidac import plans
from hlidac.app import create_app
from hlidac.config import Settings
from hlidac.fhir_guard import check_bundle
from hlidac.schemas import AnswerOption, EnableWhen, MonitoringPlan, PlanItem
from hlidac.store import InMemoryStore

FIXTURES = Path(__file__).parent / "fixtures"
PLAN_FILE = FIXTURES / "test.plan.yaml"
Q_FILE = FIXTURES / "questionnaire-test.json"
ANSWER = "urn:hlidac:answer"


def questionnaire() -> dict:
    return json.loads(Q_FILE.read_text(encoding="utf-8"))


def sidecar(**changes) -> plans.PlanSidecar:
    return plans.read_sidecar(PLAN_FILE).evolve(**changes)


def item(q: dict, link_id: str) -> dict:
    return next(i for i in q["item"] if i["linkId"] == link_id)


def coding(code: str) -> dict:
    return {"system": ANSWER, "code": code}


def test_the_fixture_loads_into_the_expected_plan():
    yes_no = [
        AnswerOption(code="yes", text_cs="TEST Ano"),
        AnswerOption(code="no", text_cs="TEST Ne"),
        AnswerOption(code="dont_know", text_cs="TEST Nevím"),
    ]
    expected = MonitoringPlan(
        questionnaire_url="urn:hlidac:questionnaire:test",
        questionnaire_version="1",
        title_cs="TEST dotazník",
        items=[
            PlanItem(
                link_id="fever",
                text_cs="TEST Otázka na horečku?",
                type="choice",
                required=True,
                red_flag=True,
                ctcae_term="TEST CTCAE term",
                options=yes_no,
            ),
            PlanItem(
                link_id="temperature",
                text_cs="TEST Otázka na teplotu?",
                type="quantity",
                required=False,
                unit="Cel",
                enable_when=[EnableWhen(link_id="fever", answer_code="yes")],
            ),
            PlanItem(
                link_id="nausea",
                text_cs="TEST Otázka na nevolnost?",
                type="choice",
                required=True,
                options=[
                    AnswerOption(code="no", text_cs="TEST Žádná"),
                    AnswerOption(code="mild", text_cs="TEST Mírná"),
                    AnswerOption(code="dont_know", text_cs="TEST Nevím"),
                ],
            ),
            PlanItem(link_id="note", text_cs="TEST Poznámka", type="text", required=False),
        ],
        rule_ids=[],
        mapping_ids=["TEST-map-fever"],
        advice_card_ids=[],
        max_clarification_turns=3,
        source_ref="tests/fixtures (invented, TEST only)",
        approved_by=None,
    )
    assert plans.load_plan(PLAN_FILE, FIXTURES) == expected


def _top(key: str, value=None, drop: bool = False):
    def change(q: dict) -> None:
        if drop:
            del q[key]
        else:
            q[key] = value

    return change


def _on(link_id: str, key: str, value=None, drop: bool = False):
    def change(q: dict) -> None:
        if drop:
            del item(q, link_id)[key]
        else:
            item(q, link_id)[key] = value

    return change


def _two_answers(q):
    item(q, "temperature")["enableWhen"][0]["answerString"] = "yes"


def _two_option_values(q):
    item(q, "fever")["answerOption"][0]["valueString"] = "Ano"


def _condition_answer_boolean(q):
    condition = item(q, "temperature")["enableWhen"][0]
    del condition["answerCoding"]
    condition["answerBoolean"] = True


def _later_target(q):
    item(q, "temperature")["enableWhen"][0]["question"] = "nausea"


def _unknown_target(q):
    item(q, "temperature")["enableWhen"][0]["question"] = "missing"


def _duplicate_link_id(q):
    item(q, "note")["linkId"] = "fever"


def _second_ctcae(q):
    item(q, "fever")["code"].append(
        {"system": "urn:hlidac:ctcae:v5.0", "code": "00000001", "display": "TEST other term"}
    )


def _group(q):
    item(q, "note")["item"] = [{"linkId": "inner", "text": "TEST", "type": "text"}]


def _two_conditions(behaviour):
    def change(q):
        note = item(q, "note")
        note["enableWhen"] = [
            {"question": "fever", "operator": "=", "answerCoding": coding("yes")},
            {"question": "nausea", "operator": "=", "answerCoding": coding("mild")},
        ]
        if behaviour is not None:
            note["enableBehavior"] = behaviour

    return change


def _no_tag(q):
    q["meta"]["tag"] = [{"system": "urn:other", "code": "x"}]


def _wrong_identifier_system(q):
    q["identifier"] = [{"system": "urn:other", "value": "test-v1"}]


def _option(field, value):
    def change(q):
        item(q, "fever")["answerOption"][0] = {field: value}

    return change


def _condition(**fields):
    def change(q):
        item(q, "temperature")["enableWhen"][0].update(fields)

    return change


def _fever_ctcae(**fields):
    def change(q):
        item(q, "fever")["code"][1] = {"code": "00000000", "display": "TEST CTCAE term"} | fields

    return change


def _drop_from_ctcae(key):
    def change(q):
        del item(q, "fever")["code"][1][key]

    return change


def _symptom_without_code(q):
    del item(q, "fever")["code"][0]["code"]


def _second_ctcae_version(q):
    item(q, "nausea")["code"] = [
        {"system": "urn:hlidac:ctcae:v6.0", "code": "00000002", "display": "TEST other term"}
    ]


def _identifier_value(value):
    def change(q):
        q["identifier"][0]["value"] = value

    return change


def _extension(extension):
    def change(q):
        item(q, "fever")["extension"] = [extension]

    return change


STRUCTURAL = {
    "resourceType": (_top("resourceType", "Observation"), "resourceType"),
    "status not R4": (_top("status", "final"), "status"),
    "status missing": (_top("status", drop=True), "status"),
    "url missing": (_top("url", drop=True), "url"),
    "version missing": (_top("version", drop=True), "version"),
    "title missing": (_top("title", drop=True), "title"),
    "language": (_top("language", "en"), "language"),
    "no identifier of ours": (_wrong_identifier_system, "identifier"),
    "no hlidac tag": (_no_tag, "meta.tag"),
    "unknown resource element": (_top("contained", []), "unsupported element"),
    "no items": (_top("item", []), "item"),
    "duplicate linkId": (_duplicate_link_id, "unique"),
    "linkId pattern": (_on("note", "linkId", "a note"), "linkId"),
    "type not R4": (_on("note", "type", "number"), "not an R4 item type"),
    "type unsupported": (_on("note", "type", "boolean"), "not supported"),
    "group": (_group, "nested items"),
    "unknown item element": (_on("note", "repeats", True), "unsupported element"),
    "text missing": (_on("note", "text", drop=True), "text"),
    "required missing": (_on("note", "required", drop=True), "required"),
    "required not bool": (_on("note", "required", "true"), "required"),
    "two CTCAE terms": (_second_ctcae, "at most one CTCAE"),
    "unknown extension": (
        _extension({"url": "urn:hlidac:extension:red_flag", "valueBoolean": True}),
        "unsupported extension",
    ),
    "red flag not boolean": (
        _extension({"url": plans.RED_FLAG_URL, "valueString": "true"}),
        "valueBoolean",
    ),
    "option has two values": (_two_option_values, "exactly one of"),
    "option not a coding": (_option("valueString", "Ano"), "valueCoding"),
    "option unknown element": (_option("initialSelected", True), "unsupported element"),
    "condition has two answers": (_two_answers, "exactly one of"),
    "condition has no answer": (
        lambda q: item(q, "temperature")["enableWhen"][0].pop("answerCoding"),
        "exactly one of",
    ),
    "operator not R4": (_condition(operator="=="), "not an R4 operator"),
    "operator unsupported": (_condition(operator="!="), "only the operator"),
    "answer not a coding": (_condition_answer_boolean, "answerCoding"),
    "condition on a later item": (_later_target, "earlier item"),
    "condition on an unknown item": (_unknown_target, "earlier item"),
    "two conditions without enableBehavior": (_two_conditions(None), "enableBehavior"),
    "enableBehavior any": (_two_conditions("any"), "only enableBehavior 'all'"),
    "CTCAE system typo": (_fever_ctcae(system="urn:hlidac:ctcea:v5.0"), "is not urn:hlidac"),
    "CTCAE system without version": (_fever_ctcae(system="urn:hlidac:ctcae"), "is not urn:hlidac"),
    "CTCAE version not major.minor": (
        _fever_ctcae(system="urn:hlidac:ctcae:v5"),
        "is not urn:hlidac",
    ),
    "other code system": (_fever_ctcae(system="http://snomed.info/sct"), "is not urn:hlidac"),
    "CTCAE coding without code": (_drop_from_ctcae("code"), "a code"),
    "CTCAE coding without term": (_drop_from_ctcae("display"), "names its term"),
    "CTCAE coding with an empty term": (
        _fever_ctcae(system="urn:hlidac:ctcae:v5.0", display=" "),
        "non-empty",
    ),
    "symptom coding without code": (_symptom_without_code, "a code"),
    "two CTCAE versions": (_second_ctcae_version, "one CTCAE version"),
    "linkId with a trailing newline": (_on("note", "linkId", "note\n"), "linkId"),
    "identifier with a trailing newline": (_identifier_value("test-v1\n"), "identifier"),
}


@pytest.mark.parametrize("change, message", STRUCTURAL.values(), ids=STRUCTURAL.keys())
def test_check_questionnaire_rejects(change, message):
    q = questionnaire()
    change(q)
    with pytest.raises(ValueError, match=message):
        plans.check_questionnaire(q)


def test_the_fixture_passes_the_check():
    plans.check_questionnaire(questionnaire())


def test_conditions_with_enable_behaviour_all_are_kept():
    q = questionnaire()
    _two_conditions("all")(q)
    plan = plans.plan_from_questionnaire(q, sidecar())
    assert plan.item("note").enable_when == [
        EnableWhen(link_id="fever", answer_code="yes"),
        EnableWhen(link_id="nausea", answer_code="mild"),
    ]


def test_a_choice_item_without_nevim_is_rejected():
    q = questionnaire()
    item(q, "nausea")["answerOption"].pop()  # dont_know
    with pytest.raises(ValidationError, match="Nevím"):
        plans.plan_from_questionnaire(q, sidecar())


def test_a_condition_on_an_answer_the_item_does_not_offer_is_rejected():
    q = questionnaire()
    item(q, "temperature")["enableWhen"][0]["answerCoding"] = coding("maybe")
    with pytest.raises(ValidationError, match="unknown item or answer"):
        plans.plan_from_questionnaire(q, sidecar())


def test_an_unapproved_plan_stays_labelled_demo():
    q = questionnaire()
    q["description"] = "TEST bez označení"
    with pytest.raises(ValueError, match="DEMO"):
        plans.plan_from_questionnaire(q, sidecar())
    q = questionnaire()
    q["status"] = "active"
    with pytest.raises(ValueError, match="draft"):
        plans.plan_from_questionnaire(q, sidecar())
    with pytest.raises(ValidationError, match="DEMO"):
        sidecar(approval="TEST schváleno")


def test_the_sidecar_is_strict(tmp_path):
    text = PLAN_FILE.read_text(encoding="utf-8")
    for bad in (text + "extra_field: 1\n", text.replace("max_clarification_turns: 3", "")):
        (tmp_path / "bad.plan.yaml").write_text(bad, encoding="utf-8")
        with pytest.raises(ValueError, match="bad.plan.yaml"):
            plans.read_sidecar(tmp_path / "bad.plan.yaml")
    for name in ("../secrets.json", "questionnaire-test.json\n"):
        with pytest.raises(ValidationError):
            sidecar(questionnaire=name)


def test_duplicate_yaml_keys_are_rejected(tmp_path):
    text = PLAN_FILE.read_text(encoding="utf-8")
    (tmp_path / "dup.plan.yaml").write_text(text + "approved_by: someone\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate YAML key 'approved_by'"):
        plans.read_sidecar(tmp_path / "dup.plan.yaml")


def test_duplicate_json_keys_are_rejected(tmp_path):
    raw = Q_FILE.read_text(encoding="utf-8").replace(
        '"status": "draft",', '"status": "draft", "status": "active",'
    )
    (tmp_path / "q.json").write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON key"):
        plans.read_questionnaire(tmp_path / "q.json")


def test_the_bundle_passes_the_fhir_guard():
    q = questionnaire()
    bundle = plans.questionnaire_bundle(q)
    check_bundle(bundle)
    [entry] = bundle["entry"]
    assert entry["request"] == {
        "method": "PUT",
        "url": "Questionnaire?identifier=urn:hlidac:questionnaire|test-v1",
    }
    assert entry["resource"] == q
    assert entry["resource"] is not q


@pytest.mark.parametrize(
    "tamper",
    [
        lambda e: e["resource"]["meta"].update(tag=[]),
        lambda e: e["request"].update(url="Questionnaire?identifier=urn:hlidac:questionnaire|x"),
        lambda e: e["resource"].update(identifier=[{"system": "urn:other", "value": "test-v1"}]),
    ],
    ids=["no tag", "another identifier", "identifier not ours"],
)
def test_a_tampered_bundle_fails_the_fhir_guard(tamper):
    bundle = plans.questionnaire_bundle(questionnaire())
    tamper(bundle["entry"][0])
    with pytest.raises(ValueError):
        check_bundle(bundle)


def test_enqueueing_twice_writes_one_outbox_row():
    store = InMemoryStore()
    q = questionnaire()
    assert plans.enqueue_questionnaire(store, q, T0) is True
    assert plans.enqueue_questionnaire(store, copy.deepcopy(q), T0) is False
    [row] = store.list_outbox()
    assert row.idempotency_key == "questionnaire:urn:hlidac:questionnaire:test|1"
    assert row.status == "pending"
    assert row.job.bundle() == plans.questionnaire_bundle(q)


def _data_dir(tmp_path: Path, *plan_names: str) -> Path:
    (tmp_path / "plans").mkdir()
    (tmp_path / "questionnaires").mkdir()
    shutil.copy(Q_FILE, tmp_path / "questionnaires" / Q_FILE.name)
    for name in plan_names:
        shutil.copy(PLAN_FILE, tmp_path / "plans" / name)
    return tmp_path


def test_load_plans_keys_by_url_and_version(tmp_path):
    loaded = plans.load_plans(_data_dir(tmp_path, "test.plan.yaml"))
    assert list(loaded) == [("urn:hlidac:questionnaire:test", "1")]


def test_load_plans_rejects_two_plans_for_one_questionnaire(tmp_path):
    with pytest.raises(ValueError, match="another plan"):
        plans.load_plans(_data_dir(tmp_path, "a.plan.yaml", "b.plan.yaml"))


def test_load_plans_without_plans_is_empty(tmp_path):
    assert plans.load_plans(tmp_path) == {}
    assert plans.load_plans(_data_dir(tmp_path)) == {}


def test_the_real_data_dir_loads():
    loaded = plans.load_plans()
    for (url, version), plan in loaded.items():
        assert (plan.questionnaire_url, plan.questionnaire_version) == (url, version)


def test_the_app_uses_the_plans_from_data_by_default():
    app = create_app(Settings(_env_file=None))
    assert app.state.pipeline.plans == plans.load_plans()


def test_the_cli_prints_counts_only(tmp_path, capsys):
    assert plans.main(["check", "--data-dir", str(_data_dir(tmp_path, "test.plan.yaml"))]) == 0
    out = capsys.readouterr().out
    assert out.strip() == ("plans: 1, items: 4, red flags: 1, with CTCAE term: 1, conditional: 1")
    assert "TEST" not in out
    (tmp_path / "plans" / "broken.plan.yaml").write_text("questionnaire: 1\n", encoding="utf-8")
    assert plans.main(["check", "--data-dir", str(tmp_path)]) == 1
