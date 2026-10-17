"""The clinician app (WP13): the nurse queue, the case view and the unresolved list, in Czech.

- Every route needs the nurse role, and every patient's data passes `ensure_patient_access`: a
  nurse sees only patients whose plan belongs to their team.
- CTCAE grades are shown as provisional until a clinician confirms or corrects them. A review is a
  new `GradeReview` row; the engine's grade stays in its assessment (append-only).
- Every action is audited and enqueues the matching FHIR update through the outbox (WP12); the
  page never waits for IRIS.
- Unresolved shows failed outbox rows by kind, template, status and error type only, never an
  email's body or address, nor the raw error text (D29). It reads only the rows' columns
  (`Store.list_outbox_failures`), so a job that no longer validates still shows.
- Notifications (WP16) show their approved text and the delivery state per channel: in app, and
  the email's outbox status only (spec §9).
- The case's audit trail (WP25) lists every recorded step of one case in order, with exactly what
  each model call was sent (after pseudonymisation) and returned, and replays the stored
  assessment from its own snapshots. The rules page shows the versioned content the engine uses.
"""

import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from hlidac import assessment as engine
from hlidac import audit, fhir_mapping, notifications, processing, redflags, wizard
from hlidac.ctcae import CtcaeMapping, read_mapping
from hlidac.groups import default_groups
from hlidac.schemas import (
    APP_EVENTS,
    Account,
    Alert,
    Assessment,
    AuditEntry,
    Checkin,
    CtcaeGrade,
    GradeReview,
    MonitoringPlan,
    OutboxFailure,
    PlanItem,
    QuoteSource,
)
from hlidac.security.access import can_access_patient, ensure_patient_access, require_role
from hlidac.store import Store
from hlidac.ui import inbox, render
from hlidac.ui.clinician import timeline
from hlidac.ui.demo import check_deadlines

_nurse = require_role("nurse")
# Every nurse page first checks the alert deadlines (WP18): an overdue alert escalates at once.
router = APIRouter(prefix="/sestra", dependencies=[Depends(_nurse), Depends(check_deadlines)])
Nurse = Annotated[Account, Depends(_nurse)]

PRIORITY = {"urgent": ("⚠", "Naléhavé"), "routine": ("●", "Ke kontrole")}
ALERT_STATUS = {"open": "Nové", "acknowledged": "Převzato", "resolved": "Vyřešeno"}
ASSESSMENT_STATUS = {
    "urgent": "Naléhavé",
    "routine_review": "Ke kontrole",
    "insufficient_information": "Chybí informace",
    "no_escalation": "Bez upozornění",
}
GRADE_ICON = {"provisional": "◐", "confirmed": "✔", "corrected": "✎"}
GRADE_STATUS = {
    "provisional": "Předběžný, navržený systémem",
    "confirmed": "Potvrzený",
    "corrected": "Opravený",
}
FINDING_STATE = {
    "present": "uvádí",
    "absent": "popírá",
    "unknown": "neví",
    "not_mentioned": "nezmínil(a)",
}
FLASH = "nurse_flash"  # one status message after an action, shown once (WCAG 4.1.3)
NOTE_NEEDED = "Napište, proč stupeň opravujete."
RESOLUTION_NEEDED = "Napište, co jste udělali."
GRADE_NEEDED = "Vyberte stupeň 1 až 5."


def _store(request: Request) -> Store:
    return request.app.state.store


def _now(request: Request) -> datetime:
    return request.app.state.clock.now()


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def _page(
    request: Request, name: str, status_code: int = 200, *, account: Account, **context: object
) -> HTMLResponse:
    """A nurse page: the layout's nav shows the unread notification count."""
    unread = inbox.unread(_store(request), account)
    return render(request, name, status_code, account=account, unread=unread, **context)


def _plan(request: Request, checkin: Checkin) -> MonitoringPlan | None:
    return request.app.state.pipeline.plan(checkin.questionnaire_url, checkin.questionnaire_version)


def _error_type(failure: OutboxFailure) -> str:
    """Only the kind of error: the raw text could quote a recipient or a server's answer."""
    if failure.last_error is None:
        return "—"
    return failure.last_error.split(":", 1)[0].strip()[:60] or "chyba"


def _effective(grade: CtcaeGrade, reviews: list[GradeReview]) -> CtcaeGrade:
    mine = [r.grade for r in reviews if r.grade.grade_id == grade.grade_id]
    return mine[-1] if mine else grade


def _grade_text(grade: CtcaeGrade) -> str:
    if grade.grade is not None:
        return f"stupeň {grade.grade}"
    return f"nejméně stupeň {grade.at_least}, nutné klinické posouzení"


def _patient_label(store: Store, patient_id: str) -> str:
    """Pseudonymous label for the patient (e.g. 'Pacient 01', never a real name; WP14, D43)."""
    accounts = store.list_patient_accounts(patient_id)
    return accounts[0].display_name if accounts else patient_id


# Short Czech names for the demo Questionnaire's items, for display only: the stored assessment
# keeps the link ids (traceability), the nurse reads words.
ITEM_LABEL = {
    "fever": "horečka",
    "temperature": "teplota",
    "chills": "zimnice",
    "chills_severity": "síla zimnice",
    "unwell": "celkově zle",
    "nausea": "nevolnost",
    "nausea_intake": "jídlo a pití",
    "vomiting": "zvracení",
    "vomiting_episodes": "počet zvracení",
    "diarrhoea": "průjem",
    "diarrhoea_stools": "počet stolic",
    "diarrhoea_blood": "krev nebo hlen ve stolici",
    "other": "jiné potíže",
    **default_groups().labels(),  # the illness-group items (D50)
}
GENERAL_GROUP = "Obecný dotazník (bez skupiny)"
_LINK_ID = re.compile(r"\b(" + "|".join(sorted(ITEM_LABEL, key=len, reverse=True)) + r")\b")
_REPORTED = re.compile(r"^Reported: (\w+): (.*)$")
_NUMBER = re.compile(r"^(-?\d+(?:\.\d+)?) (\S+)( \(not yet confirmed\))?$")
BASIS_PREFIX = {
    "At least, not exact: ": "Proč jen „nejméně“ (originál mapování): ",
    "Mapping ": "Mapování ",
}


def readable(text: str) -> str:
    """A rule reason for the nurse: item names in Czech, no empty UKONS grade."""
    return _LINK_ID.sub(lambda m: ITEM_LABEL[m.group(1)], text.replace(" (stupeň –)", ""))


def _basis_line(line: str, items: dict[str, PlanItem]) -> str:
    """One stored basis line in Czech; the CTCAE wording itself stays verbatim (NCI, English)."""
    reported = _REPORTED.match(line)
    if reported is None:
        for prefix, czech in BASIS_PREFIX.items():
            if line.startswith(prefix):
                return czech + line.removeprefix(prefix)
        return line
    link_id, said = reported.groups()
    item = items.get(link_id)
    label = ITEM_LABEL.get(link_id, link_id)
    if said == "not answered":
        return f"Pacient uvedl – {label}: bez odpovědi"
    number = _NUMBER.match(said)
    if number is not None:
        value, unit, unconfirmed = number.groups()
        shown = f"{value.replace('.', ',')} {wizard.unit_label(unit)}"
        if unconfirmed:
            shown += " (číslo pacient zatím nepotvrdil, proto se do stupně nepočítá)"
        return f"Pacient uvedl – {label}: {shown}"
    option = next((o for o in item.options if o.code == said), None) if item else None
    return f"Pacient uvedl – {label}: {option.text_cs if option else said}"


def _illness_group(store: Store, patient_id: str) -> str:
    """The name of the patient's illness group, from their plan's questionnaire (D50)."""
    plan = store.get_patient_plan(patient_id)
    group = default_groups().by_url(plan.questionnaire_url) if plan else None
    return group.name_cs if group else GENERAL_GROUP


def _names(store: Store) -> Callable[[str | None], str]:
    """The display name for an account id (e.g. 'Sestra Demo'), the id if the account is gone."""

    def name(account_id: str | None) -> str:
        if not account_id:
            return ""
        account = store.get_account(account_id)
        return account.display_name if account and account.display_name else account_id

    return name


@dataclass(frozen=True)
class QueueRow:
    alert: Alert
    patient_label: str
    assessment: Assessment | None
    top_grade: str
    reason: str
    grouped: int = 0  # later reports of the same episode that joined this alert (WP18)
    illness_group: str = ""  # the patient's illness group (D50)


def _top_grade(assessment: Assessment | None, reviews: list[GradeReview]) -> str:
    if assessment is None or not assessment.grades:
        return "—"
    grades = [_effective(g, reviews) for g in assessment.grades]
    best = max(grades, key=lambda g: g.grade or g.at_least or 0)
    label = "předběžný" if best.status == "provisional" else GRADE_STATUS[best.status].lower()
    return f"{GRADE_ICON[best.status]} {best.ctcae_term}: {_grade_text(best)} ({label})"


def _team_alerts(request: Request, account: Account, status: str) -> list[QueueRow]:
    store = _store(request)
    rows = []
    for alert in store.list_alerts(status=status):
        if not can_access_patient(account, alert.patient_id, store):
            continue
        assessment = store.get_assessment(alert.assessment_id)
        reviews = store.list_grade_reviews(alert.assessment_id) if assessment else []
        reason = (
            "; ".join(readable(r.reason) for r in assessment.triggered_rules) if assessment else "—"
        )
        label = _patient_label(store, alert.patient_id)
        joined = len(processing.grouped_ids(store, alert))
        rows.append(
            QueueRow(
                alert,
                label,
                assessment,
                _top_grade(assessment, reviews),
                reason or "—",
                joined,
                _illness_group(store, alert.patient_id),
            )
        )
    # Urgent first, then the earliest deadline.
    return sorted(rows, key=lambda r: (r.alert.priority != "urgent", r.alert.ack_deadline))


# --- Queue ----------------------------------------------------------------------------------


@router.get("")
def queue(request: Request, account: Nurse) -> HTMLResponse:
    audit.record(_store(request), _now(request), account.account_id, "view_queue")
    return _page(
        request,
        "nurse/queue.html",
        account=account,
        open_rows=_team_alerts(request, account, "open"),
        acknowledged_rows=_team_alerts(request, account, "acknowledged"),
        now=_now(request),
        priority=PRIORITY,
        alert_status=ALERT_STATUS,
    )


# --- Case view ------------------------------------------------------------------------------


def _own_alert(request: Request, account: Account, alert_id: str) -> Alert:
    alert = _store(request).get_alert(alert_id)
    if alert is None:
        raise HTTPException(status_code=404)
    ensure_patient_access(account, alert.patient_id, _store(request))
    return alert


def _case_parts(request: Request, alert: Alert) -> tuple[Assessment, Checkin]:
    store = _store(request)
    assessment = store.get_assessment(alert.assessment_id)
    checkin = store.get_checkin(assessment.checkin_id) if assessment else None
    if assessment is None or checkin is None:
        raise HTTPException(status_code=404)
    return assessment, checkin


def _case(
    request: Request,
    account: Account,
    alert: Alert,
    status_code: int = 200,
    errors: dict[str, str] | None = None,
    submitted: dict[str, str] | None = None,
) -> HTMLResponse:
    """`errors` by field ("resolve", "<grade_id>:grade", "<grade_id>:note"); `submitted` keeps
    what the nurse entered on a failed form, so nothing is asked again (WCAG 3.3.7)."""
    store = _store(request)
    assessment, checkin = _case_parts(request, alert)
    plan = _plan(request, checkin)
    items = {item.link_id: item for item in plan.items} if plan else {}
    reviews = store.list_grade_reviews(assessment.assessment_id)
    findings = []
    for finding in assessment.findings:
        item = items.get(finding.link_id)
        answer = wizard.answer_text(item, finding) if item else ""
        quote = finding.source.quote if finding.source and finding.source.kind == "quote" else None
        findings.append(
            {
                "question": item.text_cs if item else finding.link_id,
                "state": FINDING_STATE[finding.state],
                "answer": answer,
                "quote": quote,
                "confirmed": finding.confirmed,
                "button": finding.source is not None and finding.source.kind == "answer",
                "unread": finding.source is not None and finding.source.kind == "message",
            }
        )
    grades = []
    for grade in assessment.grades:
        history = [r for r in reviews if r.grade.grade_id == grade.grade_id]
        grades.append(
            {
                "proposed": grade,
                "current": _effective(grade, reviews),
                "history": history,
                "basis": [_basis_line(line, items) for line in grade.basis],
            }
        )
    patient_plan = store.get_patient_plan(alert.patient_id)
    return _page(
        request,
        "nurse/case.html",
        status_code,
        account=account,
        alert=alert,
        patient_label=_patient_label(store, alert.patient_id),
        illness_group=_illness_group(store, alert.patient_id),
        assessment=assessment,
        checkin=checkin,
        findings=findings,
        missing=[items[m].text_cs if m in items else m for m in assessment.missing_link_ids],
        grades=grades,
        timeline=timeline.build(store, alert),
        written=[m for m in checkin.messages if m.kind == "text"],
        accessibility=patient_plan.accessibility if patient_plan else None,
        errors=errors or {},
        submitted=submitted or {},
        flash=request.session.pop(FLASH, None),
        priority=PRIORITY,
        alert_status=ALERT_STATUS,
        assessment_status=ASSESSMENT_STATUS,
        grade_status=GRADE_STATUS,
        grade_icon=GRADE_ICON,
        grade_text=_grade_text,
        readable=readable,
        who=_names(store),
    )


@router.get("/pripad/{alert_id}")
def case_page(request: Request, account: Nurse, alert_id: str) -> HTMLResponse:
    alert = _own_alert(request, account, alert_id)
    audit.record(
        _store(request), _now(request), account.account_id, "view_case",
        target=f"alert:{alert.alert_id}",
    )  # fmt: skip
    return _case(request, account, alert)


def _save_alert(request: Request, alert: Alert, action: str, account: Account) -> None:
    store = _store(request)
    store.save_alert(alert)
    audit.record(store, _now(request), account.account_id, action, target=f"alert:{alert.alert_id}")
    _, checkin = _case_parts(request, alert)
    fhir_mapping.enqueue_alert(store, alert, checkin, _now(request))


@router.post("/pripad/{alert_id}/prevzit")
def acknowledge(request: Request, account: Nurse, alert_id: str) -> RedirectResponse:
    alert = _own_alert(request, account, alert_id)
    if alert.status == "open":
        now = _now(request)
        alert = alert.evolve(
            status="acknowledged", acknowledged_by=account.account_id, acknowledged_at=now
        )
        _save_alert(request, alert, "alert_acknowledged", account)
        request.session[FLASH] = "Upozornění jste převzali."
    return _redirect(f"/sestra/pripad/{alert_id}")


@router.post("/pripad/{alert_id}/vyresit", response_model=None)
def resolve(
    request: Request, account: Nurse, alert_id: str, note: Annotated[str, Form()] = ""
) -> HTMLResponse | RedirectResponse:
    alert = _own_alert(request, account, alert_id)
    if alert.status == "resolved":
        return _redirect(f"/sestra/pripad/{alert_id}")
    if not note.strip():
        return _case(request, account, alert, 400, {"resolve": RESOLUTION_NEEDED}, {"note": note})
    now = _now(request)
    acknowledged = alert.status == "acknowledged"
    alert = alert.evolve(
        status="resolved",
        acknowledged_by=alert.acknowledged_by if acknowledged else account.account_id,
        acknowledged_at=alert.acknowledged_at if acknowledged else now,
        resolved_by=account.account_id,
        resolved_at=now,
        resolution_note=note.strip(),
    )
    _save_alert(request, alert, "alert_resolved", account)
    request.session[FLASH] = "Upozornění je vyřešené."
    return _redirect(f"/sestra/pripad/{alert_id}")


@router.post("/pripad/{alert_id}/stupen/{grade_id}", response_model=None)
def review_grade(
    request: Request,
    account: Nurse,
    alert_id: str,
    grade_id: str,
    grade: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
) -> HTMLResponse | RedirectResponse:
    """Confirm the proposed grade, or correct it (a correction needs a note)."""
    alert = _own_alert(request, account, alert_id)
    assessment, checkin = _case_parts(request, alert)
    proposed = next((g for g in assessment.grades if g.grade_id == grade_id), None)
    if proposed is None:
        raise HTTPException(status_code=404)
    if grade not in {"1", "2", "3", "4", "5"}:
        errors = {f"{grade_id}:grade": GRADE_NEEDED}
        return _case(request, account, alert, 400, errors, {"grade_id": grade_id, "note": note})
    value = int(grade)
    # Confirming means agreeing with the proposal: its exact grade, or the "at least" grade itself.
    status = "confirmed" if value == (proposed.grade or proposed.at_least) else "corrected"
    if status == "corrected" and not note.strip():
        errors = {f"{grade_id}:note": NOTE_NEEDED}
        kept = {"grade_id": grade_id, "grade": grade, "note": note}
        return _case(request, account, alert, 400, errors, kept)
    now = _now(request)
    reviewed = proposed.evolve(
        grade=value,
        at_least=None,
        needs_clinical_assessment=False,
        status=status,
        confirmed_by=account.account_id,
        confirmed_at=now,
    )
    review = GradeReview(
        review_id=secrets.token_hex(16),
        assessment_id=assessment.assessment_id,
        grade=reviewed,
        note=note.strip() or None,
    )
    store = _store(request)
    store.add_grade_review(review)
    audit.record(
        store, now, account.account_id, f"grade_{status}",
        target=f"assessment:{assessment.assessment_id}", grade_id=grade_id, grade=value,
    )  # fmt: skip
    fhir_mapping.enqueue_grade_review(store, checkin, assessment, review, now)
    request.session[FLASH] = f"Stupeň {value} {'potvrzen' if status == 'confirmed' else 'opraven'}."
    return _redirect(f"/sestra/pripad/{alert_id}")


# --- Unresolved -----------------------------------------------------------------------------


@router.get("/nevyresene")
def unresolved(request: Request, account: Nurse) -> HTMLResponse:
    """Everything that must not get lost: failed and partial check-ins, reports nobody has
    assessed, assessments still missing information, and outbox rows that failed."""
    store = _store(request)
    reports = []
    for plan in store.list_patient_plans(account.team_id or ""):
        for checkin in store.list_checkins(plan.patient_id):
            assessments = [
                a for a in store.list_assessments(plan.patient_id)
                if a.checkin_id == checkin.checkin_id
            ]  # fmt: skip
            latest = assessments[-1] if assessments else None
            if checkin.status == "processing_failed":
                why = "Zpracování selhalo"
            elif checkin.status == "in_progress":
                why = "Rozpracované, neodeslané"
            elif latest is None:
                why = "Odeslané, nikdo ho nevyhodnotil"
            elif latest.status == "insufficient_information":
                why = "Chybí informace"
            else:
                continue
            reports.append(
                {
                    "checkin": checkin,
                    "patient_label": _patient_label(store, plan.patient_id),
                    "why": why,
                    "assessment": latest,
                }
            )
    reports.sort(key=lambda r: r["checkin"].updated_at)
    failed = [
        {
            "item": failure,
            "kind": "FHIR zápis" if failure.kind == "fhir_transaction" else "E-mail",
            "template": failure.template_id or "—",
            "error": _error_type(failure),
        }
        for failure in store.list_outbox_failures()
    ]
    unnotified = [
        {"alert": alert, "patient_label": _patient_label(store, alert.patient_id)}
        for plan in store.list_patient_plans(account.team_id or "")
        for alert in store.list_alerts(patient_id=plan.patient_id)
        if notifications.failure_open(store, alert.alert_id)
    ]
    audit.record(store, _now(request), account.account_id, "view_unresolved")
    return _page(
        request, "nurse/unresolved.html",
        account=account, reports=reports, failed=failed, unnotified=unnotified,
    )  # fmt: skip


# --- Notifications (WP16) -------------------------------------------------------------------


@router.get("/oznameni")
def notification_list(request: Request, account: Nurse) -> HTMLResponse:
    store = _store(request)
    audit.record(store, _now(request), account.account_id, "view_notifications")
    entries = inbox.entries(store, account)
    return _page(request, "nurse/notifications.html", account=account, entries=entries)


@router.post("/oznameni/{notification_id}")
def open_notification(request: Request, account: Nurse, notification_id: str) -> RedirectResponse:
    return inbox.open_one(request, account, notification_id, "/sestra")


# --- Audit trail of a case (WP25) -----------------------------------------------------------

ACTION = {
    "checkin_started": "Pacient začal hlášení",
    "checkin_text": "Pacient napsal zprávu",
    "checkin_answer": "Pacient odpověděl",
    "checkin_answer_confirmed": "Pacient potvrdil odpověď",
    "checkin_completed": "Pacient hlášení odeslal",
    "model_call": "Volání modelu (extrakce z textu)",
    "model_call:transcription": "Volání modelu (přepis hlasu)",
    "model_call_refused": "Brána volání modelu odmítla",
    "checkin_extracted": "Zjištění z textu převzata (s citací)",
    "extraction_failed": "Extrakce selhala, otázky jdou tlačítky",
    "context_read_failed": "Údaje o léčbě se nepodařilo načíst",
    "assessment_created": "Pravidla vyhodnotila hlášení",
    "checkin_processing_failed": "Zpracování hlášení selhalo",
    "fhir_enqueue_failed": "Zápis do FHIR se nepodařilo zařadit",
    "view_result": "Pacient viděl výsledek",
    "alert_created": "Vzniklo upozornění",
    "alert_grouped": "Přičleněno k otevřenému upozornění",
    "alert_escalated": "Upozornění eskalováno",
    "notification_created": "Oznámení vytvořeno",
    "notification_read": "Oznámení přečteno",
    notifications.FAILED: "Oznámení selhalo",
    notifications.COMPLETED: "Oznámení doručeno po opakování",
    "view_case": "Případ otevřen",
    "view_case_audit": "Záznam případu otevřen",
    "alert_acknowledged": "Upozornění převzato",
    "alert_resolved": "Upozornění vyřešeno",
    "grade_confirmed": "Stupeň CTCAE potvrzen",
    "grade_corrected": "Stupeň CTCAE opraven",
}
PURPOSE = {"extraction": "extrakce z textu", "transcription": "přepis hlasu"}
# The conversation (D46): who took each step and what it was.
CHAT_EVENT = {
    "question": "Otázka",
    "confirmation": "Kontrola odpovědi",
    "result": "Výsledná obrazovka",
    "text": "Zpráva vlastními slovy",
    "answer": "Odpověď",
    "confirmed": "Potvrzení",
    "sent": "Odeslání hlášení",
}
REPLAY_SAME = "Shoduje se: stejné vstupy a verze dávají stejný výsledek."


@cache
def _content() -> tuple[engine.Ruleset, CtcaeMapping]:
    """The ruleset and mapping files the app runs on (read once; they change only by a release)."""
    return engine.read_rules(), read_mapping()


def replay_verdict(
    assessment: Assessment,
    plan: MonitoringPlan | None,
    rules: engine.Ruleset,
    mapping: CtcaeMapping,
    checkin: Checkin | None = None,
) -> tuple[bool, str]:
    """Re-derive a stored assessment from its own snapshots (principle 7). Returns (same, text);
    anything short of an exact match, including versions we no longer have, is never "same"."""
    if plan is None:
        return False, "Nelze přepočítat: dotazník této verze není načtený."
    try:
        again = engine.replay(assessment, plan, mapping, rules)
        texts = [
            m.text for m in (checkin.messages if checkin else ()) if m.kind == "text" and m.text
        ]
        texts.extend(
            f.source.quote for f in assessment.findings if isinstance(f.source, QuoteSource)
        )
        again = redflags.raise_on_hit(again, "\n".join(texts))
    except ValueError:
        return False, (
            "Nelze přepočítat: hlášení bylo vyhodnoceno jinými verzemi, než jaké běží teď "
            f"(pravidla v{rules.version}, mapování v{mapping.version}, "
            f"engine v{engine.ENGINE_VERSION})."
        )
    except Exception:  # noqa: BLE001 — a failed replay is shown, never hidden
        return False, "Přepočet selhal."
    if again == assessment:
        return True, REPLAY_SAME
    return False, "Liší se: přepočet dal jiný výsledek, než je uložený."


def _actor(store: Store, actor: str) -> str:
    if actor == "system":
        return "systém"
    account = store.get_account(actor)
    if account is None:
        return actor
    return f"{'sestra' if account.role == 'nurse' else 'pacient'} {account.account_id}"


def _action_label(entry: AuditEntry) -> str:
    if entry.model_call is not None and entry.model_call.purpose == "transcription":
        return ACTION["model_call:transcription"]
    return ACTION.get(entry.action, entry.action)


def _case_trail(store: Store, alert: Alert, checkin: Checkin) -> list[AuditEntry]:
    """Every audit entry about this check-in: its messages (model calls), assessments, alerts
    and their notifications. Read target by target, never the whole log."""
    assessments = [
        a for a in store.list_assessments(checkin.patient_id) if a.checkin_id == checkin.checkin_id
    ]
    assessment_ids = {a.assessment_id for a in assessments}
    alert_ids = {alert.alert_id} | {
        a.alert_id
        for a in store.list_alerts(patient_id=checkin.patient_id)
        if a.assessment_id in assessment_ids
    }
    targets = {f"checkin:{checkin.checkin_id}"}
    targets |= {f"message:{m.message_id}" for m in checkin.messages}
    targets |= {f"assessment:{a}" for a in assessment_ids}
    targets |= {f"alert:{a}" for a in alert_ids}
    recipients = store.list_team_accounts(alert.owner_team_id)
    recipients += store.list_patient_accounts(checkin.patient_id)
    for account in recipients:
        for note in store.list_notifications(account.account_id):
            if any(
                note.notification_id
                == notifications.notification_id(a, account.account_id, note.kind)
                for a in alert_ids
            ):
                targets.add(f"notification:{note.notification_id}")
    entries = {e.audit_id: e for target in sorted(targets) for e in store.list_audit(target)}
    return sorted(entries.values(), key=lambda e: e.at)


@router.get("/pripad/{alert_id}/zaznam")
def case_audit(request: Request, account: Nurse, alert_id: str) -> HTMLResponse:
    """The case's audit trail: who did what and when, what each model saw and returned, and
    whether the stored assessment replays to the same result."""
    store = _store(request)
    alert = _own_alert(request, account, alert_id)
    assessment, checkin = _case_parts(request, alert)
    audit.record(
        store, _now(request), account.account_id, "view_case_audit", target=f"alert:{alert_id}"
    )
    rules, mapping = _content()
    same, verdict = replay_verdict(assessment, _plan(request, checkin), rules, mapping, checkin)
    rows, calls = [], 0
    for e in _case_trail(store, alert, checkin):
        calls += e.model_call is not None
        rows.append(
            {
                "entry": e,
                "actor": _actor(store, e.actor),
                "action": _action_label(e),
                "details": ", ".join(f"{k} = {v}" for k, v in e.details.items() if v is not None),
                "call_no": calls if e.model_call is not None else None,
            }
        )
    return _page(
        request,
        "nurse/case_audit.html",
        account=account,
        alert=alert,
        patient_label=_patient_label(store, alert.patient_id),
        assessment=assessment,
        rows=rows,
        calls=[r for r in rows if r["entry"].model_call is not None],
        replay_same=same,
        replay_text=verdict,
        assessment_status=ASSESSMENT_STATUS,
        history=checkin.history,
        app_events=APP_EVENTS,
        chat_event=CHAT_EVENT,
        purpose=PURPOSE,
        on_iris=request.app.state.settings.store_backend == "iris",
    )


# --- Plans and rules (WP25) -----------------------------------------------------------------

OUTCOME = ASSESSMENT_STATUS


@router.get("/pravidla")
def rules_page(request: Request, account: Nurse) -> HTMLResponse:
    """The versioned content the engine runs on: plans, rules and the CTCAE mapping. Read-only;
    content changes only through reviewed files in git."""
    audit.record(_store(request), _now(request), account.account_id, "view_rules")
    rules, mapping = _content()
    plans = sorted(
        request.app.state.pipeline.plans.values(),
        key=lambda p: (p.questionnaire_url, p.questionnaire_version),
    )
    return _page(
        request,
        "nurse/rules.html",
        account=account,
        plans=plans,
        rules=rules,
        mapping=mapping,
        outcome=OUTCOME,
    )


# --- Illness groups (D50) -------------------------------------------------------------------


@router.get("/skupiny")
def groups_page(request: Request, account: Nurse) -> HTMLResponse:
    """The team's patients by illness group, each group with the questions its patients get.
    Read-only: groups and questions change only through reviewed files in git."""
    store = _store(request)
    audit.record(store, _now(request), account.account_id, "view_groups")
    plans = request.app.state.pipeline.plans
    groups = default_groups()
    patients: dict[str, list[dict[str, str]]] = {}
    if account.team_id is not None:
        for plan in store.list_patient_plans(account.team_id):
            accounts = store.list_patient_accounts(plan.patient_id)
            if not accounts:  # a plan left from an earlier demo run: nobody can log in
                continue
            patients.setdefault(plan.questionnaire_url, []).append(
                {
                    "label": accounts[0].display_name,
                    "email": ", ".join(a.email for a in accounts),
                    "patient_id": plan.patient_id,
                }
            )
    sections = []
    for group in groups.groups:
        sections.append(
            {
                "group_id": group.group_id,
                "name": group.name_cs,
                "icd10": ", ".join(group.icd10),
                "patients": sorted(
                    patients.get(group.questionnaire_url, []), key=lambda p: p["label"]
                ),
                "questions": [{"text_cs": i.text, "red_flag": i.red_flag} for i in group.items],
            }
        )
    fallback = groups.fallback_questionnaire
    sections.append(
        {
            "group_id": "general",
            "name": GENERAL_GROUP,
            "icd10": "",
            "patients": sorted(patients.get(fallback.url, []), key=lambda p: p["label"]),
            "questions": [],
        }
    )
    core = plans.get((fallback.url, fallback.version))
    return _page(
        request,
        "nurse/groups.html",
        account=account,
        sections=sections,
        core_questions=[i for i in core.items if not i.enable_when] if core else [],
        groups=groups,
    )
