"""D50: illness groups. One questionnaire per large cancer group, assigned by ICD-10 code; the
group items can only raise urgency. Invented answers, no patient data."""

import pytest
from demo_helpers import ALL_CLEAR, MAPPING, NOW, RULES, context

from hlidac import assessment as engine
from hlidac import groups as groups_mod
from hlidac import wizard
from hlidac.plans import load_plans

GROUPS = groups_mod.read_groups()
PLANS = load_plans()


def group_plan(group_id: str):
    group = next(g for g in GROUPS.groups if g.group_id == group_id)
    return PLANS[(group.questionnaire_url, "1")]


def run(plan, answers: dict[str, str]):
    found = []
    for link_id, code in answers.items():
        built = wizard.finding_from_answer(wizard.button_answer(plan.item(link_id), code), "m1")
        found.append(built.evolve(confirmed=True))
    return engine.assess(
        assessment_id="a1", checkin_id="c1", patient_id="p1", findings=found, plan=plan,
        mapping=MAPPING, rules=RULES, context=context(), now=NOW,
    )  # fmt: skip


def all_denied(plan) -> dict[str, str]:
    """The core all-clear plus "no" to every group item of this plan."""
    group = GROUPS.by_url(plan.questionnaire_url)
    return {**ALL_CLEAR, **{i.link_id: "no" for i in group.items}}


def test_the_generated_files_match_the_groups_file():
    assert groups_mod.main(["check"]) == 0


def test_every_group_has_a_plan_with_the_core_items_first():
    core = PLANS[("urn:hlidac:questionnaire:demo", "1")]
    core_ids = [i.link_id for i in core.items]
    for group in GROUPS.groups:
        plan = PLANS[(group.questionnaire_url, "1")]
        ids = [i.link_id for i in plan.items]
        assert ids[: len(core_ids) - 1] == core_ids[:-1]
        assert ids[-1] == "other"
        assert {i.link_id for i in group.items} <= set(ids)
        engine.check_content(plan, MAPPING, RULES)


@pytest.mark.parametrize(
    ("codes", "group_id"),
    [
        (["C50.9"], "breast"),
        (["C509"], "breast"),
        (["C16.9"], "upper_gi"),
        (["C18.7"], "digestive"),
        (["C20"], "digestive"),
        (["C25.0"], "liver_pancreas"),
        (["C34.1"], "lung"),
        (["C45.0"], "lung"),
        (["D38.1"], "lung"),
        (["C61"], "prostate"),
        (["C62.9"], "testis"),
        (["C64"], "urological"),
        (["C67.9"], "urological"),
        (["C53.9"], "gynaecological"),
        (["C54.1"], "uterus"),
        (["C56"], "ovary"),
        (["C83.3"], "haematological"),
        (["C91.0"], "leukaemia_myeloma"),
        (["C90.0"], "leukaemia_myeloma"),
        (["C01"], "head_neck"),
        (["C73"], "thyroid"),
        (["C01", "C02", "C73"], "head_neck"),  # most codes win
        (["C71.9"], "brain"),
        (["C43.5"], "skin"),
        (["C44.3"], "skin_other"),
        (["C40.2"], "bone"),
        (["C49.2"], "sarcoma"),
        (["C61", "C18", "C20"], "digestive"),  # most codes win
        (["D48.1"], None),
        (["I10"], None),
        ([], None),
        (["garbage"], None),
    ],
)
def test_group_for_codes(codes, group_id):
    group = groups_mod.group_for_codes(GROUPS, codes)
    assert (group.group_id if group else None) == group_id


def test_overlapping_blocks_are_rejected():
    raw = GROUPS.model_dump()
    raw["groups"][1]["icd10"] = ["C50-C52"]
    with pytest.raises(ValueError, match="overlap"):
        groups_mod.Groups.model_validate(raw)


def test_all_denied_in_a_group_questionnaire_is_no_escalation():
    plan = group_plan("breast")
    a = run(plan, all_denied(plan))
    assert a.status == "no_escalation", a.triggered_rules


@pytest.mark.parametrize("group", GROUPS.groups, ids=lambda g: g.group_id)
def test_a_yes_to_any_group_item_is_never_green(group):
    plan = group_plan(group.group_id)
    for item in group.items:
        a = run(plan, {**all_denied(plan), item.link_id: "yes"})
        assert a.status in ("routine_review", "urgent"), item.link_id
        expected = "red-flag-present" if item.red_flag else "group-symptom-reported"
        assert expected in {t.rule_id for t in a.triggered_rules}


@pytest.mark.parametrize("group", GROUPS.groups, ids=lambda g: g.group_id)
def test_dont_know_or_no_answer_to_a_group_item_is_insufficient(group):
    plan = group_plan(group.group_id)
    item = group.items[0]
    for answers in ({**all_denied(plan), item.link_id: "dont_know"},
                    {k: v for k, v in all_denied(plan).items() if k != item.link_id}):  # fmt: skip
        a = run(plan, answers)
        assert a.status != "no_escalation" and item.link_id in a.missing_link_ids


def test_the_general_questionnaire_never_fires_the_group_rule():
    core = PLANS[("urn:hlidac:questionnaire:demo", "1")]
    a = run(core, {**ALL_CLEAR, "nausea": "yes", "nausea_intake": "intake_usual"})
    assert a.status == "no_escalation"
    assert "group-symptom-reported" not in {t.rule_id for t in a.triggered_rules}


def test_demo_records_assign_each_patient_their_group_questionnaire():
    from hlidac.iris.demo_patients import demo_records

    codes = {"invented-1": ["C50.9"], "invented-2": ["I10"], "invented-3": ["C34.1", "C34.9"]}
    accounts, plans = demo_records(["invented-1", "invented-2", "invented-3"], NOW, codes, GROUPS)
    assert [p.questionnaire_url for p in plans] == [
        "urn:hlidac:questionnaire:group-breast",
        "urn:hlidac:questionnaire:demo",
        "urn:hlidac:questionnaire:group-lung",
    ]
    for plan in plans:  # every assigned questionnaire has a loaded plan
        assert (plan.questionnaire_url, plan.questionnaire_version) in PLANS
    emails = [a.email for a in accounts if a.role == "patient"]
    assert len(set(emails)) == 3 and all("invented" not in e for e in emails)


def test_pick_per_group_keeps_order_and_limit():
    from hlidac.iris.demo_patients import pick_per_group

    codes = {"a": ["C50"], "b": ["C50.1"], "c": ["C61"], "d": ["C50.2"], "e": ["I10"]}
    assert pick_per_group(["a", "b", "c", "d", "e"], codes, GROUPS, 2) == ["a", "b", "c"]


def test_groups_page_lists_the_teams_patients_by_group_and_their_questions():
    from security.auth_helpers import PATIENT_B
    from test_clinician_app import NURSE, OTHER_NURSE, logged_in, urgent_case

    app, store, mailbox, nurse, _ = urgent_case()
    plan = store.get_patient_plan(PATIENT_B.patient_id)
    store.save_patient_plan(plan.evolve(questionnaire_url="urn:hlidac:questionnaire:group-lung"))
    orphan = plan.evolve(patient_id="invented-no-account")  # a plan from an earlier run
    store.save_patient_plan(orphan)
    page = nurse.get("/sestra/skupiny").text
    assert "invented-no-account" not in page
    lung = page.split('id="group-lung"')[1].split("<h2")[0]
    assert PATIENT_B.email in lung and "Objevila se při kašli krev?" in lung
    assert "varovný příznak" in lung
    assert any(e.action == "view_groups" for e in store.list_audit())
    assert "Nádory plic a hrudníku" in nurse.get("/sestra").text  # the queue's group column
    other = logged_in(app, mailbox, OTHER_NURSE)  # another team never sees our patients
    assert PATIENT_B.email not in other.get("/sestra/skupiny").text
    patient = logged_in(app, mailbox, PATIENT_B)
    assert patient.get("/sestra/skupiny").status_code == 403
    assert NURSE.team_id != OTHER_NURSE.team_id


def test_live_group_check_in_asks_the_group_questions_and_a_yes_reaches_the_nurse():
    from security.auth_helpers import csrf
    from test_processing import answer, demo_app, signed_in, start

    app, mailbox = demo_app()
    store = app.state.store
    plan = store.get_patient_plan("demo-patient-b")
    store.save_patient_plan(plan.evolve(questionnaire_url="urn:hlidac:questionnaire:group-lung"))
    patient = signed_in(app, mailbox, "pacient.b@demo.local")
    base = start(patient)
    lung = group_plan("lung")
    answers = {**all_denied(lung), "lung_cough": "yes"}
    assert "Objevila se při kašli krev?" in patient.get(f"{base}/otazka/lung_blood_cough").text
    for link_id, value in answers.items():
        answer(patient, base, link_id, value)
    summary = f"{base}/odeslat"
    result = patient.post(summary, data={"csrf_token": csrf(patient, summary)})
    page = patient.get(result.headers["location"]).text
    assert "Zavolejte hned" not in page
    [saved] = store.list_assessments("demo-patient-b")
    assert saved.questionnaire_url == "urn:hlidac:questionnaire:group-lung"
    assert saved.status == "routine_review", saved.triggered_rules
    assert "group-symptom-reported" in {t.rule_id for t in saved.triggered_rules}
    assert len(store.list_alerts()) == 1
    nurse = signed_in(app, mailbox, "sestra@demo.local")
    queue = nurse.get("/sestra").text
    assert "Nádory plic a hrudníku" in queue and "nový nebo silnější kašel" in queue


def test_an_item_shared_by_groups_reads_the_same_everywhere():
    """D51: a link id reused across groups (e.g. gi_tingling) has one label and one question, so
    the nurse screens and the timeline name it the same for every group."""
    seen: dict[str, tuple[str, str]] = {}
    for group in GROUPS.groups:
        for item in group.items:
            assert seen.setdefault(item.link_id, (item.label_cs, item.text)) == (
                item.label_cs,
                item.text,
            ), item.link_id
