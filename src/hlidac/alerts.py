"""Alert deadlines and fallback (WP18 part 2, spec §10).

`check_deadlines(store, now, escalation=..., base_url=...)` runs whenever the demo clock moves and
whenever a clinician page loads. An alert still `open` (nobody acknowledged it) whose
`ack_deadline` has passed goes up one fallback level, and the owning team's fallback contacts
(`data/plans/escalation-demo-v1.yaml`) are notified in the app and by email through the outbox.

- Uncertainty never lowers urgency: an escalation only ever adds a notice; nothing is closed,
  lowered or hidden. An acknowledged or resolved alert is never escalated.
- One fallback level in this version (`MAX_FALLBACK_LEVEL`); after it the alert stays at the top
  of the queue, marked overdue and escalated.
- Idempotent: the alert row is saved at level 1 before anyone is notified, so a second check never
  escalates it again, and notification ids come from alert, recipient and kind, so a retry adds
  no second notice. If notifying fails (no fallback contact configured, a contact who isn't a nurse
  of the team, a broken template), the failure is audited (`notification_failed`), shows in
  Unresolved, and the next check retries it until it completes.
- The clock is passed in; nothing here reads the system time.
"""

from datetime import datetime
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import Field, StringConstraints

from hlidac import audit, notifications
from hlidac.content import DATA_DIR, read_yaml
from hlidac.schemas import Account, Alert, Strict
from hlidac.schemas._base import Email, Id, NonEmpty, Version
from hlidac.store import Store

ESCALATION_FILE = DATA_DIR / "plans" / "escalation-demo-v1.yaml"
MAX_FALLBACK_LEVEL = 1
SYSTEM = "system"


class TeamEscalation(Strict):
    fallback_contacts: list[Email] = Field(min_length=1)
    # The team's phone line for patients: the urgent screen sends the patient here first, and to
    # 155 only when life is at risk (D54). None: the urgent screen offers 155 only.
    patient_line: Annotated[str, StringConstraints(pattern=r"^\+?[0-9]{3,15}$")] | None = None


class Escalation(Strict):
    content_id: Id
    version: Version
    approval: NonEmpty  # "DEMO — čeká na schválení FN Motol" until approved
    approved_by: Id | None
    source_ref: NonEmpty
    teams: dict[Id, TeamEscalation]

    def patient_line(self, team_id: str) -> str | None:
        team = self.teams.get(team_id)
        return team.patient_line if team else None

    def fallback_emails(self, team_id: str) -> list[str]:
        team = self.teams.get(team_id)
        return list(team.fallback_contacts) if team else []


def read_escalation(path: Path = ESCALATION_FILE) -> Escalation:
    """The versioned fallback configuration. Raises ValueError if missing or malformed."""
    try:
        return Escalation.model_validate(read_yaml(path))
    except (ValueError, OSError, yaml.YAMLError) as error:
        raise ValueError(f"{path.name}: {error}") from error


def fallback_contacts(store: Store, escalation: Escalation, team_id: str) -> list[Account]:
    """The team's fallback contacts. Raises LookupError for one that isn't a nurse of the team:
    they couldn't open the case, so the notice would go nowhere useful."""
    contacts = []
    for email in escalation.fallback_emails(team_id):
        account = store.find_account_by_email(email)
        if account is None or account.role != "nurse" or account.team_id != team_id:
            raise LookupError("a fallback contact is not a nurse account of the team")
        contacts.append(account)
    return contacts


def is_overdue(alert: Alert, now: datetime) -> bool:
    return alert.status == "open" and now >= alert.ack_deadline


def _notify(
    store: Store, alert: Alert, escalation: Escalation, now: datetime, base_url: str
) -> None:
    """Never raises: a failure is audited and listed in Unresolved until a later check completes
    the notice (the same pattern as `processing._notify`)."""
    target = f"alert:{alert.alert_id}"
    failed_before = notifications.failure_open(store, alert.alert_id)
    try:
        contacts = fallback_contacts(store, escalation, alert.owner_team_id)
        notifications.notify_escalation(store, alert, contacts, now, base_url=base_url)
    except Exception as error:  # noqa: BLE001 — a failure stays visible (audit, Unresolved)
        audit.record(store, now, SYSTEM, notifications.FAILED, target=target,
                     error=type(error).__name__)  # fmt: skip
        return
    if failed_before:
        audit.record(store, now, SYSTEM, notifications.COMPLETED, target=target)


def check_deadlines(
    store: Store, now: datetime, *, escalation: Escalation, base_url: str
) -> list[Alert]:
    """Escalate every open alert past its deadline; returns the alerts escalated by this call."""
    escalated = []
    for alert in store.list_alerts(status="open"):
        if not is_overdue(alert, now):
            continue
        if alert.fallback_level >= MAX_FALLBACK_LEVEL:
            if notifications.failure_open(store, alert.alert_id):
                _notify(store, alert, escalation, now, base_url)  # retry an interrupted notice
            continue
        raised = alert.evolve(fallback_level=alert.fallback_level + 1)
        store.save_alert(raised)
        audit.record(store, now, SYSTEM, "alert_escalated", target=f"alert:{alert.alert_id}",
                     fallback_level=raised.fallback_level,
                     ack_deadline=alert.ack_deadline.isoformat(),
                     escalation=escalation.version)  # fmt: skip
        _notify(store, raised, escalation, now, base_url)
        escalated.append(raised)
    return escalated
