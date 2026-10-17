"""D59: the hands-free audio mode in a real browser. The app reads each step (silenced here:
`HLIDAC_AUDIO_SILENT`), listens on a fake microphone (a tone, so the patient's "Hotovo" ends each
recording), a stand-in transcriber returns scripted Czech replies, and the step loads in place
(no page reload). Checked: a spoken read-back and "ano" sends free text; an exact option goes in
at once (the closing summary confirms all answers, D58); "ne" asks again; the summary is read
and captioned, and "Ano" sends the report; the urgent screen ends audio mode with focus on its
heading; axe passes in every audio state. Also the fixed yes/no words and the end-of-speech detector. No patient data, no model.
"""

import collections
import socket
import threading
import time

import pytest
import uvicorn
from patient_helpers import make_app, urgent_on_fever
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright
from security.auth_helpers import PATIENT_A, PATIENT_B
from test_axe import check, log_in

HEARD: collections.deque[str] = collections.deque()


def scripted(audio, target):
    return HEARD.popleft() if HEARD else "TEST nic"


@pytest.fixture(scope="module")
def server():
    app, store, mailbox = make_app(
        process=urgent_on_fever, session_https_only=False, transcribe=scripted
    )
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    instance = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not instance.started:
        if time.monotonic() > deadline:
            raise RuntimeError("the test server did not start")
        time.sleep(0.05)
    yield f"http://localhost:{port}", mailbox, store
    instance.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        launched = None
        args = ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"]
        for options in ({"channel": "chrome"}, {}):
            try:
                launched = playwright.chromium.launch(args=args, **options)
                break
            except PlaywrightError:
                continue
        if launched is None:
            pytest.skip("no browser for the audio run: `uv run playwright install chromium`")
        yield launched
        launched.close()


def open_chat(browser, base, mailbox, account):
    page = log_in(browser, base, mailbox, account.email)
    page.context.grant_permissions(["microphone"])
    page.add_init_script("window.HLIDAC_AUDIO_SILENT = true")
    page.add_init_script(  # count the sound cues' notes (D59: a cue before each listening)
        """window.CUE_NOTES = [];
        const make = AudioContext.prototype.createOscillator;
        AudioContext.prototype.createOscillator = function () {
          const osc = make.call(this);
          const start = osc.start.bind(osc);
          osc.start = (at) => { window.CUE_NOTES.push(osc.frequency.value); start(at); };
          return osc;
        };"""
    )
    resume = page.get_by_role("link", name="Pokračovat v hlášení")  # one left open earlier
    if resume.count():
        resume.click()
    else:
        page.get_by_role("button", name="Nahlásit, jak se cítím").click()
    page.wait_for_load_state("load")
    return page


def say(page, *replies):
    """Speak each reply: queue it for the transcriber, then end the recording with "Hotovo"."""
    for reply in replies:
        HEARD.append(reply)
        stop = page.locator("#audio-stop")
        stop.wait_for(state="visible", timeout=15000)
        page.wait_for_timeout(700)  # "speaking" for a moment, as a patient would
        stop.click()
        deadline = time.monotonic() + 15
        while HEARD:  # the transcriber took it; the next recording starts after the reply
            assert time.monotonic() < deadline, f"nobody transcribed {reply!r}"
            page.wait_for_timeout(50)


def prompt(page):
    return page.locator("#prompt").inner_text()


def test_a_whole_check_in_by_voice(server, browser):
    base, mailbox, store = server
    HEARD.clear()
    page = open_chat(browser, base, mailbox, PATIENT_A)
    chat_url = page.url
    page.get_by_role("button", name="Hlasový režim").click()
    page.locator("#audio-stop").wait_for(state="visible")
    screen = page.get_by_role("dialog", name="Hlasový režim")
    assert screen.is_visible()  # the voice screen over the chat
    assert page.get_by_role("button", name="Ukončit hlasový režim").is_visible()
    check(page)  # listening
    assert page.evaluate("window.CUE_NOTES") == [660, 880]  # "speak now" before recording

    say(page, "TEST je mi zle", "ano")  # free text: read back, confirmed by voice
    page.wait_for_function("() => document.getElementById('prompt')?.innerText.includes('horečku')")
    assert page.url == chat_url  # loaded in place: no reload, so speech keeps working
    assert "TEST je mi zle" in page.locator(".chat-log").inner_text()

    say(page, "TEST Ne.")  # an exact option: in at once (D58: the summary confirms it)
    page.wait_for_function(
        "() => document.getElementById('prompt')?.innerText.includes('nevolnost')"
    )
    check(page)  # a question in audio mode

    say(page, "TEST trochu", "ne")  # free words: read back; "ne" sends nothing, asked again
    assert "nevolnost" in prompt(page)
    say(page, "TEST Mírná")
    page.wait_for_function(
        "() => document.querySelector('[data-audio-kind]')?.dataset.audioKind === 'summary'"
    )
    page.locator("#voice-answers").wait_for(state="visible")
    caption = page.locator("#voice-caption").inner_text()
    assert "Rozuměl jsem správně?" in caption and "TEST Mírná" in caption  # the rows, captioned
    check(page)  # the summary in audio mode
    page.locator("#voice-answers [data-tap=yes]").click()  # a tap on "Ano" sends the report
    page.wait_for_url("**/vysledek")
    page.wait_for_function("() => !window.HlidacAudio.on")
    assert page.get_by_role("button", name="Hlasový režim").is_visible()
    check(page)

    [checkin] = store.list_checkins(PATIENT_A.patient_id)
    assert checkin.status == "completed"
    voiced = [e for e in checkin.history if e.event in ("text", "answer")]
    assert [e.text for e in voiced] == ["TEST je mi zle", "TEST Ne", "TEST Mírná"]
    assert page.locator("#voice-screen").is_hidden()  # the result closes the voice screen
    assert all(e.via == "voice" for e in voiced)  # the "ne" read-back sent nothing
    notes = page.evaluate("window.CUE_NOTES")
    assert notes.count(880) >= 2 * 5 and 587 in notes  # a cue on every listen and every "got it"
    assert not HEARD


def test_the_urgent_screen_ends_audio_mode(server, browser):
    base, mailbox, _ = server
    HEARD.clear()
    page = open_chat(browser, base, mailbox, PATIENT_B)
    page.get_by_role("button", name="Hlasový režim").click()
    say(page, "TEST nejsem si jistý", "ano")
    page.wait_for_function("() => document.getElementById('prompt')?.innerText.includes('horečku')")
    say(page, "TEST Ano")
    page.wait_for_url("**/vysledek")
    page.wait_for_function("() => !window.HlidacAudio.on")
    assert page.evaluate("document.activeElement.id") == "result-heading"
    assert "Zavolejte prosím hned" in page.locator("#audio-status").inner_text()
    check(page)  # the urgent screen after audio mode


def test_yes_no_words_and_end_of_speech(server, browser):
    base, mailbox, _ = server
    page = open_chat(browser, base, mailbox, PATIENT_A)
    result = page.evaluate(
        """() => {
          const t = JSON.parse(document.getElementById('audio-mode').dataset.texts);
          const c = (s) => window.HlidacAudio.classify(s, t);
          const ended = window.HlidacVoice.silence({ quietMs: 2500 });
          const steps = [];
          let now = 0;
          const feed = (level, ms) => { for (let i = 0; i < ms; i += 50) {
            now += 50; steps.push(ended(level, now)); } };
          feed(0.002, 5000);            // a quiet room: never ends before speech
          const beforeSpeech = steps.some(Boolean);
          feed(0.2, 1000);              // speech
          feed(0.002, 2000);            // a short pause: still listening
          const shortPause = steps.some(Boolean);
          feed(0.002, 600);             // 2.6 s of quiet in total: the end
          return {
            yes: [c('Ano.'), c('Jo, je to správně'), c('ANO'), c('spravne')],
            no: [c('Ne.'), c('ne, opravit'), c('Špatně')],
            unclear: [c('asi'), c('nevím'), c('')],
            beforeSpeech, shortPause, ended: steps[steps.length - 1],
            exact: [
              window.HlidacAudio.exactAnswer('Test ne.', ['TEST Ne'], false),
              window.HlidacAudio.exactAnswer('38,5 °C', [], true),
              window.HlidacAudio.exactAnswer('asi 38', [], true),
            ],
          };
        }"""
    )
    assert result["yes"] == ["yes"] * 4
    assert result["no"] == ["no"] * 3
    assert result["unclear"] == [None] * 3  # "nevím" is never a yes or a no
    assert not result["beforeSpeech"] and not result["shortPause"] and result["ended"]
    assert result["exact"] == [True, True, False]


def test_the_voice_screen_closes_by_button_or_escape(server, browser):
    base, mailbox, _ = server
    HEARD.clear()
    page = open_chat(browser, base, mailbox, PATIENT_A)
    for close in ("button", "escape"):
        page.get_by_role("button", name="Hlasový režim").click()
        dialog = page.get_by_role("dialog", name="Hlasový režim")
        dialog.wait_for()
        assert page.evaluate("document.querySelector('main').inert")  # the chat is behind it
        page.locator("#audio-stop").wait_for(state="visible")
        if close == "button":
            page.get_by_role("button", name="Ukončit hlasový režim").click()
        else:
            page.keyboard.press("Escape")
        dialog.wait_for(state="hidden")
        assert not page.evaluate("document.querySelector('main').inert")
        assert page.evaluate("document.activeElement.id") == "audio-toggle"  # focus returns
        assert not page.evaluate("window.HlidacAudio.on")
    check(page)
