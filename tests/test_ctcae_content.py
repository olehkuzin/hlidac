"""The verbatim CTCAE content (WP2) is complete and labelled with its source."""

import pathlib
import re

import pytest
import yaml

CTCAE = pathlib.Path(__file__).resolve().parents[1] / "data" / "ctcae"
TERMS = {"Fever", "Chills", "Nausea", "Vomiting", "Diarrhea"}


@pytest.fixture(params=["5.0", "6.0"])
def content(request):
    doc = yaml.safe_load((CTCAE / f"ctcae-v{request.param}.yaml").read_text(encoding="utf-8"))
    assert doc["ctcae_version"] == request.param
    return doc


def test_source_and_approval_label(content):
    src = content["source"]
    assert src["url"].startswith("https://dctd.cancer.gov/")
    assert re.fullmatch(r"[0-9a-f]{64}", src["sha256"])
    assert src["sheet"] and src["title"]
    assert content["approval"] == "DEMO — čeká na schválení FN Motol"


def test_demo_terms_present_once_with_all_grades(content):
    names = [t["term"] for t in content["terms"]]
    assert len(names) == len(set(names))
    assert set(names) == TERMS
    for t in content["terms"]:
        assert set(t["grades"]) == {"1", "2", "3", "4", "5"}
        assert all(isinstance(g, str) and g.strip() for g in t["grades"].values())
        assert t["definition"].strip() and re.fullmatch(r"\d{8}", t["meddra_code"])
