"""WP9: the patient check-in, end to end on the in-memory store with an invented TEST plan."""

import re

import pytest
from factories import alert, assessment, urgent_rule
from patient_helpers import make_app, urgent_on_fever
from security.auth_helpers import NURSE, PATIENT_A, PATIENT_B, csrf, login, new_client

from hlidac.schemas import DONT_KNOW, TYPED_VALUE, AnswerSource, Finding, Quantity


def logged_in(app, mailbox, account=PATIENT_A):
    client = new_client(app)
    login(client, mailbox, account.email)
    return client


def start(client) -> str:
    response = client.post("/pacient/hlaseni", data={"csrf_token": csrf(client, "/pacient")})
    assert response.status_code == 303
    return response.headers["location"]


def post(client, path, **data):
    return client.post(path, data={"csrf_token": csrf(client, path), **data})


def follow(client, response):
    assert response.status_code == 303, response.text
    return response.headers["location"]


def test_button_path_saves_every_answer_and_ends_without_false_reassurance():
    app, store, mailbox = make_app()
    client = logged_in(app, mailbox)
    base = start(client)
    assert "Odpovídat na otázky" in client.get(base).text

    question = follow(client, client.get(f"{base}/dalsi"))
    assert question == f"{base}/otazka/fever"
    page = client.get(question).text
    assert "Otázka 1 ze 2" in page and "TEST Otázka na horečku?" in page and "Nevím" in page

    confirm = follow(client, post(client, question, answer="no"))
    assert confirm == f"{base}/potvrdit/fever"
    assert "TEST Ne" in client.get(confirm).text
    assert follow(client, post(client, confirm)) == f"{base}/dalsi"

    question = follow(client, client.get(f"{base}/dalsi"))
    assert question == f"{base}/otazka/nausea"
    assert f'href="{base}/otazka/fever"' in client.get(question).text  # Back works
    assert follow(client, post(client, question, answer="mild")) == f"{base}/dalsi"

    summary = follow(client, client.get(f"{base}/dalsi"))
    assert summary == f"{base}/odeslat"
    assert "TEST Mírná" in client.get(summary).text
    result = follow(client, post(client, summary))
    page = client.get(result).text
    assert "Zatím je nikdo nevyhodnotil" in page
    assert "Váš tým o tomto hlášení zatím neví." in page
    assert "nesleduje nepřetržitě" in page

    [saved] = store.list_checkins(PATIENT_A.patient_id)
    assert saved.status == "completed"
    states = {f.link_id: (f.state, f.confirmed) for f in saved.findings}
    assert states == {"fever": ("absent", True), "nausea": ("present", False)}
    assert all(m.session_account_id == PATIENT_A.account_id for m in saved.messages)


def test_dont_know_is_recorded_as_unknown():
    app, store, mailbox = make_app()
    client = logged_in(app, mailbox)
    base = start(client)
    assert follow(client, post(client, f"{base}/otazka/fever", answer=DONT_KNOW)) == f"{base}/dalsi"
    [saved] = store.list_checkins(PATIENT_A.patient_id)
    assert saved.findings[0].state == "unknown"


def test_typed_temperature_is_confirmed_before_it_counts():
    app, store, mailbox = make_app()
    client = logged_in(app, mailbox)
    base = start(client)
    follow(client, post(client, f"{base}/otazka/fever", answer="yes"))
    post(client, f"{base}/potvrdit/fever")

    bad = post(client, f"{base}/otazka/temperature", answer=TYPED_VALUE, number="horko")
    assert bad.status_code == 400
    assert 'role="alert"' in bad.text and 'aria-invalid="true"' in bad.text
    assert "horko" in bad.text  # what the patient typed stays in the field

    confirm = follow(
        client, post(client, f"{base}/otazka/temperature", answer=TYPED_VALUE, number="38,4")
    )
    assert confirm == f"{base}/potvrdit/temperature"
    assert "38,4 °C" in client.get(confirm).text
    [saved] = store.list_checkins(PATIENT_A.patient_id)
    temperature = next(f for f in saved.findings if f.link_id == "temperature")
    assert (temperature.value.value, temperature.confirmed) == (38.4, False)


def test_choice_without_an_answer_shows_an_error():
    app, _, mailbox = make_app()
    client = logged_in(app, mailbox)
    base = start(client)
    response = post(client, f"{base}/otazka/fever")
    assert response.status_code == 400
    assert "Vyberte prosím jednu možnost." in response.text


def test_written_report_is_saved_and_questions_follow():
    app, store, mailbox = make_app()
    client = logged_in(app, mailbox)
    base = start(client)
    assert post(client, f"{base}/napsat", text="   ").status_code == 400
    too_long = post(client, f"{base}/napsat", text="x" * 2001)
    assert too_long.status_code == 400 and "Text je moc dlouhý" in too_long.text
    assert follow(client, post(client, f"{base}/napsat", text="TEST text")) == f"{base}/dalsi"
    [saved] = store.list_checkins(PATIENT_A.patient_id)
    assert [m.text for m in saved.messages] == ["TEST text"]


def test_starting_again_continues_the_open_report():
    app, _, mailbox = make_app()
    client = logged_in(app, mailbox)
    first = start(client)
    assert "Pokračovat v hlášení" in client.get("/pacient").text
    assert start(client) == first


def test_without_a_plan_no_report_can_start():
    app, store, mailbox = make_app(plans=False)
    client = logged_in(app, mailbox)
    assert "Dotazník zatím není připravený" in client.get("/pacient").text
    assert start(client) == "/pacient"
    assert store.list_checkins(PATIENT_A.patient_id) == []


def test_urgent_answer_shows_the_urgent_screen_at_once():
    app, store, mailbox = make_app(process=urgent_on_fever)
    client = logged_in(app, mailbox)
    base = start(client)

    # Before the confirmation and before the other questions: urgent never waits.
    result = follow(client, post(client, f"{base}/otazka/fever", answer="yes"))
    assert result == f"{base}/vysledek"
    page = client.get(result).text
    assert "Jednejte hned" in page
    assert 'href="tel:155"' not in page
    assert re.search(r'<h1 id="result-heading" tabindex="-1" autofocus>', page)
    assert "Váš tým jsme o hlášení informovali." in page
    assert urgent_rule().reason not in page  # rules and grades are for clinicians only
    assert "CTCAE" not in page

    [saved] = store.list_checkins(PATIENT_A.patient_id)
    assert saved.status == "completed"
    assert follow(client, client.get(f"{base}/otazka/nausea")) == f"{base}/vysledek"


def test_three_severe_signals_show_the_155_action():
    def emergency(store):
        def process(checkin, plan, now):
            if not any(f.link_id == "fever" and f.state == "present" for f in checkin.findings):
                return None
            temp = Quantity(value=39.5, unit="Cel")
            found = [
                Finding(
                    link_id="temperature",
                    state="present",
                    code=TYPED_VALUE,
                    value=temp,
                    source=AnswerSource(
                        kind="answer",
                        message_id="m-em-1",
                        link_id="temperature",
                        code=TYPED_VALUE,
                        value=temp,
                    ),
                ),
                Finding(
                    link_id="unwell",
                    state="present",
                    code="yes",
                    source=AnswerSource(
                        kind="answer",
                        message_id="m-em-2",
                        link_id="unwell",
                        code="yes",
                    ),
                ),
                Finding(
                    link_id="chills_severity",
                    state="present",
                    code="whole_body",
                    source=AnswerSource(
                        kind="answer",
                        message_id="m-em-3",
                        link_id="chills_severity",
                        code="whole_body",
                    ),
                ),
            ]
            result = assessment(
                assessment_id=f"a-{len(store.list_assessments(checkin.patient_id))}",
                checkin_id=checkin.checkin_id,
                patient_id=checkin.patient_id,
                status="urgent",
                findings=found,
                triggered_rules=[urgent_rule()],
            )
            store.add_assessment(result)
            store.save_alert(
                alert(
                    alert_id=f"al-{result.assessment_id}",
                    patient_id=checkin.patient_id,
                    assessment_id=result.assessment_id,
                )
            )
            return result

        return process

    app, _, mailbox = make_app(process=emergency)
    client = logged_in(app, mailbox)
    base = start(client)
    result = follow(client, post(client, f"{base}/otazka/fever", answer="yes"))
    page = client.get(result).text
    assert 'href="tel:+420000000000"' not in page
    assert 'href="tel:155"' in page


def test_processing_failure_stays_visible():
    def broken(store):
        def process(checkin, plan, now):
            raise RuntimeError("model down")

        return process

    app, store, mailbox = make_app(process=broken)
    client = logged_in(app, mailbox)
    base = start(client)
    result = follow(client, post(client, f"{base}/otazka/fever", answer="no"))
    assert result == f"{base}/vysledek"
    assert "nepodařilo zpracovat" in client.get(result).text
    [saved] = store.list_checkins(PATIENT_A.patient_id)
    assert saved.status == "processing_failed"
    assert saved.failure == "processing error: RuntimeError"


CHECKIN_PAGES = [
    "",
    "/napsat",
    "/dalsi",
    "/otazka/fever",
    "/potvrdit/fever",
    "/odeslat",
    "/vysledek",
]


@pytest.mark.parametrize("page", CHECKIN_PAGES)
def test_patient_b_cannot_open_patient_as_report(page):
    app, _, mailbox = make_app()
    base = start(logged_in(app, mailbox, PATIENT_A))
    other = logged_in(app, mailbox, PATIENT_B)
    response = other.get(f"{base}{page}")
    assert response.status_code == 403
    assert "TEST" not in response.text


@pytest.mark.parametrize("page", ["/napsat", "/otazka/fever", "/potvrdit/fever", "/odeslat"])
def test_patient_b_cannot_answer_patient_as_report(page):
    app, store, mailbox = make_app()
    base = start(logged_in(app, mailbox, PATIENT_A))
    other = logged_in(app, mailbox, PATIENT_B)
    response = other.post(
        f"{base}{page}", data={"csrf_token": csrf(other, "/pacient"), "answer": "no", "text": "x"}
    )
    assert response.status_code == 403
    [saved] = store.list_checkins(PATIENT_A.patient_id)
    assert saved.messages == [] and saved.status == "in_progress"


def test_nurse_cannot_use_the_patient_app():
    app, _, mailbox = make_app()
    base = start(logged_in(app, mailbox, PATIENT_A))
    nurse = logged_in(app, mailbox, NURSE)
    assert nurse.get("/pacient").status_code == 403
    assert nurse.get(base).status_code == 403


def test_answers_need_the_csrf_token():
    app, store, mailbox = make_app()
    client = logged_in(app, mailbox)
    base = start(client)
    response = client.post(f"{base}/otazka/fever", data={"answer": "no"})
    assert response.status_code == 403
    assert store.list_checkins(PATIENT_A.patient_id)[0].findings == []


def test_failure_after_an_urgent_assessment_still_shows_the_urgent_screen():
    def urgent_then_broken(store):
        urgent = urgent_on_fever(store)

        def process(checkin, plan, now):
            if urgent(checkin, plan, now) is not None:
                raise RuntimeError("outbox down after the alert row")

        return process

    app, store, mailbox = make_app(process=urgent_then_broken)
    client = logged_in(app, mailbox)
    base = start(client)
    result = follow(client, post(client, f"{base}/otazka/fever", answer="yes"))
    page = client.get(result).text
    assert "Jednejte hned" in page and 'href="tel:155"' not in page
    assert "nepodařilo zpracovat" not in page
    assert store.list_checkins(PATIENT_A.patient_id)[0].status == "processing_failed"


def test_unconfirmed_no_never_reaches_the_pipeline_as_absent():
    seen = []

    def recording(store):
        def process(checkin, plan, now):
            seen.append({f.link_id: (f.state, f.confirmed) for f in checkin.findings})

        return process

    app, _, mailbox = make_app(process=recording)
    client = logged_in(app, mailbox)
    base = start(client)
    post(client, f"{base}/otazka/fever", answer="no")  # not confirmed yet
    assert seen[-1] == {}
    post(client, f"{base}/potvrdit/fever")
    assert seen[-1] == {"fever": ("absent", True)}
    post(client, f"{base}/otazka/fever", answer="yes")  # unconfirmed "Ano" raises at once
    assert seen[-1] == {"fever": ("present", False)}
