"""The demo content fits together (WP1 + WP2): the Questionnaire, its plan, the CTCAE mapping, the
verbatim CTCAE file and the UKONS file. A typo in one of them must fail here, not in the demo."""

import json
import pathlib

import pytest
import yaml

from hlidac.plans import load_plan, load_plans, questionnaire_bundle
from hlidac.schemas import DONT_KNOW
from hlidac.wizard import NO

DATA = pathlib.Path(__file__).resolve().parents[1] / "data"
PLAN_FILE = DATA / "plans" / "symptoms-demo-v1.plan.yaml"
QUESTIONNAIRE = DATA / "questionnaires" / "symptoms-demo-v1.json"
YES_NO = {"yes", NO, DONT_KNOW}


def _yaml(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def plan():
    return load_plan(PLAN_FILE)


@pytest.fixture(scope="module")
def mapping():
    return _yaml(DATA / "ctcae" / "mapping-demo-v1.yaml")


@pytest.fixture(scope="module")
def ctcae(mapping):
    doc = _yaml(DATA / "ctcae" / mapping["ctcae"]["file"])
    assert doc["ctcae_version"] == mapping["ctcae"]["version"]
    return {t["term"]: t for t in doc["terms"]}


def _conditions(when):
    """Every leaf condition in a `when`, flattened."""
    if "all" in when:
        return [leaf for sub in when["all"] for leaf in _conditions(sub)]
    return [when]


def _all_whens(mapping):
    for m in mapping["mappings"]:
        for proposal in m["proposals"]:
            yield proposal["when"]
    for row in mapping["ukons_ratings"]:
        for band in row["bands"]:
            yield band["when"]


def test_the_app_loads_the_demo_plan(plan):
    assert (plan.questionnaire_url, plan.questionnaire_version) in load_plans()
    assert plan.questionnaire_url == "urn:hlidac:questionnaire:demo"  # dev_seed's patients use it
    assert plan.approved_by is None


def test_the_questionnaire_goes_to_iris_as_a_guarded_bundle():
    bundle = questionnaire_bundle(json.loads(QUESTIONNAIRE.read_text(encoding="utf-8")))
    assert bundle["entry"][0]["request"]["method"] == "PUT"


def test_no_means_only_symptom_denied(plan):
    """D30: the answer code `no` records `absent`, so it may only appear on a plain
    "do you have this symptom" question (Ano / Ne / Nevím)."""
    for item in plan.items:
        codes = {o.code for o in item.options}
        if NO in codes:
            assert codes == YES_NO, item.link_id


def test_red_flags_are_the_fever_and_bleeding_questions(plan):
    red = {i.link_id for i in plan.items if i.red_flag}
    assert red == {"fever", "unwell", "chills", "diarrhoea_blood"}


def test_red_flags_are_always_asked(plan):
    """A conditional red flag would go unasked after "Nevím" to its parent question."""
    assert all(not i.enable_when for i in plan.items if i.red_flag)


RANK = {"green": 0, "amber": 1, "red": 2}


def _fallback(whens, parent, follow_up):
    """The `when` for "symptom present, follow-up unanswered", if there is one."""
    target = [{"answer": {"link_id": parent, "codes": ["yes"]}}, {"no_value": follow_up}]
    return next((w for w in whens if w.get("all") == target), None)


def _follow_ups(plan, whens):
    """The conditional items whose answers the conditions read, with their parent item."""
    for when in whens:
        for leaf in _conditions(when):
            item = plan.item(leaf["answer"]["link_id"]) if "answer" in leaf else None
            if item is not None and item.enable_when:
                yield item.link_id, item.enable_when[0].link_id


def test_an_unanswered_follow_up_still_gets_a_grade(plan, mapping):
    """Principle 2: "Ano" + "Nevím" (or no answer) on the follow-up still proposes a grade."""
    for m in mapping["mappings"]:
        whens = [p["when"] for p in m["proposals"]]
        for follow_up, parent in _follow_ups(plan, whens):
            assert _fallback(whens, parent, follow_up) is not None, (m["mapping_id"], follow_up)


def test_an_unanswered_follow_up_never_rates_below_its_lowest_band(plan, mapping):
    """UKONS diarrhoea starts at amber, so "Ano" + "Nevím" must be at least amber, not unrated."""
    for row in mapping["ukons_ratings"]:
        whens = [b["when"] for b in row["bands"]]
        rating = {id(b["when"]): RANK[b["rating"]] for b in row["bands"]}
        for follow_up, parent in set(_follow_ups(plan, whens)):
            lowest = min(rating[id(w)] for w in whens if (follow_up, parent) in _follow_ups(plan, [w]))
            if lowest > RANK["green"]:
                fallback = _fallback(whens, parent, follow_up)
                assert fallback is not None and rating[id(fallback)] >= lowest, follow_up


def test_every_ctcae_item_has_a_mapping_with_the_official_code(plan, mapping, ctcae):
    by_link = {m["link_id"]: m for m in mapping["mappings"]}
    graded = {i.link_id: i.ctcae_term for i in plan.items if i.ctcae_term}
    assert graded.keys() == by_link.keys()
    raw = json.loads(QUESTIONNAIRE.read_text(encoding="utf-8"))
    codes = {
        item["linkId"]: c["code"]
        for item in raw["item"]
        for c in item.get("code", [])
        if c["system"].startswith("urn:hlidac:ctcae")
    }
    for link_id, term in graded.items():
        m = by_link[link_id]
        assert m["ctcae_term"] == term
        assert m["meddra_code"] == ctcae[term]["meddra_code"] == codes[link_id]


def test_plan_lists_exactly_the_mappings(plan, mapping):
    assert plan.mapping_ids == [m["mapping_id"] for m in mapping["mappings"]]
    assert mapping["questionnaire"] == {
        "url": plan.questionnaire_url,
        "version": plan.questionnaire_version,
    }


def test_every_proposal_cites_an_existing_ctcae_grade(mapping, ctcae):
    for m in mapping["mappings"]:
        for proposal in m["proposals"]:
            assert ("grade" in proposal) != ("at_least" in proposal), m["mapping_id"]
            level = proposal.get("grade", proposal.get("at_least"))
            criterion = proposal["criterion"]
            assert criterion == {"term": m["ctcae_term"], "grade": level}
            assert ctcae[m["ctcae_term"]]["grades"][str(level)] != "-"
            if "at_least" in proposal:
                assert proposal["why_at_least"]


def test_conditions_name_real_items_and_answers(plan, mapping):
    for when in _all_whens(mapping):
        for leaf in _conditions(when):
            ((kind, condition),) = leaf.items()
            if kind == "answer":
                options = {o.code for o in plan.item(condition["link_id"]).options}
                assert set(condition["codes"]) <= options - {DONT_KNOW}, condition
            elif kind == "value":
                item = plan.item(condition["link_id"])
                assert item.type == "quantity" and item.unit == condition["unit"]
            else:
                assert kind == "no_value"
                assert plan.item(condition).type != "text"


def test_ukons_rows_exist_with_the_same_rating(mapping):
    path = DATA / "ctcae" / mapping["ukons"]["file"]
    if not path.exists():
        pytest.skip("ukons-v3.yaml is local only until the UKONS licence is settled (D32)")
    ukons = _yaml(path)
    rows = {s["ukons_symptom"]: s for s in ukons["symptoms"]}
    for row in mapping["ukons_ratings"]:
        source = rows[row["ukons_symptom"]]
        assert source["page"] == row["page"]
        ratings = {g["grade"]: g["rating"] for g in source["grades"]}
        for band in row["bands"]:
            if "ukons_grade" in band:
                assert ratings[band["ukons_grade"]] == band["rating"], (row, band)
