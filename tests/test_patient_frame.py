"""D53: the messenger frame of the patient pages. The header's treatment day comes only from the
hospital record (`Pipeline.context`, the session's own patient) and never breaks a page; the chat
log shows a confirmation short after its own answer. No patient data."""

import dataclasses
from datetime import timedelta

from factories import T0
from patient_helpers import make_app
from security.auth_helpers import PATIENT_A
from test_patient_app import follow, logged_in, post, start

from hlidac.schemas import PatientContext
from hlidac.ui.patient.content import chat_texts


def with_context(app, read):
    app.state.pipeline = dataclasses.replace(app.state.pipeline, context=read)


def dose_days_ago(days):
    def read(patient_id):
        assert patient_id == PATIENT_A.patient_id  # only the session's own patient is read
        return PatientContext(
            patient_id=patient_id,
            last_sact_dose_at=T0 - timedelta(days=days),
            source_refs=["MedicationAdministration/test-1"],
            read_at=T0,
        )

    return read


def test_the_header_shows_the_days_since_the_last_dose():
    app, _, mailbox = make_app()
    with_context(app, dose_days_ago(10))
    client = logged_in(app, mailbox)
    assert "10. den po poslední dávce protinádorové léčby" in client.get("/pacient").text
    assert "10. den po poslední dávce" in client.get(start(client)).text


def test_no_record_or_a_failed_read_hides_the_line_and_keeps_the_page():
    def broken(patient_id):
        raise ConnectionError("IRIS down")

    for read in (lambda patient_id: None, broken, None):
        app, _, mailbox = make_app()
        with_context(app, read)
        client = logged_in(app, mailbox)
        page = client.get("/pacient")
        assert page.status_code == 200 and "po poslední dávce" not in page.text


def test_a_confirmation_after_its_answer_is_short_in_the_log():
    app, store, mailbox = make_app()
    client = logged_in(app, mailbox)
    base = start(client)
    client.get(f"{base}?otazky=1")
    follow(client, post(client, f"{base}/otazka/fever", answer="no", chat="1"))
    follow(client, post(client, f"{base}/potvrdit/fever", chat="1"))
    page = client.get(base).text
    log = page.split('id="tah"')[0]
    assert "Je to správně? Rozuměli jsme: TEST Ne" in log
    assert "TEST Otázka na horečku? Rozuměli jsme" not in log
    [checkin] = store.list_checkins(PATIENT_A.patient_id)  # the stored history keeps it whole
    assert any(e.text == "TEST Otázka na horečku? Rozuměli jsme: TEST Ne" for e in checkin.history)


# --- D59: audio mode, the server side ------------------------------------------------------


def spoken(page: str) -> str:
    import html
    import re

    match = re.search(r'data-audio-say="([^"]*)"', page)
    assert match, "the step says what the device reads aloud"
    return html.unescape(match.group(1))


def test_audio_mode_is_offered_only_with_voice():
    app, _, mailbox = make_app()
    client = logged_in(app, mailbox)
    assert 'id="audio-mode"' not in client.get(start(client)).text
    app, _, mailbox = make_app(transcribe=lambda audio, target: "TEST")
    client = logged_in(app, mailbox)
    page = client.get(start(client)).text
    assert 'id="audio-mode" hidden' in page and "audio.js" in page  # shown by audio.js


def test_each_step_says_only_its_approved_texts():
    app, _, mailbox = make_app(transcribe=lambda audio, target: "TEST")
    client = logged_in(app, mailbox)
    base = start(client)
    texts, audio = chat_texts(), chat_texts().audio
    assert "Jak se dnes cítíte?" in spoken(client.get(base).text)  # the opening prompt
    client.get(f"{base}?otazky=1")
    question = spoken(client.get(base).text)
    assert (
        question == f"TEST Otázka na horečku? {audio.options} TEST Ano, TEST Ne, {audio.or_} Nevím."
    )
    follow(client, post(client, f"{base}/otazka/fever", answer="no", chat="1"))
    page = client.get(base).text
    next_question = spoken(page)  # D58: no confirmation after the answer; the bridge line first
    assert "TEST Otázka na nevolnost?" in next_question and "TEST Žádná" in next_question
    follow(client, post(client, f"{base}/otazka/nausea", answer="no", chat="1"))
    page = client.get(base).text
    summary = spoken(page)  # the closing summary, every row read aloud, then the yes/no
    assert 'data-audio-kind="summary"' in page
    assert texts.summary_question in summary and summary.endswith(audio.send_report)
    assert ": TEST Ne." in summary and ": TEST Žádná." in summary


def test_an_exact_answer_on_a_question_needs_no_read_back():
    """D58: every answer is confirmed in the closing summary, which audio mode reads in full;
    so on a question an exact option or number goes in at once (free text is read back)."""
    app, _, mailbox = make_app(transcribe=lambda audio, target: "TEST")
    client = logged_in(app, mailbox)
    base = start(client)
    assert 'data-audio-direct=""' in client.get(base).text  # the opening: free words, read back
    client.get(f"{base}?otazky=1")
    assert 'data-audio-direct="1"' in client.get(base).text


def test_yes_and_no_words_never_overlap():
    import pytest
    from pydantic import ValidationError

    from hlidac.ui.patient.content import AudioTexts

    raw = chat_texts().audio.model_dump(by_alias=True)
    with pytest.raises(ValidationError):
        AudioTexts.model_validate({**raw, "no_words": [*raw["no_words"], "ano"]})
    assert "nevím" not in raw["yes_words"] + raw["no_words"]
