"""WP0 + WP10: every Store behaves the same. Runs on InMemoryStore, and on IrisStore with
HLIDAC_IRIS_TESTS=1 (conftest.py)."""

import re
from datetime import UTC, timedelta
from importlib import resources

import pytest
from factories import (
    T0,
    alert,
    answered,
    assessment,
    audit,
    checkin,
    email_item,
    fhir_item,
    grade,
    patient_account,
)

from hlidac.schemas import Account, Delivery, GradeReview, LoginCode, Notification, PatientPlan
from hlidac.store import DuplicateError, StaticContextSource


def test_accounts_found_by_email_case_insensitively(store):
    store.save_account(patient_account())
    assert store.find_account_by_email(" Pacient1@Demo.local ").account_id == "acc-p1"
    with pytest.raises(DuplicateError):
        store.save_account(patient_account(account_id="acc-other"))


def test_ids_are_case_sensitive(store):
    store.save_account(patient_account(account_id="acc-A", email="a@demo.local", patient_id="pA"))
    store.save_account(patient_account(account_id="acc-a", email="b@demo.local", patient_id="pa"))
    assert store.get_account("acc-A").email == "a@demo.local"
    assert store.get_account("acc-a").email == "b@demo.local"
    store.save_checkin(checkin(checkin_id="cA", patient_id="pA"))
    assert store.list_checkins("pa") == []


def test_an_account_can_change_its_email(store):
    store.save_account(patient_account())
    store.save_account(patient_account(email="novy@demo.local"))
    assert store.find_account_by_email("pacient1@demo.local") is None
    assert store.get_account("acc-p1").email == "novy@demo.local"


def test_checkins_are_saved_after_every_answer(store):
    store.save_checkin(checkin())
    store.save_checkin(checkin(updated_at=T0 + timedelta(minutes=1), pending_link_id="fever"))
    assert store.get_checkin("c1").pending_link_id == "fever"
    assert [c.checkin_id for c in store.list_checkins("p1")] == ["c1"]
    assert store.list_checkins("p2") == []


def test_records_come_back_unchanged(store):
    started = checkin(findings=[answered()], status="completed")
    store.save_checkin(started)
    assert store.get_checkin("c1") == started
    store.add_assessment(assessment())
    assert store.get_assessment("a1") == assessment()
    plan = PatientPlan(
        patient_id="p1",
        questionnaire_url="urn:hlidac:questionnaire:demo",
        questionnaire_version="1",
        care_team_id="team-onko",
        active_from=T0,
    )
    store.save_patient_plan(plan)
    assert store.get_patient_plan("p1") == plan
    assert store.get_patient_plan("p2") is None
    assert store.get_checkin("missing") is None
    assert store.get_account("missing") is None


def test_patient_plans_list_per_care_team(store):
    def plan(patient_id, team):
        return PatientPlan(
            patient_id=patient_id,
            questionnaire_url="urn:hlidac:questionnaire:demo",
            questionnaire_version="1",
            care_team_id=team,
            active_from=T0,
        )

    for patient_id, team in (("p2", "team-onko"), ("p1", "team-onko"), ("p3", "team-other")):
        store.save_patient_plan(plan(patient_id, team))
    assert [p.patient_id for p in store.list_patient_plans("team-onko")] == ["p1", "p2"]
    store.save_patient_plan(plan("p2", "team-other"))  # moved to another team
    assert [p.patient_id for p in store.list_patient_plans("team-onko")] == ["p1"]
    assert store.list_patient_plans("team-none") == []


def test_lists_keep_insertion_order_on_equal_times(store):
    for checkin_id in ("c3", "c1", "c2"):
        store.save_checkin(checkin(checkin_id=checkin_id))
    store.save_checkin(checkin(checkin_id="c3", pending_link_id="fever"))  # an update keeps its place
    assert [c.checkin_id for c in store.list_checkins("p1")] == ["c3", "c1", "c2"]


def test_login_codes_list_oldest_first(store):
    def code(code_id, created):
        return LoginCode(
            code_id=code_id,
            account_id="acc-p1",
            code_hash="hmac-of-a-code",
            created_at=created,
            expires_at=created + timedelta(minutes=10),
        )

    store.save_login_code(code("k2", T0 + timedelta(minutes=1)))
    store.save_login_code(code("k1", T0))
    used = code("k1", T0).evolve(used_at=T0 + timedelta(minutes=2), attempts=1)
    store.save_login_code(used)
    assert [c.code_id for c in store.list_login_codes("acc-p1")] == ["k1", "k2"]
    assert store.list_login_codes("acc-p1")[0] == used
    assert store.list_login_codes("acc-other") == []


def test_assessments_and_audit_are_append_only(store):
    store.add_assessment(assessment())
    with pytest.raises(DuplicateError):
        store.add_assessment(assessment())
    store.append_audit(audit())
    with pytest.raises(DuplicateError):
        store.append_audit(audit())
    assert len(store.list_audit("checkin:c1")) == 1
    reviewed = grade(status="confirmed", confirmed_by="acc-n1", confirmed_at=T0)
    store.add_grade_review(GradeReview(review_id="r1", assessment_id="a1", grade=reviewed))
    with pytest.raises(DuplicateError):
        store.add_grade_review(GradeReview(review_id="r1", assessment_id="a1", grade=reviewed))
    assert store.get_assessment("a1").grades[0].status == "provisional"  # engine grade kept
    assert len(store.list_grade_reviews("a1")) == 1


def test_audit_lists_by_target_and_time(store):
    store.append_audit(audit("au2").evolve(at=T0 + timedelta(seconds=1)))
    store.append_audit(audit("au1"))
    store.append_audit(audit("au3", target="checkin:c2"))
    assert [e.audit_id for e in store.list_audit("checkin:c1")] == ["au1", "au2"]
    assert len(store.list_audit()) == 3


def test_alerts_filter_by_status_and_patient(store):
    store.save_alert(alert())
    store.save_alert(alert(alert_id="al2", patient_id="p2", created_at=T0 + timedelta(seconds=1)))
    assert [a.alert_id for a in store.list_alerts(status="open")] == ["al1", "al2"]
    assert [a.alert_id for a in store.list_alerts(patient_id="p2")] == ["al2"]
    assert store.list_alerts(status="resolved") == []
    store.save_alert(alert().evolve(status="acknowledged", acknowledged_by="acc-n1",
                                    acknowledged_at=T0 + timedelta(minutes=5)))
    assert [a.alert_id for a in store.list_alerts(status="open")] == ["al2"]
    assert [a.alert_id for a in store.list_alerts("acknowledged", "p1")] == ["al1"]


def test_accounts_list_per_team_and_per_patient(store):  # WP16
    def nurse(account_id, team_id, email):
        return Account(account_id=account_id, email=email, role="nurse",
                       display_name="N", team_id=team_id)  # fmt: skip

    for i, (account_id, team_id) in enumerate([("n2", "t1"), ("n1", "t1"), ("n3", "t2"), ("N1", "T1")]):
        store.save_account(nurse(account_id, team_id, f"sestra{i}@demo.local"))
    store.save_account(patient_account())
    store.save_account(patient_account(account_id="acc-p2", email="p2@demo.local", patient_id="p2"))
    assert [a.account_id for a in store.list_team_accounts("t1")] == ["n1", "n2"]
    assert store.list_team_accounts("t9") == []
    assert [a.account_id for a in store.list_patient_accounts("p1")] == ["acc-p1"]
    assert store.list_patient_accounts("P1") == []


def test_notifications_list_per_recipient(store):
    note = Notification(
        notification_id="n1",
        recipient_account_id="acc-n1",
        kind="urgent_alert",
        template_id="tpl-new-alert",
        template_version="1",
        link="/sestra/pripady/al1",
        created_at=T0,
        deliveries=[Delivery(channel="in_app", at=T0)],
    )
    store.save_notification(note)
    store.save_notification(note.evolve(read_at=T0 + timedelta(minutes=1)))
    assert store.list_notifications("acc-n1") == [note.evolve(read_at=T0 + timedelta(minutes=1))]
    assert store.list_notifications("acc-p1") == []


def test_enqueue_is_idempotent(store):
    assert store.enqueue(fhir_item()) is True
    assert store.enqueue(fhir_item(outbox_id="o9")) is False  # same idempotency key
    assert store.enqueue(email_item()) is True
    assert [o.outbox_id for o in store.list_outbox("pending")] == ["o1", "o2"]
    with pytest.raises(DuplicateError):
        store.enqueue(email_item(key="another-key"))  # o2 again


def test_outbox_rows_keep_their_instants(store):
    store.enqueue(email_item())
    row = store.get_outbox_item("o2")
    assert row == email_item()
    assert row.created_at == T0.astimezone(UTC)
    done = row.evolve(status="done", attempts=1, processed_at=T0 + timedelta(microseconds=7))
    store.update_outbox_item(done)
    assert store.get_outbox_item("o2") == done
    assert store.list_outbox("pending") == []
    assert store.get_outbox_item("missing") is None


def test_outbox_lists_by_kind(store):
    store.enqueue(fhir_item())
    store.enqueue(email_item())
    assert [o.outbox_id for o in store.list_outbox(kind="email")] == ["o2"]
    assert [o.outbox_id for o in store.list_outbox("pending", kind="fhir_transaction")] == ["o1"]
    assert store.list_outbox("done", kind="email") == []


def test_failed_rows_are_summarised_from_their_columns(store):
    store.enqueue(fhir_item())
    store.enqueue(email_item())
    store.enqueue(email_item(outbox_id="o3", key="notification:n3:email"))  # stays pending
    for outbox_id, error in (("o2", "SMTPError"), ("o1", "FhirStatus: 400")):
        row = store.get_outbox_item(outbox_id)
        store.update_outbox_item(
            row.evolve(status="failed", attempts=5, last_error=error, processed_at=T0)
        )
    failures = {f.outbox_id: f for f in store.list_outbox_failures()}
    assert set(failures) == {"o1", "o2"}
    assert (failures["o2"].kind, failures["o2"].template_id) == ("email", "tpl-new-alert")
    assert (failures["o1"].kind, failures["o1"].template_id) == ("fhir_transaction", None)
    assert (failures["o1"].attempts, failures["o1"].last_error) == (5, "FhirStatus: 400")
    assert failures["o1"].created_at == T0


def test_context_source_returns_only_what_it_holds():
    assert StaticContextSource({}).get_context("p1") is None


def test_sql_schema_has_a_table_per_store_collection():
    sql = resources.files("hlidac.iris").joinpath("schema.sql").read_text()
    tables = set(re.findall(r"CREATE TABLE Hlidac\.(\w+)", sql))
    assert tables == {
        "account",
        "login_code",
        "patient_plan",
        "checkin",
        "assessment",
        "grade_review",
        "alert",
        "notification",
        "outbox",
        "audit_log",
    }
