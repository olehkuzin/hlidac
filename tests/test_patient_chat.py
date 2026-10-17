"""The chat check-in on the invented TEST plan: the app asks one approved question at a time, the
patient types, dictates or taps a quick reply. No live model; no patient data."""

from demo_helpers import PLAN as DEMO_PLAN
from patient_helpers import make_app, urgent_on_fever
from security.auth_helpers import PATIENT_A, csrf
from test_patient_app import follow, logged_in, post, start
from test_patient_writing import quoted

from hlidac.schemas import ExtractionResult


def say(client, base, **data):
    """Send a chat message (the composer's form is on the chat page)."""
    return client.post(f"{base}/zprava", data={"csrf_token": csrf(client, base), **data})


class AskedExtractor:
    """A stand-in model that records which question each message answered."""

    def __init__(self, make=None):
        self.make, self.calls = make, []

    def __call__(self, message, plan, names, asked=None):
        self.calls.append((message.text, asked.link_id if asked else None))
        return ExtractionResult(findings=self.make(message, asked) if self.make else [])


def test_the_chat_opens_with_the_approved_prompt_and_reads_the_reply():
    extractor = AskedExtractor(lambda m, a: [quoted(m, "nausea", "present", "yes")])
    app, store, mailbox = make_app(extract=extractor)
    client = logged_in(app, mailbox)
    base = start(client)
    page = client.get(base).text
    assert "Jak se dnes cítíte?" in page and 'action="' + base + '/zprava"' in page
    assert follow(client, say(client, base, text="TEST je mi zle")) == base
    assert extractor.calls == [("TEST je mi zle", None)]
    page = client.get(base).text
    assert "TEST je mi zle" in page  # the patient's own words in the log
    assert "TEST Otázka na horečku?" in page  # then the red flag, as an approved question
    [checkin] = store.list_checkins(PATIENT_A.patient_id)
    assert checkin.history[0].text.startswith("Jak se dnes cítíte?")


def test_a_typed_option_counts_like_the_button_and_is_confirmed_in_the_closing_summary():
    """D58: no "Je to správně?" after each answer; one "Rozuměl jsem správně?" at the end lists
    every answer, and sending from it confirms them (G5). Until then a denial can't close a red
    flag."""
    app, store, mailbox = make_app(extract=AskedExtractor())
    client = logged_in(app, mailbox)
    base = start(client)
    client.get(f"{base}?otazky=1")
    page = client.get(base).text
    assert "TEST Otázka na horečku?" in page  # the questions mode stays after a reload
    follow(client, say(client, base, text="test ne.", link_id="fever"))
    page = client.get(base).text
    assert "Je to správně?" not in page and "TEST Otázka na nevolnost?" in page
    follow(client, post(client, f"{base}/otazka/nausea", answer="no", chat="1"))
    page = client.get(base).text
    assert "Rozuměl jsem správně?" in page and "Ano, odeslat hlášení" in page
    assert "Horečka nebo zvýšená teplota</dt>" in page and f'href="{base}?opravit=fever"' in page
    [checkin] = store.list_checkins(PATIENT_A.patient_id)
    assert not next(f for f in checkin.findings if f.link_id == "fever").confirmed
    assert "Rozuměl jsem správně? Tohle jsem si zapsal:" in checkin.history[-1].text  # D46

    response = post(client, f"{base}/odeslat", confirm="1")
    assert follow(client, response) == f"{base}/vysledek"
    [checkin] = store.list_checkins(PATIENT_A.patient_id)
    assert next(f for f in checkin.findings if f.link_id == "fever").confirmed
    events = [e.event for e in checkin.history]
    assert events[events.index("confirmed") + 1] == "sent"
    audit = store.list_audit(f"checkin:{checkin.checkin_id}")
    assert any(e.action == "checkin_answers_confirmed" for e in audit)


def test_a_summary_answer_can_be_fixed_and_more_can_be_added():
    """D58: "Opravit" asks that question again in the chat; words typed under the summary are
    read by the model, and the summary comes back with them."""

    def read(message, asked):
        return [quoted(message, "note", "present", quote="pálí mě kůže")] if not asked else []

    app, _, mailbox = make_app(extract=AskedExtractor(read))
    client = logged_in(app, mailbox)
    base = start(client)
    client.get(f"{base}?otazky=1")
    follow(client, post(client, f"{base}/otazka/fever", answer="no", chat="1"))
    follow(client, post(client, f"{base}/otazka/nausea", answer="no", chat="1"))
    assert "Rozuměl jsem správně?" in client.get(base).text
    page = client.get(f"{base}?opravit=fever").text
    assert '<h1 id="prompt" class="bubble-heading" tabindex="-1">TEST Otázka na horečku?' in page
    follow(client, post(client, f"{base}/otazka/fever", answer="dont_know", chat="1"))
    page = client.get(base).text
    assert "Rozuměl jsem správně?" in page and "Nevím" in page

    follow(client, say(client, base, text="TEST a ještě mě pálí kůže"))
    page = client.get(base).text
    assert "Děkuji, doplnil jsem to." in page and "Rozuměl jsem správně?" in page
    assert "Další, co jste uvedli" in page and "„pálí mě kůže“" in page


def test_a_free_reply_is_read_with_the_question_as_a_hint_and_asked_again_if_unclear():
    extractor = AskedExtractor()
    app, _, mailbox = make_app(extract=extractor)
    client = logged_in(app, mailbox)
    base = start(client)
    client.get(f"{base}?otazky=1")
    follow(client, say(client, base, text="TEST asi trochu", link_id="fever"))
    assert extractor.calls == [("TEST asi trochu", "fever")]
    page = client.get(base).text
    assert "nerozuměli" in page and "TEST Otázka na horečku?" in page


def test_an_urgent_quick_reply_goes_straight_to_the_urgent_screen():
    app, _, mailbox = make_app(process=urgent_on_fever, extract=AskedExtractor())
    client = logged_in(app, mailbox)
    base = start(client)
    client.get(f"{base}?otazky=1")
    response = post(client, f"{base}/otazka/fever", answer="yes", chat="1")
    assert follow(client, response) == f"{base}/vysledek"  # urgent never waits


def test_a_number_typed_for_a_number_question_counts_and_is_shown_back():
    extractor = AskedExtractor()
    app, _, mailbox = make_app(extract=extractor)
    client = logged_in(app, mailbox)
    base = start(client)
    client.get(f"{base}?otazky=1")
    follow(client, post(client, f"{base}/otazka/fever", answer="yes", chat="1"))
    follow(client, post(client, f"{base}/potvrdit/fever", chat="1"))
    assert "TEST Otázka na teplotu?" in client.get(base).text
    follow(client, say(client, base, text="38,4 °C", link_id="temperature"))
    assert extractor.calls == []  # a plain number needs no model
    page = client.get(base).text
    assert "Je to správně?" in page and "38,4 °C" in page


def test_a_quick_reply_returns_to_the_chat():
    app, _, mailbox = make_app()
    client = logged_in(app, mailbox)
    base = start(client)
    client.get(f"{base}?otazky=1")
    assert follow(client, post(client, f"{base}/otazka/fever", answer="no", chat="1")) == base
    follow(client, post(client, f"{base}/otazka/nausea", answer="no", chat="1"))
    page = client.get(base).text
    assert "Ano, odeslat hlášení" in page  # nothing more to ask: confirm and send from the chat
    response = post(client, f"{base}/odeslat", confirm="1")
    assert follow(client, response) == f"{base}/vysledek"


def test_after_a_noted_reading_the_fever_question_says_so_and_its_retry_names_the_options():
    """D55: "Mám teplotu 37" → 37 °C confirmed → the fever question says the reading is noted; a
    reply that only repeats the reading gets a retry naming the options, not "nerozuměli"."""

    def read(message, asked):
        return [] if asked else [quoted(message, "temperature", "present", value=37.0, quote="37")]

    app, store, mailbox = make_app(extract=AskedExtractor(read), plan=DEMO_PLAN)
    client = logged_in(app, mailbox)
    base = start(client)
    follow(client, say(client, base, text="TEST mám teplotu 37"))
    assert "Rozuměli jsme:" in client.get(base).text
    follow(client, post(client, f"{base}/potvrdit/temperature", chat="1"))
    page = client.get(base).text
    assert "Teplotu z teploměru jsme si zapsali: 37 °C." in page
    assert DEMO_PLAN.item("fever").text_cs in page
    follow(client, say(client, base, text="TEST právě jsem řekl, mám 37", link_id="fever"))
    page = client.get(base).text
    assert "Teplotu už máme zapsanou: 37 °C. Teď prosím vyberte jednu z možností: Ano, Ne, Nevím." in page
    assert "nerozuměli" not in page
    [checkin] = store.list_checkins(PATIENT_A.patient_id)
    assert checkin.history[-1].text.startswith("Teplotu už máme zapsanou")  # what was shown


# --- D56: bridge lines and the patient's previous report --------------------------------------


def _earlier_report(store, link_id="nausea", code="mild"):
    """A sent report from yesterday in which the patient reported `link_id`."""
    from datetime import timedelta

    from factories import T0, checkin

    from hlidac import wizard
    from hlidac.schemas import InboundMessage

    plan = make_app()[0].state.pipeline.plans
    item = next(iter(plan.values())).item(link_id)
    button = wizard.button_answer(item, code)
    message = InboundMessage(
        message_id="old-m", checkin_id="old", patient_id=PATIENT_A.patient_id,
        session_account_id=PATIENT_A.account_id, reported_by="patient",
        received_at=T0 - timedelta(days=1), kind="answer", answer=button,
    )  # fmt: skip
    found = wizard.finding_from_answer(button, "old-m")
    store.save_checkin(
        checkin(
            checkin_id="old", patient_id=PATIENT_A.patient_id, status="completed",
            started_at=T0 - timedelta(days=1), updated_at=T0 - timedelta(days=1),
            messages=[message], findings=[found],
        )
    )


def test_the_chat_thanks_the_patient_and_says_why_it_asks():
    app, store, mailbox = make_app(extract=AskedExtractor())
    client = logged_in(app, mailbox)
    base = start(client)
    follow(client, say(client, base, text="TEST mám se dobře"))
    page = client.get(base).text
    assert "Děkuji, rozumím. Zeptám se ještě na pár věcí" in page  # after the patient's words
    follow(client, post(client, f"{base}/otazka/fever", answer="yes", chat="1"))
    follow(client, post(client, f"{base}/potvrdit/fever", chat="1"))
    page = client.get(base).text
    assert "Ještě se zeptám podrobněji na teplotu a horečku." in page  # a follow-up says so
    [saved] = store.list_checkins(PATIENT_A.patient_id)
    shown = [e.text for e in saved.history if e.event == "question"]
    assert any(t.startswith("Děkuji, rozumím.") for t in shown)  # the history keeps the lead


def test_the_chat_picks_up_the_previous_report():
    app, store, mailbox = make_app(extract=AskedExtractor())
    _earlier_report(store)
    client = logged_in(app, mailbox)
    base = start(client)
    page = client.get(base).text
    assert "Při minulém hlášení jste uvedli nevolnost, jídlo a pití." in page
    follow(client, say(client, base, text="TEST nic nového"))
    follow(client, post(client, f"{base}/otazka/fever", answer="no", chat="1"))
    follow(client, post(client, f"{base}/potvrdit/fever", chat="1"))
    page = client.get(base).text
    assert "TEST Otázka na nevolnost?" in page
    assert "Při minulém hlášení jste to uvedli, proto se ptám, jak je to dnes." in page


def test_no_previous_report_means_no_history_line():
    app, _, mailbox = make_app(extract=AskedExtractor())
    client = logged_in(app, mailbox)
    assert "Při minulém hlášení" not in client.get(start(client)).text
