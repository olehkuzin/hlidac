"""WP16 (UI): the notification lists of the patient and the nurse, on the in-memory store.

The notifications are made directly with the template ids of the approved content
(`data/content/notifications/`), so these tests don't depend on the alert wiring. No patient data.
"""

from datetime import timedelta

from factories import T0, email_item
from patient_helpers import make_app
from security.auth_helpers import NURSE, PATIENT_A, PATIENT_B, csrf, login, new_client
from test_clinician_app import OTHER_NURSE

from hlidac.notifications import text_for
from hlidac.schemas import Delivery, Notification

PATIENT_TEXT = "Váš tým jsme o hlášení informovali."
NURSE_TEXT = "Čeká nové naléhavé hlášení."


def notification(nid, account, at=T0, **fields) -> Notification:
    patient = account.role == "patient"
    defaults = {
        "kind": "team_notified" if patient else "urgent_alert",
        "template_id": "team-notified" if patient else "urgent-alert",
        "link": "/pacient" if patient else "/sestra/pripad/al-1",
        "deliveries": [Delivery(channel="in_app", at=at)],
    }
    return Notification(
        notification_id=nid,
        recipient_account_id=account.account_id,
        template_version="1",
        created_at=at,
        **{**defaults, **fields},
    )


def setup(*notes):
    app, store, mailbox = make_app()
    store.save_account(OTHER_NURSE)
    for note in notes:
        store.save_notification(note)
    return app, store, mailbox


def logged_in(app, mailbox, account):
    client = new_client(app)
    login(client, mailbox, account.email)
    return client


def open_note(client, path, list_page):
    return client.post(path, data={"csrf_token": csrf(client, list_page)})


def test_texts_come_from_the_approved_content():
    assert text_for(notification("n", PATIENT_A)) == PATIENT_TEXT
    assert text_for(notification("n", NURSE)) == NURSE_TEXT


def test_patient_list_shows_approved_text_newest_first_with_state_in_words():
    older = notification("n-old", PATIENT_A, read_at=T0 + timedelta(minutes=5))
    newer = notification("n-new", PATIENT_A, at=T0 + timedelta(hours=2))
    app, _, mailbox = setup(older, newer)
    client = logged_in(app, mailbox, PATIENT_A)

    assert "Nová oznámení: 1" in client.get("/pacient").text
    page = client.get("/pacient/oznameni").text
    assert page.count(PATIENT_TEXT) == 2
    assert page.index("/pacient/oznameni/n-new") < page.index("/pacient/oznameni/n-old")
    assert "Nové" in page and "Přečtené" in page  # the state in words, not colour alone
    assert "16. 10. 2026 v 10:00" in page  # the demo clock's time, in Czech


def test_opening_marks_read_redirects_to_the_link_and_updates_the_count():
    app, store, mailbox = setup(notification("n1", PATIENT_A))
    client = logged_in(app, mailbox, PATIENT_A)

    response = open_note(client, "/pacient/oznameni/n1", "/pacient/oznameni")
    assert response.status_code == 303
    assert response.headers["location"] == "/pacient"
    [saved] = store.list_notifications(PATIENT_A.account_id)
    assert saved.read_at == T0
    assert "Nová oznámení: 0" in client.get("/pacient").text
    assert "Přečtené" in client.get("/pacient/oznameni").text


def test_opening_without_the_csrf_token_is_rejected():
    app, store, mailbox = setup(notification("n1", PATIENT_A))
    client = logged_in(app, mailbox, PATIENT_A)
    client.get("/pacient/oznameni")

    response = client.post("/pacient/oznameni/n1", data={})
    assert response.status_code == 403
    assert store.list_notifications(PATIENT_A.account_id)[0].read_at is None


def test_a_patient_cannot_see_or_open_another_patients_notification():
    app, store, mailbox = setup(notification("n-b", PATIENT_B))
    client = logged_in(app, mailbox, PATIENT_A)

    page = client.get("/pacient/oznameni").text
    assert "n-b" not in page and PATIENT_TEXT not in page
    assert open_note(client, "/pacient/oznameni/n-b", "/pacient/oznameni").status_code == 404
    assert store.list_notifications(PATIENT_B.account_id)[0].read_at is None


def test_a_link_outside_the_own_app_falls_back_to_the_list():
    app, _, mailbox = setup(notification("n1", PATIENT_A, link="/sestra/pripad/al-1"))
    client = logged_in(app, mailbox, PATIENT_A)
    response = open_note(client, "/pacient/oznameni/n1", "/pacient/oznameni")
    assert response.headers["location"] == "/pacient/oznameni"


def test_the_lists_need_the_right_login():
    app, _, mailbox = setup()
    anonymous = new_client(app)
    assert anonymous.get("/pacient/oznameni").headers["location"] == "/prihlaseni"
    assert anonymous.get("/sestra/oznameni").headers["location"] == "/prihlaseni"
    nurse = logged_in(app, mailbox, NURSE)
    assert nurse.get("/pacient/oznameni").status_code == 403
    patient = logged_in(app, mailbox, PATIENT_A)
    assert patient.get("/sestra/oznameni").status_code == 403


def test_a_nurse_sees_only_her_own_notifications():
    app, store, mailbox = setup(notification("n-mine", NURSE), notification("n-other", OTHER_NURSE))
    client = logged_in(app, mailbox, NURSE)

    page = client.get("/sestra/oznameni").text
    assert "/sestra/oznameni/n-mine" in page and "n-other" not in page
    assert "Oznámení (nová: 1)" in client.get("/sestra").text  # the nav count on every page
    assert open_note(client, "/sestra/oznameni/n-other", "/sestra/oznameni").status_code == 404
    assert store.list_notifications(OTHER_NURSE.account_id)[0].read_at is None

    response = open_note(client, "/sestra/oznameni/n-mine", "/sestra/oznameni")
    assert response.headers["location"] == "/sestra/pripad/al-1"
    assert "Oznámení (nová: 0)" in client.get("/sestra").text


def test_the_email_delivery_shows_its_status_but_never_the_body_or_address():
    item = email_item("o-mail")
    deliveries = [
        Delivery(channel="in_app", at=T0),
        Delivery(channel="email", at=T0, outbox_id="o-mail"),
    ]
    app, store, mailbox = setup(
        notification("n-mail", NURSE, deliveries=deliveries),
        notification("n-app", NURSE, at=T0 + timedelta(minutes=1)),
    )
    client = logged_in(app, mailbox, NURSE)
    store.enqueue(item)  # after the login, which sends pending emails in the dev runner

    page = client.get("/sestra/oznameni").text
    assert "čeká na odeslání" in page and "bez e-mailu" in page and "doručeno" in page
    for secret in (item.job.to, item.job.text_body, item.job.subject):
        assert secret not in page

    failed = item.evolve(status="failed", attempts=1, last_error="SMTPError: x", processed_at=T0)
    store.update_outbox_item(failed)
    page = client.get("/sestra/oznameni").text
    assert "selhalo" in page and "SMTPError" not in page
