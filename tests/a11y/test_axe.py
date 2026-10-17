"""WP15: automated accessibility checks (accessibility.md §7 step 1, plus parts of steps 4–5).

The app runs in a real server on the in-memory store with the invented TEST plan; a real browser
(system Chrome, or Playwright's Chromium) walks the check-in by clicking, like a patient would, and
runs axe-core on every screen:
- WCAG 2.0–2.2 A and AA plus axe best practices on every page: zero violations;
- on patient pages also AAA contrast (7:1, 1.4.6);
- targets ≥ 48 × 48 px, and no horizontal scrolling at 320 px width and at 200 % text.

Keyboard and screen-reader passes stay manual (§7 steps 2–3); log them by hand.
No browser installed: the test is skipped with a reason (`uv run playwright install chromium`).
"""

import json
import socket
import threading
import time
from datetime import timedelta
from pathlib import Path

import axe_playwright_python
import pytest
import uvicorn
from factories import T0, assessment, email_item
from patient_helpers import make_app, urgent_on_fever
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright
from security.auth_helpers import NURSE, PATIENT_A, PATIENT_B, PATIENT_C

from hlidac.advice import load_cards
from hlidac.schemas import Delivery, Notification

AXE_JS = (Path(axe_playwright_python.__file__).parent / "axe.min.js").read_text(encoding="utf-8")
AA_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"]
# Controls the patient taps; inline text links are exempt (WCAG 2.5.8 inline exception).
TARGETS = "main button, main .button, main .option, main .choice, main input, main textarea"
MIN_TARGET_PX = 48


CARDS = load_cards()
HEARD = "TEST přepis hlasu"  # the stand-in transcriber's reply (WP17); no model runs in tests
CARD = CARDS[("nausea-mild", "1")]


def urgent_for_patient_b(store):
    """The urgent stand-in for patient B only, so patient A can walk every question. Patient A's
    sent report gets a stand-in `no_escalation` with the real advice card, so axe sees the card."""
    urgent = urgent_on_fever(store)

    def process(checkin, plan, now):
        if checkin.patient_id == PATIENT_B.patient_id:
            return urgent(checkin, plan, now)
        if checkin.status != "completed":
            return None
        result = assessment(
            assessment_id=f"a-card-{checkin.checkin_id}",
            checkin_id=checkin.checkin_id,
            patient_id=checkin.patient_id,
            findings=list(checkin.findings),
            grades=[],
            questionnaire_url=plan.questionnaire_url,
            questionnaire_version=plan.questionnaire_version,
            advice_card_id=CARD.card_id,
            advice_card_version=CARD.version,
        )
        store.add_assessment(result)
        return result

    return process


def seed_notifications(store) -> None:
    """WP16: patient A has a read and an unread notification; the nurse one with an email."""
    sent = email_item("o-axe").evolve(status="done", processed_at=T0)  # never resent at login
    store.enqueue(sent)
    for minutes, (nid, account, kind, template, link, read, email) in enumerate(
        (
            ("n-a1", PATIENT_A, "team_notified", "team-notified", "/pacient", T0, None),
            ("n-a2", PATIENT_A, "team_notified", "team-notified", "/pacient", None, None),
            ("n-n1", NURSE, "urgent_alert", "urgent-alert", "/sestra", None, sent.outbox_id),
        )
    ):
        deliveries = [Delivery(channel="in_app", at=T0)]
        if email:
            deliveries.append(Delivery(channel="email", at=T0, outbox_id=email))
        store.save_notification(
            Notification(
                notification_id=nid,
                recipient_account_id=account.account_id,
                kind=kind,
                template_id=template,
                template_version="1",
                link=link,
                created_at=T0 + timedelta(minutes=minutes),  # n-a2 is the newest
                read_at=read,
                deliveries=deliveries,
            )
        )


@pytest.fixture(scope="module")
def server():
    # Plain http on localhost in the test, so the Secure session cookie is not required.
    app, store, mailbox = make_app(
        process=urgent_for_patient_b, advice_cards=CARDS, session_https_only=False,
        demo_panel=True, transcribe=lambda audio, target: HEARD,
    )
    seed_notifications(store)
    # The urgent screen with the team's line first (D54), the variant patients see in the demo.
    escalation = app.state.escalation
    [team] = escalation.teams.values()
    app.state.escalation = escalation.model_copy(update={"teams": {"t1": team}})
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    instance = uvicorn.Server(config)
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not instance.started:
        if time.monotonic() > deadline:
            raise RuntimeError("the test server did not start")
        time.sleep(0.05)
    yield f"http://localhost:{port}", mailbox
    instance.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        launched = None
        # A fake microphone (a tone) so the voice test records without a device or a prompt.
        args = ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"]
        for options in ({"channel": "chrome"}, {}):
            try:
                launched = playwright.chromium.launch(args=args, **options)
                break
            except PlaywrightError:
                continue
        if launched is None:
            pytest.skip("no browser for the axe run: `uv run playwright install chromium`")
        yield launched
        launched.close()


def axe(page: Page, options: dict) -> list[dict]:
    page.evaluate(AXE_JS)
    result = page.evaluate("options => axe.run(document, options)", options)
    return result["violations"]


def describe(violations: list[dict]) -> str:
    lines = []
    for violation in violations:
        targets = [node["target"] for node in violation["nodes"]][:3]
        lines.append(f"{violation['id']}: {violation['help']} {json.dumps(targets)}")
    return "\n".join(lines)


def check(page: Page, patient: bool = True) -> None:
    """Every check on the page as it is now."""
    where = page.url
    violations = axe(page, {"runOnly": {"type": "tag", "values": AA_TAGS}})
    assert not violations, f"{where}\n{describe(violations)}"
    if patient:
        options = {"runOnly": {"type": "rule", "values": ["color-contrast-enhanced"]}}
        violations = axe(page, options)
        assert not violations, f"{where} (AAA contrast)\n{describe(violations)}"

    small = page.eval_on_selector_all(
        TARGETS,
        """(nodes, min) => nodes
            .filter(n => n.type !== 'hidden' && n.type !== 'radio')
            .filter(n => n.getClientRects().length)  // not rendered (hidden): not a target
            .map(n => [n.outerHTML.slice(0, 80), n.getBoundingClientRect()])
            .filter(([, r]) => r.width < min || r.height < min)
            .map(([html, r]) => `${html} ${Math.round(r.width)}x${Math.round(r.height)}`)""",
        MIN_TARGET_PX,
    )
    assert not small, f"{where}: targets under {MIN_TARGET_PX} px: {small}"

    for width, text_size in ((320, "100%"), (400, "200%")):
        page.set_viewport_size({"width": width, "height": 800})
        page.evaluate("size => document.documentElement.style.fontSize = size", text_size)
        overflow = page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        assert overflow <= 0, f"{where}: scrolls sideways at {width} px, text {text_size}"
    page.evaluate("document.documentElement.style.fontSize = ''")
    page.set_viewport_size({"width": 390, "height": 844})


def log_in(browser, base: str, mailbox, email: str) -> Page:
    page = browser.new_context(viewport={"width": 390, "height": 844}).new_page()
    page.goto(f"{base}/prihlaseni")
    check(page)
    sent = len(mailbox.sent)
    page.get_by_label("Váš e-mail").fill(email)
    page.get_by_role("button", name="Poslat kód").click()
    page.wait_for_url("**/prihlaseni/kod")
    check(page)
    deadline = time.monotonic() + 10
    while len(mailbox.sent) == sent:  # the code goes out in a background task
        assert time.monotonic() < deadline, "no login email"
        time.sleep(0.05)
    page.get_by_label("Kód z e-mailu").fill(mailbox.last_code())
    page.get_by_role("button", name="Přihlásit se").click()
    page.wait_for_load_state()
    return page


def test_check_in_flow_has_no_violations(server, browser):
    """The chat: the opening prompt, an error, quick replies, a typed number and an unclear reply
    asked again, then the closing "Rozuměl jsem správně?" (D58) and sending from the chat."""
    base, mailbox = server
    page = log_in(browser, base, mailbox, PATIENT_A.email)
    check(page)  # home
    page.get_by_role("button", name="Nahlásit, jak se cítím").click()
    page.wait_for_load_state()
    check(page)  # the chat's opening prompt

    send = page.get_by_role("button", name="Odeslat zprávu")
    send.click()
    page.wait_for_load_state()
    check(page)  # the empty-message error
    page.get_by_label("Vaše zpráva").fill("TEST text")
    send.click()
    page.wait_for_load_state()
    assert "TEST Otázka na horečku?" in page.locator("#prompt").inner_text()
    check(page)  # a question with quick replies and the log
    page.wait_for_load_state("load")
    assert page.evaluate("document.activeElement.id") == "prompt", "focus on the new question"

    page.get_by_role("button", name="TEST Ano").click()
    page.wait_for_load_state()

    # D58: no confirmation after each answer; the closing summary confirms them all
    assert "TEST Otázka na teplotu?" in page.locator("#prompt").inner_text()
    check(page)  # the number question
    page.get_by_label("Nebo odpovězte vlastními slovy").fill("horko")
    send.click()
    page.wait_for_load_state()
    assert "nerozuměli" in page.content()
    check(page)  # asked again
    page.get_by_label("Nebo odpovězte vlastními slovy").fill("38,4")
    send.click()
    page.wait_for_load_state()
    assert "38,4" in page.content()
    assert "TEST Otázka na nevolnost?" in page.locator("#prompt").inner_text()
    page.get_by_role("button", name="Nevím").click()
    page.wait_for_load_state()
    assert page.locator("#prompt").inner_text() == "Rozuměl jsem správně?"
    assert page.evaluate("document.activeElement.id") == "prompt", "focus on the summary"
    check(page)  # the closing summary with its fix links
    page.get_by_role("button", name="Ano, odeslat hlášení").click()
    page.wait_for_url("**/vysledek")
    check(page)  # the result


def test_urgent_screen_has_no_violations(server, browser):
    base, mailbox = server
    page = log_in(browser, base, mailbox, PATIENT_B.email)
    page.get_by_role("button", name="Nahlásit, jak se cítím").click()
    page.get_by_text("Jiné možnosti").click()
    page.get_by_role("link", name="Odpovídat na otázky").click()
    page.wait_for_url("**/otazka/fever")
    check(page)  # the one-question-per-screen path is still there
    page.get_by_role("button", name="Pokračovat").click()
    check(page)  # "choose one" error
    page.get_by_label("TEST Ano").check()
    check(page)  # a selected option
    page.get_by_role("button", name="Pokračovat").click()

    page.wait_for_url("**/vysledek")
    check(page)  # the urgent screen
    assert page.get_by_role("link", name="Onkologická ambulance: +420 000 000 000").is_visible()
    page.wait_for_load_state("load")  # the deferred focus script has run
    focused = page.evaluate("document.activeElement.id")
    assert focused == "result-heading", "the urgent screen moves focus to its heading"


def test_no_plan_home_and_clinician_pages_have_no_violations(server, browser):
    base, mailbox = server
    page = log_in(browser, base, mailbox, PATIENT_C.email)
    check(page)  # home without a Questionnaire
    page.goto(f"{base}/sestra")
    check(page)  # the 403 page

    nurse = log_in(browser, base, mailbox, NURSE.email)
    check(nurse, patient=False)  # the queue, with patient B's urgent alert
    nurse.get_by_role("link", name="Otevřít").first.click()
    nurse.wait_for_url("**/sestra/pripad/**")
    check(nurse, patient=False)  # the case view
    nurse.get_by_role("button", name="Potvrdit stupeň").first.click()
    nurse.wait_for_load_state()
    check(nurse, patient=False)  # after a confirmation
    nurse.get_by_role("link", name="Záznam případu").click()
    nurse.wait_for_url("**/zaznam")
    check(nurse, patient=False)  # the case's audit trail (WP25)
    nurse.get_by_role("link", name="Pravidla").click()
    nurse.wait_for_url("**/sestra/pravidla")
    check(nurse, patient=False)  # plans and rules (WP25)
    nurse.get_by_role("link", name="Nevyřešené").click()
    nurse.wait_for_url("**/sestra/nevyresene")
    check(nurse, patient=False)
    nurse.get_by_role("link", name="Skupiny").click()
    nurse.wait_for_url("**/sestra/skupiny")
    check(nurse, patient=False)  # patients by illness group (D50)


def test_notification_pages_have_no_violations(server, browser):
    base, mailbox = server
    page = log_in(browser, base, mailbox, PATIENT_A.email)
    page.get_by_role("link", name="Nová oznámení").click()
    page.wait_for_url("**/pacient/oznameni")
    check(page)  # a read and an unread notification
    page.get_by_role("button", name="Otevřít").first.click()  # the newest, unread one
    page.wait_for_url("**/pacient")
    page.get_by_role("link", name="Nová oznámení").click()
    page.wait_for_url("**/pacient/oznameni")
    assert "Nová oznámení: 0" in page.content()
    check(page)  # all read

    nurse = log_in(browser, base, mailbox, NURSE.email)
    nurse.get_by_role("link", name="Oznámení").click()
    nurse.wait_for_url("**/sestra/oznameni")
    assert "odesláno" in nurse.content()
    check(nurse, patient=False)


def test_demo_panel_has_no_violations(server, browser):
    # Last in the file: it moves the shared server's demo clock.
    base, mailbox = server
    nurse = log_in(browser, base, mailbox, NURSE.email)
    nurse.get_by_role("link", name="Demo panel").click()
    nurse.wait_for_url("**/demo")
    check(nurse, patient=False)
    nurse.get_by_role("button", name="Posunout čas o 15 minut").click()
    nurse.wait_for_load_state()
    assert "Čas posunut o 15 minut" in nurse.content()
    check(nurse, patient=False)  # with the status message


def test_voice_records_uploads_and_fills_the_text_box(server, browser):
    """WP17 / S2: a real browser records (fake microphone), turns it into a 16 kHz WAV, uploads it
    with the CSRF header, and the transcript lands in the text box to check; axe passes in every
    state. Nothing is sent until the patient presses Odeslat zprávu."""
    base, mailbox = server
    page = log_in(browser, base, mailbox, PATIENT_A.email)
    page.context.grant_permissions(["microphone"])
    page.get_by_role("button", name="Nahlásit, jak se cítím").click()
    page.wait_for_load_state("load")
    toggle = page.get_by_role("button", name="Namluvit")
    check(page)

    toggle.click()
    stop = page.get_by_role("button", name="Ukončit nahrávání")
    stop.wait_for()
    assert "Nahrávám" in page.locator("#voice-status").inner_text()
    check(page)  # the recording state
    page.wait_for_timeout(800)
    stop.click()

    text = page.get_by_label("Vaše zpráva")
    page.wait_for_function(
        "heard => document.getElementById('text').value === heard", arg=HEARD, timeout=15000
    )
    assert "Rozuměli jsme" in page.locator("#voice-status").inner_text()
    assert text.evaluate("node => node === document.activeElement")
    check(page)  # the transcript to check
    page.get_by_role("button", name="Odeslat zprávu").click()
    page.wait_for_load_state()
    assert HEARD in page.locator(".chat-log").inner_text()
