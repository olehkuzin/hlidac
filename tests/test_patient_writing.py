"""WP8 + WP5 in the patient app: written text → quoted findings → only the missing questions, on
the invented TEST plan. The extractor is a stand-in with fixed results; no live model."""

from patient_helpers import make_app, urgent_on_fever
from security.auth_helpers import PATIENT_A
from test_patient_app import follow, logged_in, post, start

from hlidac import extraction
from hlidac.extraction import ExtractionFailed
from hlidac.schemas import TYPED_VALUE, ExtractionResult, Finding, Quantity, QuoteSource


def quoted(message, link_id, state, code=None, value=None, quote=None):
    return Finding(
        link_id=link_id,
        state=state,
        code=code,
        value=Quantity(value=value, unit="Cel") if value is not None else None,
        source=QuoteSource(kind="quote", message_id=message.message_id, quote=quote or "TEST"),
        extractor_version=extraction.EXTRACTOR_VERSION,
    )


class Extractor:
    def __init__(self, make=None, error=None):
        self.make, self.error, self.calls = make, error, []

    def __call__(self, message, plan, names):
        self.calls.append((message, names))
        if self.error:
            raise self.error
        return ExtractionResult(findings=self.make(message) if self.make else [])


def test_a_written_denial_of_a_red_flag_is_shown_back_with_its_quote():
    extractor = Extractor(lambda m: [quoted(m, "fever", "absent", "no", quote="horečku nemám")])
    app, store, mailbox = make_app(extract=extractor)
    client = logged_in(app, mailbox)
    base = start(client)
    assert follow(client, post(client, f"{base}/napsat", text="TEST horečku nemám")) == (
        f"{base}/dalsi"
    )
    [(message, names)] = extractor.calls
    assert message.text == "TEST horečku nemám" and names == [PATIENT_A.display_name]

    # D58: the required item nobody mentioned is asked; the denial isn't asked again one by one
    assert follow(client, client.get(f"{base}/dalsi")) == f"{base}/otazka/nausea"
    [saved] = store.list_checkins(PATIENT_A.patient_id)
    fever = next(f for f in saved.findings if f.link_id == "fever")
    assert not fever.confirmed  # a quote never closes a red flag by itself (G5) ...
    assert "TEST Ne" in client.get(f"{base}/odeslat").text  # ... it is listed for checking
    post(client, f"{base}/odeslat", confirm="1")  # and sending from the list confirms it

    [saved] = store.list_checkins(PATIENT_A.patient_id)
    assert saved.extracted_message_ids == [message.message_id]
    fever = next(f for f in saved.findings if f.link_id == "fever")
    assert fever.state == "absent" and fever.confirmed and fever.source.kind == "quote"
    actions = [e.action for e in store.list_audit(f"checkin:{saved.checkin_id}")]
    assert "checkin_extracted" in actions


def test_a_written_red_flag_is_confirmed_first_then_urgent_at_once():
    # D49: the model's reading of the patient's words alone never opens the ambulance action by
    # itself; the confirmation is the very next screen, and "Ano, je to správně" makes it urgent
    # at once.
    extractor = Extractor(lambda m: [quoted(m, "fever", "present", "yes")])
    app, store, mailbox = make_app(process=urgent_on_fever, extract=extractor)
    client = logged_in(app, mailbox)
    base = start(client)
    assert follow(client, post(client, f"{base}/napsat", text="TEST je mi zle")) == f"{base}/dalsi"
    assert not store.list_alerts(patient_id=PATIENT_A.patient_id)
    confirm = follow(client, client.get(f"{base}/dalsi"))
    assert confirm == f"{base}/potvrdit/fever"  # asked before anything else
    result = follow(client, post(client, confirm))
    assert result == f"{base}/vysledek"  # no further question waits
    page = client.get(result).text
    assert "Váš tým jsme o hlášení informovali." in page
    assert "tel:155" not in page
    assert store.list_alerts(patient_id=PATIENT_A.patient_id)


def test_a_sent_report_counts_an_unconfirmed_red_flag_quote():
    extractor = Extractor(lambda m: [quoted(m, "fever", "present", "yes")])
    app, store, mailbox = make_app(process=urgent_on_fever, extract=extractor)
    client = logged_in(app, mailbox)
    base = start(client)
    follow(client, post(client, f"{base}/napsat", text="TEST je mi zle"))
    result = follow(client, post(client, f"{base}/odeslat"))
    assert result == f"{base}/vysledek"  # sending never loses what the patient said
    page = client.get(result).text
    assert "Váš tým jsme o hlášení informovali." in page
    assert "tel:155" not in page
    assert store.list_alerts(patient_id=PATIENT_A.patient_id)


def test_a_quoted_number_is_confirmed_before_it_counts():
    def make(m):
        return [
            quoted(m, "fever", "present", "yes"),
            quoted(m, "temperature", "present", value=38.4, quote="38,4"),
        ]

    app, _, mailbox = make_app(extract=Extractor(make))
    client = logged_in(app, mailbox)
    base = start(client)
    follow(client, post(client, f"{base}/napsat", text="TEST teplota 38,4"))
    assert follow(client, client.get(f"{base}/dalsi")) == f"{base}/potvrdit/fever"
    post(client, f"{base}/potvrdit/fever")
    confirm = follow(client, client.get(f"{base}/dalsi"))
    assert confirm == f"{base}/potvrdit/temperature"
    assert "38,4 °C" in client.get(confirm).text


def test_a_failed_extraction_keeps_the_text_and_asks_every_question():
    app, store, mailbox = make_app(extract=Extractor(error=ExtractionFailed("x")))
    client = logged_in(app, mailbox)
    base = start(client)
    assert follow(client, post(client, f"{base}/napsat", text="TEST text")) == f"{base}/dalsi"
    assert follow(client, client.get(f"{base}/dalsi")) == f"{base}/otazka/fever"
    [saved] = store.list_checkins(PATIENT_A.patient_id)
    assert saved.status == "in_progress" and saved.extracted_message_ids == []
    assert [m.text for m in saved.messages] == ["TEST text"]
    # the unread text fills the free-text item, so the rules send the report to a nurse
    [note] = [f for f in saved.findings if f.link_id == "note"]
    assert note.state == "present" and note.source.kind == "message"
    assert note.source.message_id == saved.messages[0].message_id
    [failed] = [
        e
        for e in store.list_audit(f"checkin:{saved.checkin_id}")
        if e.action == "extraction_failed"
    ]
    assert failed.details["error"] == "ExtractionFailed"
    assert "TEST text" not in str(failed.details)  # never the patient's text


def test_after_the_turns_the_summary_still_offers_the_rest(monkeypatch):
    # one turn: the reported fever's follow-up takes it, so nausea is left for the summary
    monkeypatch.setattr("hlidac.clarification.MIN_QUESTIONS", 0)  # D56's minimum tested apart
    extractor = Extractor(lambda m: [quoted(m, "fever", "present", "yes")])
    app, store, mailbox = make_app(extract=extractor)
    plans = app.state.pipeline.plans
    key, plan = next(iter(plans.items()))
    plans[key] = plan.evolve(max_clarification_turns=1)
    client = logged_in(app, mailbox)
    base = start(client)
    follow(client, post(client, f"{base}/napsat", text="TEST mám horečku"))
    post(client, f"{base}/potvrdit/fever")
    temperature = follow(client, client.get(f"{base}/dalsi"))
    assert temperature == f"{base}/otazka/temperature"
    confirm = follow(client, post(client, temperature, answer=TYPED_VALUE, number="37,2"))
    follow(client, post(client, confirm))
    summary = follow(client, client.get(f"{base}/dalsi"))
    assert summary == f"{base}/odeslat"
    page = client.get(summary).text
    assert "Na některé otázky jste ještě neodpověděli" in page
    assert f'href="{base}/otazka/nausea"' in page
    [saved] = store.list_checkins(PATIENT_A.patient_id)
    assert saved.clarification_turns == 1


def test_without_a_model_the_text_is_kept_unread_for_the_nurse():
    app, store, mailbox = make_app()
    client = logged_in(app, mailbox)
    base = start(client)
    assert follow(client, post(client, f"{base}/napsat", text="TEST text")) == f"{base}/dalsi"
    [saved] = store.list_checkins(PATIENT_A.patient_id)
    [note] = [f for f in saved.findings if f.link_id == "note"]
    assert note.source.kind == "message"
    actions = [e.action for e in store.list_audit(f"checkin:{saved.checkin_id}")]
    assert "extraction_failed" not in actions  # nothing failed; no model is configured
