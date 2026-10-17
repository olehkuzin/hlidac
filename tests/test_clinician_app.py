"""WP13: the nurse queue, the case view and the unresolved list, on the in-memory store."""

from datetime import timedelta

import pytest
from factories import T0, assessment, email_item, grade, urgent_rule
from patient_helpers import make_app, urgent_on_fever
from security.auth_helpers import NURSE, PATIENT_A, PATIENT_B, csrf, login, new_client

from hlidac import audit, processing
from hlidac.schemas import Account
from hlidac.ui.clinician import _basis_line, readable

OTHER_NURSE = Account(
    account_id="acc-n2", email="sestra2@demo.local", role="nurse", display_name="N2", team_id="t2"
)


def logged_in(app, mailbox, account):
    client = new_client(app)
    login(client, mailbox, account.email)
    return client


def post(client, path, page, **data):
    return client.post(path, data={"csrf_token": csrf(client, page), **data})


def urgent_case():
    """Patient B reports fever: an urgent assessment and alert (the WP4 stand-in)."""
    app, store, mailbox = make_app(process=urgent_on_fever)
    store.save_account(OTHER_NURSE)
    patient = logged_in(app, mailbox, PATIENT_B)
    base = patient.post("/pacient/hlaseni", data={"csrf_token": csrf(patient, "/pacient")}).headers[
        "location"
    ]
    post(patient, f"{base}/otazka/fever", f"{base}/otazka/fever", answer="yes")
    [alert] = store.list_alerts()
    nurse = logged_in(app, mailbox, NURSE)
    return app, store, mailbox, nurse, alert


def test_queue_shows_the_urgent_alert_with_its_reason_and_provisional_grade():
    _, _, _, nurse, alert = urgent_case()
    page = nurse.get("/sestra").text
    assert "Naléhavé" in page and "⚠" in page  # icon and word, not colour alone
    assert "Horečka do 56 dnů od léčby (DEMO)" in page
    assert "předběžný" in page
    assert f"/sestra/pripad/{alert.alert_id}" in page


def test_case_view_shows_rule_versions_answers_and_the_provisional_grade():
    _, store, _, nurse, alert = urgent_case()
    page = nurse.get(f"/sestra/pripad/{alert.alert_id}").text
    assert "pravidlo fever-sact v1" in page
    assert "Předběžný, navržený systémem" in page
    assert "DEMO criterion" in page  # the grade's basis
    assert "tlačítko" in page  # where each answer came from
    assert any(e.action == "view_case" for e in store.list_audit(f"alert:{alert.alert_id}"))


def test_case_view_names_items_in_czech_and_keeps_the_stored_reason():
    reason = "UKONS: 5. Fever on SACT – červené hodnocení (stupeň –)"
    assert readable(reason) == "UKONS: 5. Fever on SACT – červené hodnocení"  # CTCAE/UKONS kept
    assert readable("Chybí odpověď (unwell, diarrhoea_blood)") == (
        "Chybí odpověď (celkově zle, krev nebo hlen ve stolici)"
    )
    line = _basis_line("Reported: temperature: 38.7 Cel (not yet confirmed)", {})
    assert line.startswith("Pacient uvedl – teplota: 38,7 ")
    assert "nepotvrdil" in line and "nepočítá" in line
    assert _basis_line("CTCAE v5.0 Fever grade 1 (NCI): 38.0 - 39.0", {}).endswith("38.0 - 39.0")


def test_case_view_shows_who_took_the_alert_by_name():
    _, _, _, nurse, alert = urgent_case()
    path = f"/sestra/pripad/{alert.alert_id}"
    post(nurse, f"{path}/prevzit", path)
    page = nurse.get(path).text
    assert f"({NURSE.display_name}, " in page and f"({NURSE.account_id}, " not in page


def test_patient_report_is_queued_for_fhir():
    _, store, _, _, _ = urgent_case()
    keys = [item.idempotency_key for item in store.list_outbox()]
    assert any(key.startswith("checkin:") for key in keys)


def test_acknowledge_records_who_and_enqueues_the_task_update():
    _, store, _, nurse, alert = urgent_case()
    case = f"/sestra/pripad/{alert.alert_id}"
    assert post(nurse, f"{case}/prevzit", case).status_code == 303
    saved = store.get_alert(alert.alert_id)
    assert (saved.status, saved.acknowledged_by) == ("acknowledged", NURSE.account_id)
    keys = [i.idempotency_key for i in store.list_outbox()]
    assert f"alert:{alert.alert_id}:acknowledged" in keys
    assert "Převzato" in nurse.get(case).text


def test_confirming_the_proposed_grade_keeps_the_engine_grade_and_adds_a_review():
    _, store, _, nurse, alert = urgent_case()
    case = f"/sestra/pripad/{alert.alert_id}"
    response = post(nurse, f"{case}/stupen/g1", case, grade="1")
    assert response.status_code == 303
    [review] = store.list_grade_reviews(alert.assessment_id)
    assert (review.grade.status, review.grade.confirmed_by) == ("confirmed", NURSE.account_id)
    assert store.get_assessment(alert.assessment_id).grades[0].status == "provisional"
    assert f"grade-review:{review.review_id}" in [i.idempotency_key for i in store.list_outbox()]
    page = nurse.get(case).text
    assert "Stav: Potvrzený" in page
    assert 'role="status"' in page and "Stupeň 1 potvrzen." in page
    assert "Stupeň 1 potvrzen." not in nurse.get(case).text  # shown once


def test_a_correction_needs_a_reason():
    _, store, _, nurse, alert = urgent_case()
    case = f"/sestra/pripad/{alert.alert_id}"
    response = post(nurse, f"{case}/stupen/g1", case, grade="3")
    assert response.status_code == 400
    assert "Napište, proč stupeň opravujete." in response.text
    assert store.list_grade_reviews(alert.assessment_id) == []
    # The nurse's choice and note are kept, and the error sits on the note (WCAG 3.3.7, 3.3.1).
    assert 'value="3" checked' in response.text
    assert 'aria-invalid="true" aria-describedby="err-note-g1" autofocus' in response.text
    post(nurse, f"{case}/stupen/g1", case, grade="3", note="TEST důvod")
    [review] = store.list_grade_reviews(alert.assessment_id)
    assert (review.grade.status, review.grade.grade, review.note) == ("corrected", 3, "TEST důvod")


@pytest.mark.parametrize("value", ["", "0", "6", "x"])
def test_grade_must_be_one_to_five(value):
    _, store, _, nurse, alert = urgent_case()
    case = f"/sestra/pripad/{alert.alert_id}"
    assert post(nurse, f"{case}/stupen/g1", case, grade=value).status_code == 400
    assert store.list_grade_reviews(alert.assessment_id) == []


def test_resolve_needs_a_note_and_records_both_steps():
    _, store, _, nurse, alert = urgent_case()
    case = f"/sestra/pripad/{alert.alert_id}"
    assert post(nurse, f"{case}/vyresit", case).status_code == 400
    post(nurse, f"{case}/vyresit", case, note="TEST zavoláno")
    saved = store.get_alert(alert.alert_id)
    assert (saved.status, saved.resolved_by, saved.acknowledged_by) == (
        "resolved",
        NURSE.account_id,
        NURSE.account_id,
    )
    assert "Žádná nová upozornění." in nurse.get("/sestra").text


def test_nurse_of_another_team_cannot_see_or_act_on_the_case():
    app, store, mailbox, _, alert = urgent_case()
    other = logged_in(app, mailbox, OTHER_NURSE)
    case = f"/sestra/pripad/{alert.alert_id}"
    assert alert.alert_id not in other.get("/sestra").text
    assert other.get(case).status_code == 403
    token = csrf(other, "/sestra")
    for action, data in (
        ("prevzit", {}),
        ("stupen/g1", {"grade": "1"}),
        ("vyresit", {"note": "x"}),
    ):
        assert other.post(f"{case}/{action}", data={"csrf_token": token, **data}).status_code == 403
    assert store.get_alert(alert.alert_id).status == "open"
    assert store.list_grade_reviews(alert.assessment_id) == []


def test_patients_cannot_open_clinician_pages():
    app, _, mailbox, _, alert = urgent_case()
    patient = logged_in(app, mailbox, PATIENT_B)
    for path in ("/sestra", f"/sestra/pripad/{alert.alert_id}", "/sestra/nevyresene"):
        assert patient.get(path).status_code == 403


def test_unresolved_lists_failed_unassessed_and_partial_reports():
    app, _, mailbox = make_app()  # no engine: a sent report stays unassessed
    patient = logged_in(app, mailbox, PATIENT_A)
    base = patient.post("/pacient/hlaseni", data={"csrf_token": csrf(patient, "/pacient")}).headers[
        "location"
    ]
    post(patient, f"{base}/odeslat", f"{base}/odeslat")
    nurse = logged_in(app, mailbox, NURSE)
    page = nurse.get("/sestra/nevyresene").text
    assert "Odeslané, nikdo ho nevyhodnotil" in page

    patient.post("/pacient/hlaseni", data={"csrf_token": csrf(patient, "/pacient")})
    assert "Rozpracované, neodeslané" in nurse.get("/sestra/nevyresene").text


def test_unresolved_shows_failed_emails_without_body_or_address():
    app, store, mailbox = make_app()
    item = email_item()
    store.enqueue(item)
    store.update_outbox_item(
        item.evolve(
            status="failed", attempts=5, last_error="SMTPError: sestra@demo.local", processed_at=T0
        )
    )
    nurse = logged_in(app, mailbox, NURSE)
    page = nurse.get("/sestra/nevyresene").text
    assert "tpl-new-alert" in page and "SMTPError" in page
    assert "sestra@demo.local" not in page.replace(NURSE.email, "")
    assert item.job.text_body not in page


def test_unresolved_lists_an_alert_whose_notification_failed():
    _, store, _, nurse, alert = urgent_case()
    assert "Žádná neodeslaná oznámení" in nurse.get("/sestra/nevyresene").text
    audit.record(store, T0, "system", "notification_failed", target=f"alert:{alert.alert_id}",
                 error="ConnectionError")  # fmt: skip
    page = nurse.get("/sestra/nevyresene").text
    assert "Neodeslaná oznámení (1)" in page and f"/sestra/pripad/{alert.alert_id}" in page
    audit.record(store, T0, "system", "notification_completed", target=f"alert:{alert.alert_id}")
    assert "Neodeslaná oznámení (0)" in nurse.get("/sestra/nevyresene").text


# --- symptom timeline (WP24) and grouped reports (WP18) --------------------------------------


def grouped_case():
    """Patient B's urgent alert plus a later report of the same episode grouped into it, and an
    older report without an alert."""
    _, store, _, nurse, alert = urgent_case()
    first = store.get_assessment(alert.assessment_id)
    patient_id = alert.patient_id
    older = assessment(
        assessment_id="a-old", checkin_id="c-old", patient_id=patient_id,
        assessed_at=T0 - timedelta(days=2), grades=[grade(grade_id="g-old", grade=1)],
    )  # fmt: skip
    later = assessment(
        assessment_id="a-later", checkin_id="c-later", patient_id=patient_id, status="urgent",
        triggered_rules=[urgent_rule()], assessed_at=first.assessed_at + timedelta(hours=1),
        grades=[grade(grade_id="g-later", grade=2)],
    )  # fmt: skip
    store.add_assessment(older)
    store.add_assessment(later)
    audit.record(store, T0, "system", "alert_grouped", target=f"alert:{alert.alert_id}",
                 assessment="a-later", status="urgent", checkin="c-later")  # fmt: skip
    return store, nurse, alert


def test_queue_counts_later_reports_grouped_into_the_alert():
    _, nurse, _ = grouped_case()
    assert "1 další hlášení téže epizody" in nurse.get("/sestra").text


def test_case_view_shows_the_symptom_timeline_with_grouped_reports():
    _, nurse, alert = grouped_case()
    page = nurse.get(f"/sestra/pripad/{alert.alert_id}").text
    timeline = page[page.index('id="prubeh"'):page.index("Co pacient uvedl")]
    assert "Průběh příznaků podle stupně CTCAE" in timeline
    assert "Nausea" in timeline and "stupeň 2 (předběžný)" in timeline
    assert "přičleněno k tomuto upozornění" in timeline
    assert 'aria-current="true"' in timeline and "toto hlášení" in timeline
    assert "bez upozornění" in timeline
    assert timeline.index("bez upozornění") < timeline.index("toto hlášení")  # oldest first
    assert "Další hlášení téže epizody" in page


def test_grouped_report_tells_the_patient_the_team_knows():
    store, _, alert = grouped_case()
    later = store.get_assessment("a-later")
    assert processing.alert_of(store, later) == alert
