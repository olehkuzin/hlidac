"""Who is logged in, and what they may see (WP30).

- `current_account`: the session's account, or `LoginRequired` (the app redirects to the login).
- `require_role(...)`: a route dependency; any other role gets 403. Every non-public route depends
  on one of these (a test enforces it).
- `ensure_patient_access`: call it before reading or changing anything that belongs to a patient.
  A patient sees only their own records; a nurse only patients whose plan is owned by their team.
  Everything else is 403.

Patient routes take the patient from the session, never from the URL.
"""

from collections.abc import Callable

from fastapi import HTTPException, Request

from hlidac.schemas import Account, Role
from hlidac.store import Store

SESSION_ACCOUNT = "account_id"


class LoginRequired(Exception):
    """No valid login in this session."""


def current_account(request: Request) -> Account:
    account_id = request.session.get(SESSION_ACCOUNT)
    if not isinstance(account_id, str):
        raise LoginRequired
    account = request.app.state.store.get_account(account_id)
    if account is None:  # the account was removed after login
        request.session.clear()
        raise LoginRequired
    return account


current_account.requires_login = True  # type: ignore[attr-defined]  # read by the route test


def require_role(*roles: Role) -> Callable[[Request], Account]:
    def dependency(request: Request) -> Account:
        account = current_account(request)
        if account.role not in roles:
            raise HTTPException(status_code=403)
        return account

    dependency.__name__ = f"require_{'_or_'.join(roles)}"
    dependency.requires_login = True  # type: ignore[attr-defined]
    return dependency


def can_access_patient(account: Account, patient_id: str, store: Store) -> bool:
    if account.role == "patient":
        return account.patient_id == patient_id
    if account.role == "nurse":
        plan = store.get_patient_plan(patient_id)
        return plan is not None and plan.care_team_id == account.team_id
    return False


def ensure_patient_access(account: Account, patient_id: str, store: Store) -> None:
    if not can_access_patient(account, patient_id, store):
        raise HTTPException(status_code=403)
