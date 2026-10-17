"""The patient web app (WP9): home, the check-in wizard and the result screen, in Czech.

- Every route takes the patient from the session (`require_role("patient")`), never from the URL.
  A check-in id in a path is checked with `ensure_patient_access` before anything is read.
- One question per screen; every answer is saved at once (no time limits, nothing lost), and the
  pipeline runs after each one, so an urgent answer shows the urgent screen straight away.
- Written text is read by the model gateway (WP8) into quoted findings, and then only the missing
  items are asked (WP5, `clarification`). A quoted reported red flag or number is shown back for
  a button confirmation before it counts (G5, D49). Every other consequential answer is confirmed
  once, together, in the chat's closing "Rozuměl jsem správně?" (D58), not after each question. If extraction is off or fails, the text is kept for
  the nurse on the free-text item (a routine review, whatever the buttons say) and every question
  is asked with buttons.
- Every step is kept in order on the check-in (`Checkin.history`, D46): each question and
  confirmation shown, each message and answer given, sending and the result screen.
- Patients see only approved text: Questionnaire items, the result texts in
  `data/content/patient/`, advice cards and notification templates (WP16). Never a grade, a rule
  or model output.
"""

import contextlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from hlidac import audit, clarification, extraction, fhir_mapping, processing, transcription, wizard
from hlidac.groups import default_groups
from hlidac.pipeline import Pipeline
from hlidac.schemas import (
    APP_EVENTS,
    DONT_KNOW,
    TYPED_VALUE,
    Account,
    Assessment,
    ChatEntry,
    Checkin,
    Finding,
    InboundMessage,
    MonitoringPlan,
    PlanItem,
    QuoteSource,
)
from hlidac.security.access import ensure_patient_access, require_role
from hlidac.store import Store
from hlidac.ui import inbox, render
from hlidac.ui.patient.content import chat_texts, result_texts

_patient = require_role("patient")
router = APIRouter(prefix="/pacient", dependencies=[Depends(_patient)])
Patient = Annotated[Account, Depends(_patient)]


CHOOSE_ONE = "Vyberte prosím jednu možnost."
TYPE_NUMBER = "Napište prosím číslo, nebo zvolte Nevím. Desetinné číslo můžete napsat s čárkou."
WRITE_SOMETHING_CHAT = "Napište prosím odpověď, nebo vyberte jednu z možností."
WRITE_SOMETHING = "Napište prosím, jak se cítíte, nebo se vraťte a odpovídejte na otázky."
TOO_LONG = f"Text je moc dlouhý. Zkraťte ho prosím na {wizard.MAX_TEXT_LENGTH} znaků."
# Voice (WP17): fixed texts only; the transcript itself is the patient's own words, shown back in
# the text box to check and correct before anything is sent (G5).
VOICE_BAD_AUDIO = "Nahrávku se nepodařilo přečíst. Zkuste to prosím znovu, nebo text napište."
VOICE_NOTHING_HEARD = "Nic jsme neslyšeli. Zkuste to prosím znovu, nebo text napište."
VOICE_FAILED = "Přepis se nepovedl. Zkuste to prosím znovu, nebo text napište."
VOICE_OFF = "Namlouvání teď není k dispozici. Napište prosím text."

# The patient sees the urgent screen on the first urgent hit, and the team is notified at once.
# The 155 button is intentionally stricter: only several independent severe signals together show
# the ambulance action, so a single broad phrase or one urgent rule still sends the case to the
# team without telling the patient to call an ambulance straight away.
_EMERGENCY_THRESHOLD = 3
_EMERGENCY_PHRASES = frozenset(
    {
        "krev ve stolici",
        "krve ve stolici",
        "krvavou stolici",
        "krvavý průjem",
        "černou stolici",
        "zvracím krev",
        "zvracel krev",
        "zvracela krev",
        "vykašlávám krev",
        "kašlu krev",
        "krev v moči",
        "silně krvácím",
        "krvácení nejde zastavit",
        "nemůžu dýchat",
        "nemohu dýchat",
        "nemůžu se nadechnout",
        "bolest na hrudi",
        "bolest na hrudníku",
        "bolí mě na hrudi",
        "tlak na hrudi",
        "tlak na prsou",
        "omdlel",
        "bezvědomí",
        "ztratil vědomí",
        "ztratila vědomí",
        "záchvat křečí",
        "epileptický záchvat",
        "nemůžu mluvit",
        "ochrnul",
        "otok jazyka",
        "oteklý jazyk",
        "otok obličeje",
        "otekly mi rty",
        "nemohu pít",
        "nemůžu pít",
        "nic neudržím",
        "všechno vyzvracím",
        "vše vyzvracím",
        "nesnesitelná bolest",
        "nechci žít",
        "zabít se",
    }
)


class _Urgent(Exception):
    """The pipeline found the report urgent: stop asking and show the urgent screen."""


class _Failed(Exception):
    """The pipeline failed; the check-in is `processing_failed` and visible to the team."""


def _page(request: Request, name: str, status_code: int = 200, **context: object) -> HTMLResponse:
    """Every patient page in the messenger frame (D53): the chat header needs the fixed chat
    texts and the treatment day from the hospital record."""
    account = context.get("account")
    line = _context_line(request, account) if isinstance(account, Account) else None
    return render(
        request, name, status_code, chat=chat_texts(), context_line=line, **context
    )  # fmt: skip


def _context_line(request: Request, account: Account) -> str | None:
    """The header's treatment day: whole days since the last dose in the hospital record, or
    None (no record, no reader, or a failed read: a page never waits on or fails for it)."""
    read = request.app.state.pipeline.context
    if read is None or account.patient_id is None:
        return None
    try:
        context = read(account.patient_id)
    except Exception:  # noqa: BLE001 — the line is optional; the check-in must keep working
        return None
    if context is None or context.last_sact_dose_at is None:
        return None
    now = request.app.state.clock.now()
    days = (now.date() - context.last_sact_dose_at.astimezone(now.tzinfo).date()).days
    if days < 0:
        return None
    texts = chat_texts()
    return texts.context_today if days == 0 else texts.context_day.format(day=days)


def _store(request: Request) -> Store:
    return request.app.state.store


def _pipeline(request: Request) -> Pipeline:
    return request.app.state.pipeline


def _now(request: Request) -> datetime:
    return request.app.state.clock.now()


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def _base(checkin: Checkin) -> str:
    return f"/pacient/hlaseni/{checkin.checkin_id}"


def _patient_plan(request: Request, account: Account) -> MonitoringPlan | None:
    assignment = _store(request).get_patient_plan(account.patient_id or "")
    if assignment is None:
        return None
    return _pipeline(request).plan(assignment.questionnaire_url, assignment.questionnaire_version)


def _own_checkin(request: Request, account: Account, checkin_id: str) -> Checkin:
    checkin = _store(request).get_checkin(checkin_id)
    if checkin is None:
        raise HTTPException(status_code=404)
    ensure_patient_access(account, checkin.patient_id, _store(request))
    return checkin


def _plan_of(request: Request, checkin: Checkin) -> MonitoringPlan:
    plan = _pipeline(request).plan(checkin.questionnaire_url, checkin.questionnaire_version)
    if plan is None:  # the Questionnaire version was withdrawn after the check-in started
        raise HTTPException(status_code=404)
    return plan


def _open_checkin(request: Request, account: Account) -> Checkin | None:
    checkins = _store(request).list_checkins(account.patient_id or "")
    return next((c for c in reversed(checkins) if c.status == "in_progress"), None)


def _history(request: Request, plan: MonitoringPlan, checkin: Checkin) -> frozenset[str]:
    """D56: the symptoms this patient reported in their previous sent report (our own store, the
    session's patient only; item ids, never sent to a model). Empty if the read fails."""
    try:
        earlier = [
            c
            for c in _store(request).list_checkins(checkin.patient_id)
            if c.checkin_id != checkin.checkin_id and c.started_at <= checkin.started_at
        ]
    except Exception:  # noqa: BLE001 — history only adds questions; the check-in keeps working
        return frozenset()
    return clarification.reported_before(plan, earlier)


def _questions(request: Request, plan: MonitoringPlan, checkin: Checkin) -> list[PlanItem]:
    attributes = _pipeline(request).grading_attributes
    return clarification.questions(plan, checkin, attributes, _history(request, plan, checkin))


def _next(request: Request, plan: MonitoringPlan, checkin: Checkin) -> PlanItem | None:
    attributes = _pipeline(request).grading_attributes
    return clarification.next_item(plan, checkin, attributes, _history(request, plan, checkin))


def _progress(
    request: Request, plan: MonitoringPlan, checkin: Checkin, item: PlanItem
) -> tuple[int, int, PlanItem | None]:
    """Position and total of `item` among the questions this check-in asks, and the one before."""
    items = _questions(request, plan, checkin)
    if item not in items:  # an item answered from the summary after the turns ran out
        items = [*items, item]
    position = items.index(item)
    return position + 1, len(items), items[position - 1] if position else None


def _item(plan: MonitoringPlan, checkin: Checkin, link_id: str) -> PlanItem:
    item = next((i for i in wizard.question_items(plan, checkin) if i.link_id == link_id), None)
    if item is None:
        raise HTTPException(status_code=404)
    return item


def _finding(checkin: Checkin, link_id: str) -> Finding | None:
    return next((f for f in checkin.findings if f.link_id == link_id), None)


def _save(request: Request, checkin: Checkin, **changes: object) -> Checkin:
    saved = checkin.evolve(updated_at=_now(request), **changes)
    _store(request).save_checkin(saved)
    return saved


def _step(request: Request, checkin: Checkin, event: str, text: str, **fields) -> list[ChatEntry]:
    """The check-in's history with one more step (D46)."""
    entry = ChatEntry(at=_now(request), event=event, text=text, **fields)
    return [*checkin.history, entry]


def _shown(request: Request, checkin: Checkin, event: str, text: str, **fields) -> Checkin:
    """Record a screen the app showed. A reload of the same screen is not recorded twice."""
    last = checkin.history[-1] if checkin.history else None
    link_id = fields.get("link_id")
    if last is not None and (last.event, last.text, last.link_id) == (event, text, link_id):
        return checkin
    saved = checkin.evolve(history=_step(request, checkin, event, text, **fields))
    _store(request).save_checkin(saved)
    return saved


def _run_pipeline(request: Request, checkin: Checkin, plan: MonitoringPlan) -> None:
    """Run after every saved change. Urgent stops the questions at once; a failure stays visible."""
    process = _pipeline(request).process
    if process is None:
        return
    try:
        assessment = process(wizard.for_assessment(plan, checkin), plan, _now(request))
    except Exception as error:  # any failure must end visible, never as success
        reason = f"processing error: {type(error).__name__}"  # no patient text in the reason
        _save(request, checkin, status="processing_failed", failure=reason, pending_link_id=None)
        audit.record(
            _store(request), _now(request), "system", "checkin_processing_failed",
            target=f"checkin:{checkin.checkin_id}", error=type(error).__name__,
        )  # fmt: skip
        raise _Failed from error
    if assessment is not None and assessment.status == "urgent":
        _save(request, checkin, status="completed", pending_link_id=None)
        raise _Urgent


def _after_change(request: Request, checkin: Checkin, plan: MonitoringPlan, then: str):
    try:
        _run_pipeline(request, checkin, plan)
    except (_Urgent, _Failed):
        then = f"{_base(checkin)}/vysledek"
    _write_back(request, checkin.checkin_id, plan)
    return _redirect(then)


def _write_back(request: Request, checkin_id: str, plan: MonitoringPlan) -> None:
    """Queue the FHIR write (WP12) once the report is sent, urgent or failed. A partial report
    stays in our SQL record until then. Never blocks the result: a failure here is audited and
    the patient still sees the urgent screen (urgent never waits)."""
    store = _store(request)
    checkin = store.get_checkin(checkin_id)
    if checkin is None or checkin.status == "in_progress":
        return
    assessments = _assessments(store, checkin)
    assessment = assessments[-1] if assessments else None
    alert = None
    if assessment is not None:
        alerts = store.list_alerts(patient_id=checkin.patient_id)
        alert = next((a for a in alerts if a.assessment_id == assessment.assessment_id), None)
    try:
        fhir_mapping.enqueue_checkin(store, checkin, plan, assessment, alert, _now(request))
    except Exception as error:  # noqa: BLE001 — the write-back must never hide the result
        audit.record(
            store, _now(request), "system", "fhir_enqueue_failed",
            target=f"checkin:{checkin_id}", error=type(error).__name__,
        )  # fmt: skip


def _assessments(store: Store, checkin: Checkin) -> list[Assessment]:
    """This check-in's assessments, oldest first."""
    found = store.list_assessments(checkin.patient_id)
    return [a for a in found if a.checkin_id == checkin.checkin_id]


# --- Home and start -------------------------------------------------------------------------


@router.get("")
def home(request: Request, account: Patient) -> HTMLResponse:
    plan = _patient_plan(request, account)
    return _page(
        request,
        "patient/home.html",
        account=account,
        has_plan=plan is not None,
        open_checkin=_open_checkin(request, account),
        unread=inbox.unread(_store(request), account),
        texts=result_texts(),
        home_page=True,
    )


# --- Notifications (WP16) -------------------------------------------------------------------


@router.get("/oznameni")
def notification_list(request: Request, account: Patient) -> HTMLResponse:
    store = _store(request)
    audit.record(store, _now(request), account.account_id, "view_notifications")
    return _page(
        request,
        "patient/notifications.html",
        account=account,
        entries=inbox.entries(store, account),
    )


@router.post("/oznameni/{notification_id}")
def open_notification(request: Request, account: Patient, notification_id: str) -> RedirectResponse:
    return inbox.open_one(request, account, notification_id, "/pacient")


@router.post("/hlaseni")
def start_checkin(request: Request, account: Patient) -> RedirectResponse:
    """Start a report, or continue the one already open (partial answers are kept)."""
    existing = _open_checkin(request, account)
    if existing is not None:
        return _redirect(_base(existing))
    plan = _patient_plan(request, account)
    if plan is None or account.patient_id is None:
        return _redirect("/pacient")
    now = _now(request)
    checkin = Checkin(
        checkin_id=secrets.token_hex(16),
        patient_id=account.patient_id,
        questionnaire_url=plan.questionnaire_url,
        questionnaire_version=plan.questionnaire_version,
        reported_by="patient",
        started_at=now,
        updated_at=now,
    )
    _store(request).save_checkin(checkin)
    audit.record(
        _store(request), now, account.account_id, "checkin_started",
        target=f"checkin:{checkin.checkin_id}",
    )  # fmt: skip
    return _redirect(_base(checkin))


# --- The chat: the check-in as a conversation -----------------------------------------------
#
# The app's side of the chat is only approved text: the opening prompt (feature-description.md §2),
# Questionnaire items, the G5 confirmation and the fixed texts below; never model output. The
# patient answers each question by typing, dictating (voice.js, checked in the box before sending)
# or tapping a quick reply (the item's approved options). A typed reply that is exactly an option,
# or a number for a number question, counts like the button; anything else is read by the model
# gateway with the question as a hint (WP8), and clarification (WP5) picks the next question. The
# pipeline runs after every message, so an urgent reply goes to the urgent screen at once.

CHAT_OPENING = "Jak se dnes cítíte? Popište nám případné potíže a změny v užívání léků."
CHAT_OPENING_HINT = "Napište nebo namluvte vlastními slovy. Ptát se budeme jen na to, co chybí."
CHAT_RETRY = (
    "Této odpovědi jsme úplně nerozuměli. Vyberte prosím jednu z možností, nebo to napište jinak."
)
CHAT_CONFIRM = "Je to správně?"
_UNITS = re.compile(r"\s*(°\s*c|°|stupňů|stupně|stupeň|c)\.?\s*$")


@dataclass(frozen=True)
class _Prompt:
    kind: str  # opening | question | confirm | summary
    text: str
    item: PlanItem | None = None
    quote: str | None = None
    answer_text: str | None = None
    retry: bool = False
    lead: str | None = None  # said before the question: D55 reading note, else D56 bridge
    rows: tuple[tuple[str, str, str | None], ...] = ()  # summary: (label, answer, link id to fix)


def _chat_prompt(
    request: Request, plan: MonitoringPlan, checkin: Checkin, fix: str, ask: bool
) -> _Prompt:
    """What the chat asks now: the opening prompt, the next question, a confirmation of the
    model's reading (D49), or the closing summary "Rozuměl jsem správně?" (D58)."""
    if fix:
        return _Prompt("question", "", _item(plan, checkin, fix))
    if not checkin.messages and not ask and not any(e.link_id for e in checkin.history):
        return _Prompt("opening", CHAT_OPENING)
    item = _next(request, plan, checkin)
    if item is None:
        texts = chat_texts()
        rows = tuple(_summary_rows(plan, checkin))
        return _Prompt("summary", texts.summary_question, rows=rows, lead=_summary_lead(checkin))
    finding = _finding(checkin, item.link_id)
    if finding is not None and wizard.awaits_confirmation(item, finding):
        quote = finding.source.quote if isinstance(finding.source, QuoteSource) else None
        return _Prompt("confirm", "", item, quote, wizard.answer_text(item, finding))
    asked = [e for e in checkin.history if e.event == "question"]
    retry = (
        bool(checkin.history)
        and checkin.history[-1].event == "text"
        and bool(asked)
        and asked[-1].link_id == item.link_id
    )
    # D55's note on a noted reading first (its retry names the options), else D56's bridge line
    lead = _reading_lead(plan, checkin, item, retry) or (
        None if retry else _lead(request, plan, checkin, item)
    )
    return _Prompt("question", "", item, retry=retry, lead=lead)


def _label(item: PlanItem) -> str:
    """The item's short name in the summary: the core name, else its group's, else the question."""
    label = chat_texts().summary_labels.get(item.link_id) or default_groups().labels().get(
        item.link_id
    )
    return label[:1].upper() + label[1:] if label else item.text_cs


def _summary_rows(plan: MonitoringPlan, checkin: Checkin) -> list[tuple[str, str, str | None]]:
    """D58: what the chat understood, in Questionnaire order: each answered question with the
    approved option text or the number given, then the patient's own words on the free-text item
    (the model's verbatim quote, no fix link: words already said can only add)."""
    rows: list[tuple[str, str, str | None]] = []
    for item in wizard.question_items(plan, checkin):
        finding = _finding(checkin, item.link_id)
        if finding is not None and wizard.is_done(item, checkin):
            rows.append((_label(item), wizard.answer_text(item, finding), item.link_id))
    texts = chat_texts()
    for item in (i for i in plan.items if i.type == "text"):
        finding = _finding(checkin, item.link_id)
        if finding is not None and isinstance(finding.source, QuoteSource):
            rows.append((texts.summary_other, f"„{finding.source.quote}“", None))
    return rows


def _summary_lead(checkin: Checkin) -> str | None:
    """Thanks before the summary: for something added under an earlier summary, or a reply."""
    texts = chat_texts()
    last = next((e for e in reversed(checkin.history) if e.event not in APP_EVENTS), None)
    summary_before = any(
        e.event == "question" and texts.summary_question in e.text for e in checkin.history
    )
    if last is not None and last.event == "text" and summary_before:
        return texts.ack_added
    if last is not None and last.event in ("answer", "confirmed"):
        replies = sum(e.event in ("answer", "confirmed") for e in checkin.history)
        return texts.ack_answer[(replies - 1) % len(texts.ack_answer)]
    return None


def _group_topics(plan: MonitoringPlan) -> str | None:
    """D58: the opening names the extra questions of the patient's illness group (D50), by the
    group items' short names; never the diagnosis."""
    group = default_groups().by_url(plan.questionnaire_url)
    if group is None:
        return None
    return chat_texts().group_topics.format(topics=", ".join(i.label_cs for i in group.items))


def _lead(request: Request, plan: MonitoringPlan, checkin: Checkin, item: PlanItem) -> str | None:
    """D56: what the chat says before a question so it reads like a conversation, all approved
    text (`chat-v1.json`): thanks for the patient's words or reply, then why this question: a
    symptom from the previous report, or a follow-up of a symptom the patient reported."""
    texts, parts = chat_texts(), []
    last = next((e for e in reversed(checkin.history) if e.event not in APP_EVENTS), None)
    if last is not None and last.event == "text":
        parts.append(texts.ack_text)
    elif last is not None and last.event in ("answer", "confirmed"):
        replies = sum(e.event in ("answer", "confirmed") for e in checkin.history)
        parts.append(texts.ack_answer[(replies - 1) % len(texts.ack_answer)])
    reported = {f.link_id for f in checkin.findings if f.state == "present"}
    symptom = _followed_up(request, item, reported)
    if item.link_id in _history(request, plan, checkin) and item.link_id not in reported:
        parts.append(texts.asked_before)
    elif symptom is not None and (topic := texts.topics.get(symptom)) is not None:
        parts.append(texts.follow_up.format(topic=topic))
    return " ".join(parts) or None


def _followed_up(request: Request, item: PlanItem, reported: set[str]) -> str | None:
    """The reported symptom this item follows up (its `enableWhen`, or a grading attribute)."""
    for condition in item.enable_when:
        if condition.link_id in reported:
            return condition.link_id
    attributes = _pipeline(request).grading_attributes or {}
    return next((s for s in sorted(reported) if item.link_id in attributes.get(s, ())), None)


def _history_intro(request: Request, plan: MonitoringPlan, checkin: Checkin) -> str | None:
    """D56: the opening names what the patient reported last time, so the chat picks it up."""
    before = _history(request, plan, checkin)
    if not before:
        return None
    texts = chat_texts()
    topics = [texts.topics[i.link_id] for i in plan.items if i.link_id in before & texts.topics.keys()]
    if not topics:
        return texts.history_intro_plain
    return texts.history_intro.format(topics=", ".join(topics))


def _reading_lead(
    plan: MonitoringPlan, checkin: Checkin, item: PlanItem, retry: bool
) -> str | None:
    """D55: a symptom asked after its reading is confirmed says the reading is noted first, so the
    patient isn't asked as if unheard ("Teplotu … jsme si zapsali: 37 °C"). A reply that missed the
    question gets a retry naming the options instead of the general one."""
    reading = wizard.noted_reading(plan, checkin, item.link_id)
    if reading is None:
        return None
    texts = chat_texts()
    template = (texts.reading_retry if retry else texts.reading_noted).get(reading.link_id)
    if template is None:
        return None
    value = wizard.answer_text(plan.item(reading.link_id), reading)
    options = ", ".join(option.text_cs for option in item.options)
    return template.format(value=value, options=options)


def _record_prompt(request: Request, checkin: Checkin, prompt: _Prompt) -> Checkin:
    if prompt.kind == "summary":  # the history keeps exactly what the summary listed (D46)
        texts = chat_texts()
        listed = "; ".join(f"{label}: {answer}" for label, answer, _ in prompt.rows)
        parts = [prompt.lead, prompt.text, texts.summary_intro, listed]
        return _shown(request, checkin, "question", " ".join(p for p in parts if p))
    if prompt.kind == "confirm" and prompt.item is not None:
        text = f"{prompt.item.text_cs} Rozuměli jsme: {prompt.answer_text}"
        return _shown(request, checkin, "confirmation", text, link_id=prompt.item.link_id)
    if prompt.item is not None:
        text = f"{prompt.lead} {prompt.item.text_cs}" if prompt.lead else prompt.item.text_cs
        return _shown(request, checkin, "question", text, link_id=prompt.item.link_id)
    return _shown(request, checkin, "question", prompt.text)


def _topics(plan: MonitoringPlan) -> list[str]:
    """Short names of the plan's core items, in Questionnaire order, for the opening bubble."""
    topics = chat_texts().topics
    return [topics[i.link_id] for i in plan.items if i.link_id in topics]


def _chat_log(checkin: Checkin) -> list[ChatEntry]:
    """The conversation so far, without the prompt on screen now (it is the page's heading).
    A confirmation right after its own answer is shown short ("Je to správně? Rozuměli jsme:
    Ne"): the question is in the bubble just above. The stored history keeps the full text."""
    shown = [e for e in checkin.history if e.event not in ("result", "sent")]
    shown = shown[:-1] if shown and shown[-1].event in APP_EVENTS else shown
    texts, log = chat_texts(), []
    for entry in shown:
        previous = log[-1] if log else None
        if (
            entry.event == "confirmation"
            and previous is not None
            and previous.event == "answer"
            and previous.link_id == entry.link_id
        ):
            short = f"{CHAT_CONFIRM} {texts.understood} {previous.text}"
            entry = entry.model_copy(update={"text": short})
        log.append(entry)
    return log


def _chat(
    request: Request,
    account: Account,
    checkin: Checkin,
    plan: MonitoringPlan,
    fix: str = "",
    ask: bool = False,
    status_code: int = 200,
    error: str | None = None,
    text: str = "",
) -> HTMLResponse:
    prompt = _chat_prompt(request, plan, checkin, fix, ask)
    # A confirmation repeats its question only when the chat never asked it (read from words).
    asked_before = prompt.item is not None and any(
        e.event == "question" and e.link_id == prompt.item.link_id for e in checkin.history
    )
    checkin = _record_prompt(request, checkin, prompt)
    item = prompt.item
    return _page(
        request,
        "patient/chat.html",
        status_code,
        account=account,
        base=_base(checkin),
        log=_chat_log(checkin),
        prompt=prompt,
        item=item,
        options=item.options if item is not None and item.type == "choice" else [],
        dont_know=DONT_KNOW,
        unit=wizard.unit_label(item.unit) if item is not None else None,
        hint=CHAT_OPENING_HINT if prompt.kind == "opening" else None,
        topics=_topics(plan) if prompt.kind == "opening" else [],
        group_topics=_group_topics(plan) if prompt.kind == "opening" else None,
        history_intro=_history_intro(request, plan, checkin) if prompt.kind == "opening" else None,
        asked_before=asked_before,
        retry=CHAT_RETRY,
        confirm=CHAT_CONFIRM,
        error=error,
        text=text,
        voice=_voice_on(request),
    )


@router.get("/hlaseni/{checkin_id}", response_model=None)
def checkin_start(
    request: Request, account: Patient, checkin_id: str, opravit: str = "", otazky: str = ""
) -> HTMLResponse | RedirectResponse:
    checkin = _own_checkin(request, account, checkin_id)
    if checkin.status != "in_progress":
        return _redirect(f"{_base(checkin)}/vysledek")
    return _chat(request, account, checkin, _plan_of(request, checkin), opravit, otazky == "1")


def _typed_answer(item: PlanItem, text: str):
    """A typed reply that is exactly an approved option ("ne", "Nevím") or, for a number
    question, just a number ("38,4 °C"): the same as tapping it. Anything else is None."""
    said = text.strip().rstrip(".!").casefold()
    if said in ("nevím", "nevim"):
        with contextlib.suppress(ValueError):
            return wizard.button_answer(item, DONT_KNOW)
    if item.type == "quantity":
        value = wizard.parse_number(_UNITS.sub("", said))
        return wizard.button_answer(item, TYPED_VALUE, value) if value is not None else None
    option = next((o for o in item.options if o.text_cs.casefold() == said), None)
    return wizard.button_answer(item, option.code) if option is not None else None


@router.post("/hlaseni/{checkin_id}/zprava", response_model=None)
def chat_message(
    request: Request,
    account: Patient,
    checkin_id: str,
    text: Annotated[str, Form()] = "",
    voice: Annotated[str, Form()] = "",
    link_id: Annotated[str, Form()] = "",
) -> HTMLResponse | RedirectResponse:
    """A message typed or dictated into the chat, as the reply to the question on screen."""
    checkin = _own_checkin(request, account, checkin_id)
    if checkin.status != "in_progress":
        return _redirect(f"{_base(checkin)}/vysledek")
    plan = _plan_of(request, checkin)
    wanted = text.strip()
    error = None
    if not wanted:
        error = WRITE_SOMETHING_CHAT
    elif len(wanted) > wizard.MAX_TEXT_LENGTH:
        error = TOO_LONG
    asked = _item(plan, checkin, link_id) if link_id else None
    if error is not None:
        return _chat(request, account, checkin, plan, link_id, bool(link_id), 400, error, text)
    via = "voice" if voice == "1" else "keyboard"
    button = _typed_answer(asked, wanted) if asked is not None else None
    if button is not None and asked is not None:
        checkin, _ = _take_answer(request, account, checkin, plan, asked, button, via)
    else:
        checkin = _take_text(request, account, checkin, plan, wanted, voice == "1", asked)
    return _after_change(request, checkin, plan, _base(checkin))


# --- Writing in one's own words -------------------------------------------------------------


def _voice_on(request: Request) -> bool:
    return _pipeline(request).transcribe is not None


@router.get("/hlaseni/{checkin_id}/napsat", response_model=None)
def write_page(
    request: Request, account: Patient, checkin_id: str
) -> HTMLResponse | RedirectResponse:
    checkin = _own_checkin(request, account, checkin_id)
    if checkin.status != "in_progress":
        return _redirect(f"{_base(checkin)}/vysledek")
    return _page(
        request, "patient/write.html",
        account=account, base=_base(checkin), voice=_voice_on(request),
    )  # fmt: skip


@router.post("/hlaseni/{checkin_id}/napsat", response_model=None)
def write_submit(
    request: Request,
    account: Patient,
    checkin_id: str,
    text: Annotated[str, Form()] = "",
    voice: Annotated[str, Form()] = "",
) -> HTMLResponse | RedirectResponse:
    checkin = _own_checkin(request, account, checkin_id)
    if checkin.status != "in_progress":
        return _redirect(f"{_base(checkin)}/vysledek")
    plan = _plan_of(request, checkin)
    wanted = text.strip()
    error = None
    if not wanted:
        error = WRITE_SOMETHING
    elif len(wanted) > wizard.MAX_TEXT_LENGTH:
        error = TOO_LONG
    if error is not None:
        return _page(
            request, "patient/write.html", 400,
            account=account, base=_base(checkin), error=error, text=text,
            voice=_voice_on(request),
        )  # fmt: skip
    checkin = _take_text(request, account, checkin, plan, wanted, voice == "1")
    return _after_change(request, checkin, plan, f"{_base(checkin)}/dalsi")


def _take_text(
    request: Request,
    account: Account,
    checkin: Checkin,
    plan: MonitoringPlan,
    wanted: str,
    voice: bool,
    asked: PlanItem | None = None,
) -> Checkin:
    """Save the patient's own words and let the model read them (`asked`: the chat's question)."""
    message = InboundMessage(
        message_id=secrets.token_hex(8),
        checkin_id=checkin.checkin_id,
        patient_id=checkin.patient_id,
        session_account_id=account.account_id,
        reported_by=checkin.reported_by,
        received_at=_now(request),
        kind="text",
        text=wanted,
    )
    via = "voice" if voice else "keyboard"
    checkin = _save(
        request,
        checkin,
        messages=[*checkin.messages, message],
        history=_step(request, checkin, "text", wanted, message_id=message.message_id, via=via),
    )
    audit.record(
        _store(request), _now(request), account.account_id, "checkin_text",
        target=f"checkin:{checkin.checkin_id}", message=message.message_id,
        via=via,
    )  # fmt: skip
    return _extract(request, account, checkin, plan, message, asked)


@router.post("/hlaseni/{checkin_id}/prepis")
async def transcribe_voice(request: Request, account: Patient, checkin_id: str) -> JSONResponse:
    """Voice (WP17): the page uploads its recording as a 16 kHz mono WAV (CSRF in the header);
    the app transcribes it in this process and returns the text for the patient to check in the
    text box. Nothing is saved here: the audio is dropped, and the text counts only once the
    patient sends it with the write form. Every call is audited by the gateway."""
    checkin = _own_checkin(request, account, checkin_id)
    if checkin.status != "in_progress":
        raise HTTPException(status_code=409)
    transcribe = _pipeline(request).transcribe
    if transcribe is None:
        return JSONResponse({"error": VOICE_OFF}, status_code=503)
    declared = request.headers.get("content-length")
    if declared is None or not declared.isdigit() or int(declared) > transcription.MAX_BYTES:
        return JSONResponse({"error": VOICE_BAD_AUDIO}, status_code=413)
    audio = await request.body()
    try:
        transcription.read_wav(audio)
    except transcription.BadAudio as error:
        audit.record(
            _store(request), _now(request), account.account_id, "voice_rejected",
            target=f"checkin:{checkin.checkin_id}", reason=str(error),
        )  # fmt: skip
        return JSONResponse({"error": VOICE_BAD_AUDIO}, status_code=400)
    try:
        text = await run_in_threadpool(transcribe, audio, f"checkin:{checkin.checkin_id}")
    except Exception:  # noqa: BLE001 — audited by the gateway; the patient can type instead
        return JSONResponse({"error": VOICE_FAILED}, status_code=502)
    text = text.strip()[: wizard.MAX_TEXT_LENGTH]
    if not text:
        return JSONResponse({"error": VOICE_NOTHING_HEARD}, status_code=422)
    return JSONResponse({"text": text})


def _extract(
    request: Request,
    account: Account,
    checkin: Checkin,
    plan: MonitoringPlan,
    message,
    asked: PlanItem | None = None,
) -> Checkin:
    """Merge the model's quoted findings into the check-in (raise-only). If no model read the
    text (none configured, or the extraction failed), the check-in stays on the button path and
    the text is kept unread on the free-text item, so a nurse reads it (`keep_unread`); a failure
    is audited (its type only, never the text)."""
    extract = _pipeline(request).extract
    store, target = _store(request), f"checkin:{checkin.checkin_id}"
    try:
        if extract is None:
            raise LookupError("no model configured")
        names = [account.display_name]
        result = (
            extract(message, plan, names, asked=asked) if asked else extract(message, plan, names)
        )
    except Exception as error:  # noqa: BLE001 — every question is asked with buttons instead
        if extract is not None:
            audit.record(store, _now(request), "system", "extraction_failed", target=target,
                         message=message.message_id, error=type(error).__name__)  # fmt: skip
        return _save(
            request, checkin, findings=extraction.keep_unread(checkin.findings, plan, message)
        )
    checkin = _save(
        request,
        checkin,
        findings=extraction.merge(checkin.findings, result, plan),
        extracted_message_ids=[*checkin.extracted_message_ids, message.message_id],
    )
    audit.record(store, _now(request), "system", "checkin_extracted", target=target,
                 message=message.message_id, extractor=extraction.EXTRACTOR_VERSION,
                 findings=",".join(f.link_id for f in result.findings) or "-",
                 dropped="; ".join(result.uncertainties) or "-")  # fmt: skip
    return checkin


# --- One question per screen ----------------------------------------------------------------


@router.get("/hlaseni/{checkin_id}/dalsi")
def next_question(request: Request, account: Patient, checkin_id: str) -> RedirectResponse:
    checkin = _own_checkin(request, account, checkin_id)
    if checkin.status != "in_progress":
        return _redirect(f"{_base(checkin)}/vysledek")
    item = _next(request, _plan_of(request, checkin), checkin)
    if item is None:
        return _redirect(f"{_base(checkin)}/odeslat")
    finding = _finding(checkin, item.link_id)
    if (
        finding is not None
        and isinstance(finding.source, QuoteSource)
        and finding.state in ("present", "absent")
        and wizard.needs_confirmation(item, finding)
    ):  # understood from the patient's words: show it back for a button confirmation (G5)
        return _redirect(f"{_base(checkin)}/potvrdit/{item.link_id}")
    return _redirect(f"{_base(checkin)}/otazka/{item.link_id}")


def _question(
    request: Request,
    account: Account,
    checkin: Checkin,
    plan: MonitoringPlan,
    item: PlanItem,
    status_code: int = 200,
    error: str | None = None,
    number: str = "",
) -> HTMLResponse:
    position, total, previous = _progress(request, plan, checkin, item)
    base = _base(checkin)
    finding = _finding(checkin, item.link_id)
    if not number and finding is not None and finding.value is not None:
        number = f"{finding.value.value:g}".replace(".", ",")
    return _page(
        request,
        "patient/question.html",
        status_code,
        account=account,
        base=base,
        item=item,
        position=position,
        total=total,
        back=f"{base}/otazka/{previous.link_id}" if previous else base,
        has_follow_ups=any(i.enable_when for i in plan.items if i.type != "text"),
        chosen=finding.code if finding is not None else None,
        unit=wizard.unit_label(item.unit),
        number=number,
        error=error,
        dont_know=DONT_KNOW,
        typed_value=TYPED_VALUE,
    )


@router.get("/hlaseni/{checkin_id}/otazka/{link_id}", response_model=None)
def question_page(
    request: Request, account: Patient, checkin_id: str, link_id: str
) -> HTMLResponse | RedirectResponse:
    checkin = _own_checkin(request, account, checkin_id)
    if checkin.status != "in_progress":
        return _redirect(f"{_base(checkin)}/vysledek")
    plan = _plan_of(request, checkin)
    item = _item(plan, checkin, link_id)
    checkin = _shown(request, checkin, "question", item.text_cs, link_id=item.link_id)
    return _question(request, account, checkin, plan, item)


@router.post("/hlaseni/{checkin_id}/otazka/{link_id}", response_model=None)
def answer_question(
    request: Request,
    account: Patient,
    checkin_id: str,
    link_id: str,
    answer: Annotated[str, Form()] = "",
    number: Annotated[str, Form()] = "",
    chat: Annotated[str, Form()] = "",
) -> HTMLResponse | RedirectResponse:
    checkin = _own_checkin(request, account, checkin_id)
    if checkin.status != "in_progress":
        return _redirect(f"{_base(checkin)}/vysledek")
    plan = _plan_of(request, checkin)
    item = _item(plan, checkin, link_id)

    value = wizard.parse_number(number) if answer == TYPED_VALUE else None
    try:
        button = wizard.button_answer(item, answer, value)
    except ValueError:
        error = TYPE_NUMBER if item.type == "quantity" else CHOOSE_ONE
        return _question(request, account, checkin, plan, item, 400, error, number)

    checkin, finding = _take_answer(request, account, checkin, plan, item, button)
    base = _base(checkin)
    if chat:  # the chat shows the confirmation (G5) or the next question itself
        return _after_change(request, checkin, plan, base)
    then = f"{base}/potvrdit/{item.link_id}" if wizard.needs_confirmation(item, finding) else None
    return _after_change(request, checkin, plan, then or f"{base}/dalsi")


def _take_answer(
    request: Request,
    account: Account,
    checkin: Checkin,
    plan: MonitoringPlan,
    item: PlanItem,
    button,
    via: str | None = None,
) -> tuple[Checkin, Finding]:
    """Save a button answer (or a typed reply that is exactly one of the approved options)."""
    message = InboundMessage(
        message_id=secrets.token_hex(8),
        checkin_id=checkin.checkin_id,
        patient_id=checkin.patient_id,
        session_account_id=account.account_id,
        reported_by=checkin.reported_by,
        received_at=_now(request),
        kind="answer",
        answer=button,
    )
    finding = wizard.finding_from_answer(button, message.message_id)
    said = wizard.answer_text(item, finding) or button.code
    checkin = checkin.evolve(
        messages=[*checkin.messages, message],
        findings=wizard.with_finding(checkin.findings, finding),
        history=_step(
            request, checkin, "answer", said, link_id=item.link_id,
            message_id=message.message_id, via=via,
        ),
    )  # fmt: skip
    upcoming = _next(request, plan, checkin)
    checkin = _save(
        request,
        checkin,
        pending_link_id=upcoming.link_id if upcoming else None,
        clarification_turns=clarification.turns_used(plan, checkin),
    )
    audit.record(
        _store(request), _now(request), account.account_id, "checkin_answer",
        target=f"checkin:{checkin.checkin_id}", link_id=item.link_id,
    )  # fmt: skip
    return checkin, finding


# --- Confirming consequential answers (G5) --------------------------------------------------


@router.get("/hlaseni/{checkin_id}/potvrdit/{link_id}", response_model=None)
def confirm_page(
    request: Request, account: Patient, checkin_id: str, link_id: str
) -> HTMLResponse | RedirectResponse:
    checkin = _own_checkin(request, account, checkin_id)
    if checkin.status != "in_progress":
        return _redirect(f"{_base(checkin)}/vysledek")
    plan = _plan_of(request, checkin)
    item = _item(plan, checkin, link_id)
    finding = _finding(checkin, link_id)
    if finding is None or not wizard.needs_confirmation(item, finding):
        return _redirect(f"{_base(checkin)}/dalsi")
    position, total, _ = _progress(request, plan, checkin, item)
    quote = finding.source.quote if isinstance(finding.source, QuoteSource) else None
    answer_text = wizard.answer_text(item, finding)
    _shown(request, checkin, "confirmation", f"{item.text_cs} Rozuměli jsme: {answer_text}",
           link_id=link_id)  # fmt: skip
    return _page(
        request,
        "patient/confirm.html",
        account=account,
        base=_base(checkin),
        item=item,
        position=position,
        total=total,
        answer_text=answer_text,
        quote=quote,
    )


@router.post("/hlaseni/{checkin_id}/potvrdit/{link_id}", response_model=None)
def confirm_answer(
    request: Request,
    account: Patient,
    checkin_id: str,
    link_id: str,
    chat: Annotated[str, Form()] = "",
) -> RedirectResponse:
    checkin = _own_checkin(request, account, checkin_id)
    if checkin.status != "in_progress":
        return _redirect(f"{_base(checkin)}/vysledek")
    plan = _plan_of(request, checkin)
    _item(plan, checkin, link_id)
    finding = _finding(checkin, link_id)
    if finding is None:
        return _redirect(f"{_base(checkin)}/otazka/{link_id}")
    checkin = checkin.evolve(
        findings=wizard.with_finding(checkin.findings, finding.evolve(confirmed=True)),
        history=_step(request, checkin, "confirmed", "Ano, je to správně", link_id=link_id),
    )
    upcoming = _next(request, plan, checkin)
    checkin = _save(request, checkin, pending_link_id=upcoming.link_id if upcoming else None)
    audit.record(
        _store(request), _now(request), account.account_id, "checkin_answer_confirmed",
        target=f"checkin:{checkin.checkin_id}", link_id=link_id,
    )  # fmt: skip
    then = _base(checkin) if chat else f"{_base(checkin)}/dalsi"
    return _after_change(request, checkin, plan, then)


# --- Sending the report and the result ------------------------------------------------------


@router.get("/hlaseni/{checkin_id}/odeslat", response_model=None)
def summary_page(
    request: Request, account: Patient, checkin_id: str
) -> HTMLResponse | RedirectResponse:
    checkin = _own_checkin(request, account, checkin_id)
    if checkin.status != "in_progress":
        return _redirect(f"{_base(checkin)}/vysledek")
    plan = _plan_of(request, checkin)
    base = _base(checkin)
    answers = []
    for item in wizard.question_items(plan, checkin):
        finding = _finding(checkin, item.link_id)
        if finding is not None and wizard.is_done(item, checkin):
            answers.append(
                (item, wizard.answer_text(item, finding), f"{base}/otazka/{item.link_id}")
            )
    items = _questions(request, plan, checkin)
    # After clarification, items it didn't ask stay open; the patient may answer them one by one.
    open_item = _next(request, plan, checkin) or wizard.next_item(plan, checkin)
    return _page(
        request,
        "patient/summary.html",
        account=account,
        base=base,
        answers=answers,
        written=[m.text for m in checkin.messages if m.kind == "text"],
        open_item=open_item,
        open_href=f"{base}/otazka/{open_item.link_id}" if open_item else None,
        back=f"{base}/otazka/{(open_item or items[-1]).link_id}" if items else base,
    )


@router.post("/hlaseni/{checkin_id}/odeslat")
def send_report(
    request: Request, account: Patient, checkin_id: str, confirm: Annotated[str, Form()] = ""
) -> RedirectResponse:
    """Send the report. `confirm=1` comes from a page that listed every answer for the patient
    to check ("Rozuměl jsem správně?" in the chat, or the summary page): sending from it confirms
    them all at once (D58, G5). Without it an unconfirmed denial still can't close a red flag."""
    checkin = _own_checkin(request, account, checkin_id)
    if checkin.status != "in_progress":
        return _redirect(f"{_base(checkin)}/vysledek")
    plan = _plan_of(request, checkin)
    if confirm == "1":
        checkin = _confirm_all(request, account, checkin, plan)
    checkin = _save(
        request, checkin, status="completed", pending_link_id=None,
        history=_step(request, checkin, "sent", "Odeslat hlášení"),
    )  # fmt: skip
    audit.record(
        _store(request), _now(request), account.account_id, "checkin_completed",
        target=f"checkin:{checkin.checkin_id}",
    )  # fmt: skip
    return _after_change(request, checkin, plan, f"{_base(checkin)}/vysledek")


def _confirm_all(
    request: Request, account: Account, checkin: Checkin, plan: MonitoringPlan
) -> Checkin:
    """Confirm every answer the summary listed (D58) and record the patient's "yes"."""
    listed = wizard.to_confirm(plan, checkin)
    confirmed = checkin.findings
    for finding in listed:
        confirmed = wizard.with_finding(confirmed, finding.evolve(confirmed=True))
    checkin = _save(
        request, checkin, findings=confirmed,
        history=_step(request, checkin, "confirmed", chat_texts().summary_confirmed),
    )  # fmt: skip
    audit.record(
        _store(request), _now(request), account.account_id, "checkin_answers_confirmed",
        target=f"checkin:{checkin.checkin_id}",
        link_ids=",".join(f.link_id for f in listed) or "-",
    )  # fmt: skip
    return checkin


def _latest_urgent(assessments: list[Assessment]) -> Assessment | None:
    return next((a for a in reversed(assessments) if a.status == "urgent"), None)


def _strict_emergency_criteria(assessment: Assessment) -> list[str]:
    """Independent severe signals that unlock the 155 action on the urgent screen.

    The team alert still follows the assessment's `urgent` status immediately; this count is only
    for the patient-facing ambulance action."""

    found: set[str] = set()
    for finding in assessment.findings:
        if finding.state != "present":
            continue
        if finding.link_id == "temperature" and finding.value is not None:
            if finding.value.value >= 39.0 or finding.value.value < 36.0:
                found.add("temperature-danger")
        elif finding.link_id == "unwell":
            found.add("unwell")
        elif finding.link_id == "diarrhoea_blood":
            found.add("diarrhoea-blood")
        elif finding.link_id == "chills_severity" and finding.code == "whole_body":
            found.add("whole-body-chills")
    for rule in assessment.triggered_rules:
        phrase = rule.inputs.get("matched_phrase")
        if (
            rule.rule_id == "red-flag-phrase"
            and isinstance(phrase, str)
            and phrase in _EMERGENCY_PHRASES
        ):
            found.add(f"phrase:{phrase}")
    return sorted(found)


@router.get("/hlaseni/{checkin_id}/vysledek", response_model=None)
def result_page(
    request: Request, account: Patient, checkin_id: str
) -> HTMLResponse | RedirectResponse:
    store = _store(request)
    checkin = _own_checkin(request, account, checkin_id)
    assessments = _assessments(store, checkin)
    assessment = assessments[-1] if assessments else None
    # Once urgent, always urgent: a later assessment never takes the urgent screen away.
    urgent_assessment = _latest_urgent(assessments)
    urgent = urgent_assessment is not None
    if checkin.status == "in_progress" and not urgent:
        return _redirect(f"{_base(checkin)}/dalsi")

    card = None
    team_line = _team_line(request, account) if urgent else None
    emergency_call = bool(urgent_assessment) and (
        len(_strict_emergency_criteria(urgent_assessment)) >= _EMERGENCY_THRESHOLD
    )
    if urgent:  # urgent overrides everything, even a failure after the urgent assessment
        key = (
            "urgent_emergency"
            if team_line and emergency_call
            else "urgent"
            if team_line
            else "urgent_no_line_emergency"
            if emergency_call
            else "urgent_no_line"
        )
    elif checkin.status == "processing_failed":
        key = "processing_failed"
    elif assessment is None:
        key = "not_assessed"
    else:
        key = assessment.status
        if assessment.advice_card_id and assessment.advice_card_version:
            card = _pipeline(request).card(
                assessment.advice_card_id, assessment.advice_card_version
            )
    # Its own alert, or an unresolved alert of the same episode it joined (WP18 grouping).
    notified = any(processing.alert_of(store, a) is not None for a in assessments)
    audit.record(
        store, _now(request), account.account_id, "view_result",
        target=f"checkin:{checkin.checkin_id}",
    )  # fmt: skip
    texts = result_texts()
    # The result, urgent above all, never waits for the history write.
    with contextlib.suppress(Exception):
        _shown(request, checkin, "result", texts.screen(key).heading)
    return _page(
        request,
        "patient/result.html",
        account=account,
        urgent=urgent,
        screen=texts.screen(key),
        texts=texts,
        notified=notified,
        card=card,
        team_line=team_line,
        team_line_display=_display_phone(team_line) if team_line else None,
        team_line_is_demo=bool(team_line) and request.app.state.escalation.approved_by is None,
        show_emergency_call=emergency_call,
        voice=_voice_on(request),
    )


def _team_line(request: Request, account: Account) -> str | None:
    """The patient's care team's phone line (D54: `patient_line` in the escalation file, else the
    approved `care_team_phone` of the chat texts), or None: then the screen offers 155 only.
    Never raises: the urgent screen shows whatever the configuration says."""
    try:
        assignment = _store(request).get_patient_plan(account.patient_id or "")
        line = assignment and request.app.state.escalation.patient_line(assignment.care_team_id)
        return line or chat_texts().care_team_phone
    except Exception:  # noqa: BLE001 - the urgent screen never fails on a lookup
        return None


def _display_phone(number: str) -> str:
    """"+420224431111" -> "+420 224 431 111", readable aloud digit group by digit group."""
    prefix, digits = ("+420 ", number[4:]) if number.startswith("+420") else ("", number)
    return prefix + " ".join(digits[i : i + 3] for i in range(0, len(digits), 3))
