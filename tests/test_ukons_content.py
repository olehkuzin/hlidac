"""The verbatim UKONS v3 triage ratings (WP2 part 2) are complete and labelled with their source."""

import pathlib
import re

import pytest
import yaml

UKONS = pathlib.Path(__file__).resolve().parents[1] / "data" / "ctcae" / "ukons-v3.yaml"
# Kept out of git until the UKONS licence is settled (D32), so it exists only on some laptops.
pytestmark = pytest.mark.skipif(not UKONS.exists(), reason="ukons-v3.yaml is local only (D32)")
RATINGS = {"green", "amber", "red"}
# Demo symptom -> the UKONS row that covers it. Chills/rigors have no row (documented in not_in_tool).
DEMO_ROWS = {
    "fever": "5. Fever on SACT",
    "nausea": "12. Nausea",
    "vomiting": "13. Vomiting",
    "diarrhoea": "14. Diarrhoea",
}


@pytest.fixture(scope="module")
def content():
    return yaml.safe_load(UKONS.read_text(encoding="utf-8"))


def test_source_and_approval_label(content):
    src = content["source"]
    assert src["url"].startswith("https://www.ukacuteoncology.co.uk/")
    assert re.fullmatch(r"[0-9a-f]{64}", src["sha256"])
    assert src["version"] == "3" and src["title"] and src["retrieved"]
    assert any("cannot be reproduced" in t["quote"] for t in src["terms_of_use"])
    assert content["approval"] == "DEMO — čeká na schválení FN Motol"


def test_every_grade_has_text_and_a_rag_rating(content):
    for s in content["symptoms"]:
        assert s["ukons_symptom"] and isinstance(s["page"], int)
        assert [g["grade"] for g in s["grades"]] == [0, 1, 2, 3, 4]
        for g in s["grades"]:
            assert g["text"].strip()
            assert g["rating"] in RATINGS


def test_demo_symptoms_present_or_documented_absent(content):
    rows = {s["ukons_symptom"] for s in content["symptoms"]}
    assert set(DEMO_ROWS.values()) <= rows
    absent = {a["demo_symptom"] for a in content["not_in_tool"]}
    assert "chills / rigors" in absent


def test_action_rules_cover_every_rating(content):
    rules = content["action_rules"]
    assert {r["rating"] for r in rules} >= RATINGS | {"amber_plus"}
    assert all(r["text"].strip() and isinstance(r["page"], int) for r in rules)
