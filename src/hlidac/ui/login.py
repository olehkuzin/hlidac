"""Passwordless login with a one-time email code (D18), logout, and the role home pages.

- Every answer, to "send me a code" and to a wrong, expired, used or locked code, is the same
  whether or not the email has an account, so the forms can't tell who is a patient.
- The pending login is kept in the signed session as our account id; the email and the code never
  appear in a URL or an application log.
- A successful login clears the session (new CSRF token) before storing the account: rotation.
"""

import re
import secrets
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from hlidac import audit, emails
from hlidac.outbox_runner import run_once
from hlidac.schemas import Account, OutboxItem
from hlidac.security.access import SESSION_ACCOUNT, current_account
from hlidac.security.codes import RateLimited, issue_code, verify_code
from hlidac.ui import render

PENDING_LOGIN = "pending_login"
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# One message for every failed code: a different answer for "locked" or "expired" would tell a
# registered email from an unknown one. The exact reason goes to the audit only.
CODE_ERROR = (
    "Tento kód nefunguje. Je možná špatně opsaný, už vypršel nebo byl použitý. "
    "Zkontrolujte ho, nebo si nechte poslat nový kód."
)

# TEMPORARY demo skip only (localhost, DEMO_SKIP_LOGIN): the email has no account here.
SKIP_UNKNOWN = "Pro tento e-mail v této ukázce není žádný účet. Zadejte jiný e-mail."

public = APIRouter()
signed_in = APIRouter(dependencies=[Depends(current_account)])


def _home(account: Account) -> str:
    return "/pacient" if account.role == "patient" else "/sestra"


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


@public.get("/prihlaseni")
def login_page(request: Request) -> HTMLResponse:
    return render(request, "login.html")


@public.post("/prihlaseni", response_model=None)
def send_code(
    request: Request, background: BackgroundTasks, email: Annotated[str, Form()] = ""
) -> HTMLResponse | RedirectResponse:
    state = request.app.state
    wanted = email.strip().lower()
    if len(wanted) > 254 or not _EMAIL.fullmatch(wanted):
        error = "Zadejte e-mail ve tvaru jmeno@domena.cz."
        return render(request, "login.html", 400, error=error, email=email)

    account = state.store.find_account_by_email(wanted)
    request.session[PENDING_LOGIN] = account.account_id if account else ""
    if account is not None:
        _send_code(request, background, account)
    return _redirect("/prihlaseni/kod")


@public.post("/prihlaseni/novy-kod")
def resend_code(request: Request, background: BackgroundTasks) -> RedirectResponse:
    """A new code for the email already given, so it isn't typed again (WCAG 3.3.7)."""
    pending = request.session.get(PENDING_LOGIN)
    if not isinstance(pending, str):
        return _redirect("/prihlaseni")
    account = request.app.state.store.get_account(pending) if pending else None
    if account is not None:
        _send_code(request, background, account)
    return _redirect("/prihlaseni/kod")


def _send_code(request: Request, background: BackgroundTasks, account: Account) -> None:
    """Issue and email a code, unless the account hit the rate limit (then silently nothing, so
    the answer looks the same for every email)."""
    state = request.app.state
    now = state.clock.now()
    try:
        code = issue_code(state.store, account.account_id, state.security_clock.now(), state.secret)
    except RateLimited:
        audit.record(state.store, now, account.account_id, "login_code_rate_limited")
        return
    outbox_id = secrets.token_hex(16)
    state.store.enqueue(
        OutboxItem(
            outbox_id=outbox_id,
            idempotency_key=f"login-code:{outbox_id}",
            created_at=now,
            job=emails.render("login-code", account.email, code=code),
        )
    )
    audit.record(state.store, now, account.account_id, "login_code_sent")
    if state.settings.outbox_runner == "app":
        background.add_task(run_once, state.store, now, state.send_email)


# Loopback addresses ("testclient" is Starlette's test client, never a real peer).
_LOCAL_CLIENTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


def _skip_allowed(request: Request) -> bool:
    """TEMPORARY demo switch (`DEMO_SKIP_LOGIN`): off by default, and only from this machine."""
    client = request.client.host if request.client else None
    return request.app.state.settings.demo_skip_login and client in _LOCAL_CLIENTS


@public.get("/prihlaseni/kod", response_model=None)
def code_page(request: Request) -> HTMLResponse | RedirectResponse:
    if not isinstance(request.session.get(PENDING_LOGIN), str):
        return _redirect("/prihlaseni")
    return render(request, "login_code.html", skip_login=_skip_allowed(request))


@public.post("/prihlaseni/preskocit", response_model=None)
def skip_code(request: Request) -> HTMLResponse | RedirectResponse:
    """TEMPORARY, demo only: log in the account of the email just entered, without the code.
    404 unless `DEMO_SKIP_LOGIN` is on and the request comes from localhost. An unknown email is
    said plainly (demo laptop only, so no one else can use it to probe for accounts)."""
    if not _skip_allowed(request):
        raise HTTPException(status_code=404)
    state = request.app.state
    pending = request.session.get(PENDING_LOGIN)
    if not isinstance(pending, str):
        return _redirect("/prihlaseni")
    account = state.store.get_account(pending) if pending else None
    now = state.clock.now()
    if account is None:
        audit.record(state.store, now, "anonymous", "login_failed", reason="demo_skip_unknown")
        return render(request, "login_code.html", 400, error=SKIP_UNKNOWN, skip_login=True)
    request.session.clear()
    request.session[SESSION_ACCOUNT] = account.account_id
    audit.record(state.store, now, account.account_id, "login", method="demo_skip")
    return _redirect(_home(account))


@public.post("/prihlaseni/kod", response_model=None)
def check_code(
    request: Request, code: Annotated[str, Form()] = ""
) -> HTMLResponse | RedirectResponse:
    state = request.app.state
    pending = request.session.get(PENDING_LOGIN)
    if not isinstance(pending, str):
        return _redirect("/prihlaseni")

    now = state.clock.now()
    result = "invalid"
    if pending:
        result = verify_code(state.store, pending, code, state.security_clock.now(), state.secret)
    account = state.store.get_account(pending) if result == "ok" else None
    if account is None:
        audit.record(state.store, now, pending or "anonymous", "login_failed", reason=result)
        return render(
            request, "login_code.html", 400, error=CODE_ERROR, skip_login=_skip_allowed(request)
        )

    request.session.clear()
    request.session[SESSION_ACCOUNT] = account.account_id
    audit.record(state.store, now, account.account_id, "login")
    return _redirect(_home(account))


@signed_in.get("/")
def home(account: Annotated[Account, Depends(current_account)]) -> RedirectResponse:
    return _redirect(_home(account))


@signed_in.post("/odhlaseni")
def logout(
    request: Request, account: Annotated[Account, Depends(current_account)]
) -> RedirectResponse:
    request.session.clear()
    audit.record(
        request.app.state.store, request.app.state.clock.now(), account.account_id, "logout"
    )
    return _redirect("/prihlaseni")
