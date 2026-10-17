"""The demo panel (D28, system-design §10.4): move the demo clock and invite a patient to a
check-in. Nurses only, and only when `DEMO_PANEL` is on (404 otherwise); never in a hospital
deployment.

- The clock moves forward only, by one of the fixed steps (`STEPS`); after every move the alert
  deadlines are checked at once (`alerts.check_deadlines`), so an unacknowledged alert escalates
  to the fallback contact live.
- An invitation goes to the nurse's own team's patients only (`ensure_patient_access`), in the app
  and by email (`notifications.invite_to_checkin`), from approved templates.
- Every action is audited. The patient is chosen in the form body, never in the URL.
"""

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from hlidac import alerts, audit, notifications
from hlidac.schemas import Account
from hlidac.security.access import ensure_patient_access, require_role
from hlidac.ui import inbox, render


def _enabled(request: Request) -> None:
    if not request.app.state.settings.demo_panel:
        raise HTTPException(status_code=404)


_nurse = require_role("nurse")
router = APIRouter(prefix="/demo", dependencies=[Depends(_nurse), Depends(_enabled)])
Nurse = Annotated[Account, Depends(_nurse)]

STEPS = {
    "15m": (timedelta(minutes=15), "o 15 minut"),
    "1h": (timedelta(hours=1), "o 1 hodinu"),
    "1d": (timedelta(days=1), "o 1 den"),
}
FLASH = "demo_flash"


def _redirect() -> RedirectResponse:
    return RedirectResponse("/demo", status_code=303)


def check_deadlines(request: Request) -> list:
    """Run the deadline check now (also a dependency of every clinician page)."""
    state = request.app.state
    return alerts.check_deadlines(
        state.store,
        state.clock.now(),
        escalation=state.escalation,
        base_url=state.settings.app_base_url,
    )


def _team_patients(request: Request, account: Account) -> list[Account]:
    store = request.app.state.store
    return [
        patient
        for plan in store.list_patient_plans(account.team_id or "")
        for patient in store.list_patient_accounts(plan.patient_id)
    ]


@router.get("")
def panel(request: Request, account: Nurse) -> HTMLResponse:
    state = request.app.state
    now = state.clock.now()
    overdue = [a for a in state.store.list_alerts(status="open") if alerts.is_overdue(a, now)]
    return render(
        request,
        "nurse/demo.html",
        account=account,
        unread=inbox.unread(state.store, account),
        now=inbox.czech_time(now),
        steps=STEPS,
        patients=_team_patients(request, account),
        overdue=len(overdue),
        mailbox=state.settings.demo_mailbox_url,
        on_iris=state.settings.store_backend == "iris",
        flash=request.session.pop(FLASH, None),
    )


@router.post("/cas")
def advance(request: Request, account: Nurse, step: Annotated[str, Form()] = "") -> RedirectResponse:
    if step not in STEPS:
        raise HTTPException(status_code=400)
    state = request.app.state
    delta, words = STEPS[step]
    moved = state.clock.advance(delta)
    audit.record(state.store, moved, account.account_id, "demo_clock_advanced",
                 target="clock", step=step, now=moved.isoformat())  # fmt: skip
    escalated = check_deadlines(request)
    request.session[FLASH] = (
        f"Čas posunut {words}, nyní {inbox.czech_time(moved)}. "
        f"Upozornění předaná záložnímu kontaktu: {len(escalated)}."
    )
    return _redirect()


@router.post("/pozvat")
def invite(
    request: Request, account: Nurse, patient_id: Annotated[str, Form()] = ""
) -> RedirectResponse:
    state = request.app.state
    ensure_patient_access(account, patient_id, state.store)
    patients = state.store.list_patient_accounts(patient_id)
    if not patients:
        raise HTTPException(status_code=404)
    now = state.clock.now()
    for patient in patients:
        note = notifications.invite_to_checkin(
            state.store, patient, now, base_url=state.settings.app_base_url
        )
        audit.record(state.store, now, account.account_id, "demo_checkin_invited",
                     target=f"patient:{patient_id}",
                     notification=note.notification_id)  # fmt: skip
    request.session[FLASH] = "Pozvánka k hlášení odeslána v aplikaci i e-mailem."
    return _redirect()
