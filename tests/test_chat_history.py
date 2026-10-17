"""D46: the full conversation of a check-in is kept in order and shown on the case audit trail.
Hand-made fixtures only; every text below is invented."""

import pytest
from factories import T0
from patient_helpers import make_app
from security.auth_helpers import PATIENT_A
from test_clinician_app import urgent_case
from test_patient_app import follow, logged_in, post, start

from hlidac import audit
from hlidac.schemas import ChatEntry, Checkin, ModelCall
from hlidac.ui.patient.content import result_texts


def test_every_step_is_kept_in_order():
    app, store, mailbox = make_app()
    client = logged_in(app, mailbox)
    base = start(client)
    question = follow(client, client.get(f"{base}/dalsi"))
    client.get(question)
    client.get(question)  # a reload is not a second question
    confirm = follow(client, post(client, question, answer="no"))
    client.get(confirm)
    post(client, confirm)
    question = follow(client, client.get(f"{base}/dalsi"))
    client.get(question)
    post(client, question, answer="mild")
    result = follow(client, post(client, f"{base}/odeslat"))
    client.get(result)
    client.get(result)

    [saved] = store.list_checkins(PATIENT_A.patient_id)
    steps = [(h.event, h.link_id, h.text) for h in saved.history]
    assert steps == [
        ("question", "fever", "TEST Otázka na horečku?"),
        ("answer", "fever", "TEST Ne"),
        ("confirmation", "fever", "TEST Otázka na horečku? Rozuměli jsme: TEST Ne"),
        ("confirmed", "fever", "Ano, je to správně"),
        ("question", "nausea", saved.history[4].text),
        ("answer", "nausea", "TEST Mírná"),
        ("sent", None, "Odeslat hlášení"),
        ("result", None, result_texts().screen("not_assessed").heading),
    ]
    answers = [m.message_id for m in saved.messages if m.kind == "answer"]
    assert [h.message_id for h in saved.history if h.event == "answer"] == answers
    assert [h.at for h in saved.history] == sorted(h.at for h in saved.history)


def test_written_and_dictated_messages_are_kept_with_how_they_came():
    app, store, mailbox = make_app()
    client = logged_in(app, mailbox)
    base = start(client)
    post(client, f"{base}/napsat", text="TEST napsáno")
    post(client, f"{base}/napsat", text="TEST namluveno", voice="1")
    [saved] = store.list_checkins(PATIENT_A.patient_id)
    texts = [(h.event, h.text, h.via) for h in saved.history if h.event == "text"]
    assert texts == [("text", "TEST napsáno", "keyboard"), ("text", "TEST namluveno", "voice")]
    assert [h.message_id for h in saved.history if h.event == "text"] == [
        m.message_id for m in saved.messages
    ]


def test_history_refers_only_to_the_check_ins_own_messages():
    checkin = Checkin(
        checkin_id="c1",
        patient_id="p1",
        questionnaire_url="urn:test",
        questionnaire_version="1",
        reported_by="patient",
        started_at=T0,
        updated_at=T0,
    )
    stray = ChatEntry(at=T0, event="text", text="TEST", message_id="not-ours")
    with pytest.raises(ValueError, match="history"):
        checkin.evolve(history=[stray])


def test_case_audit_shows_the_conversation_and_names_voice_calls():
    _, store, _, nurse, alert = urgent_case()
    [checkin] = store.list_checkins(alert.patient_id)
    call = ModelCall(
        purpose="transcription",
        data_class="PATIENT",
        backend="local",
        endpoint_host="in-process",
        model="demo-whisper",
        request_text="[audio 100 B, sha256 0123456789abcdef]",
        response_text="TEST přepis",
        duration_ms=900,
        ok=True,
    )
    audit.record(
        store, T0, "system", "model_call", f"checkin:{checkin.checkin_id}", model_call=call
    )
    page = nurse.get(f"/sestra/pripad/{alert.alert_id}/zaznam").text
    assert "Rozhovor s pacientem (2)" in page and "TEST Otázka na horečku?" in page
    assert "Volání modelu (přepis hlasu)" in page
    assert "Volání modelu (extrakce z textu)" not in page
    assert "jen popis nahrávky" in page
    assert "ukázkovém úložišti v paměti" in page and "uložený v IRIS" not in page
