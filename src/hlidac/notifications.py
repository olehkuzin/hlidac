"""Notifications (WP16): in the app, mirrored to email for urgent alerts (D37).

Every text comes from approved, versioned content: the in-app texts from
`data/content/notifications/notifications-v<N>.json`, the email from `data/content/email/new-alert.json`.
None of it carries clinical content or patient identifiers; the in-app link opens the case for a
signed-in nurse, and the email links only to the nurse queue.

`notify_alert(store, alert, now, base_url=...)` per alert:
- every nurse of `alert.owner_team_id` (except its fallback contacts, `skip_emails`) gets an in-app
  notification (`urgent_alert` or `report_received`); for an urgent alert also one email, enqueued
  in the outbox;
- the patient's account gets `team_notified` in the app (no patient email for this).

`notify_escalation` tells a team's fallback contacts, in the app and by email
(`alert-escalated`), that an alert passed its acknowledgement deadline (WP18, `alerts.py`).
`invite_to_checkin` asks a patient for a check-in, in the app and by email (`checkin-due`); the
demo panel sends it (D28).

Idempotent: a notification id is a hash of alert, recipient and kind, and an email's outbox key
comes from that id, so a second call adds nothing and never resets `read_at`. The email is
enqueued before its notification is saved, so a stored notification always has its email.
"""

import hashlib
import re
from collections.abc import Collection, Sequence
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Literal

from hlidac import audit, emails
from hlidac.schemas import (
    Account,
    Alert,
    Delivery,
    Notification,
    NotificationKind,
    OutboxItem,
    Strict,
)
from hlidac.schemas._base import Id, NonEmpty, Version
from hlidac.store import Store

CONTENT_DIR = Path(__file__).resolve().parents[2] / "data" / "content" / "notifications"
CURRENT_VERSION = "2"
EMAIL_TEMPLATE = "new-alert"
ESCALATION_EMAIL = "alert-escalated"
INVITATION_EMAIL = "checkin-due"
INVITATION = "invitation:"  # the `about` prefix of a check-in invitation; any other is an alert id
SYSTEM = "system"

_FILE_NAME = re.compile(r"^notifications-v(\d+)\.json$")
_KIND_FOR_PRIORITY: dict[str, NotificationKind] = {
    "urgent": "urgent_alert",
    "routine": "report_received",
}


class NotificationTemplate(Strict):
    template_id: Id
    kind: NotificationKind
    audience: Literal["nurse", "patient"]
    text: NonEmpty


class NotificationContent(Strict):
    content_id: Literal["notifications"]
    version: Version
    approval: NonEmpty  # "DEMO — čeká na schválení FN Motol" until approved
    source_ref: NonEmpty
    templates: list[NotificationTemplate]


@cache
def read_content(version: str = CURRENT_VERSION) -> NotificationContent:
    """The approved in-app texts of one version (raises if the file or a field is wrong)."""
    path = CONTENT_DIR / f"notifications-v{version}.json"
    if not path.is_file():
        raise LookupError(f"no notification content version {version!r}")
    content = NotificationContent.model_validate_json(path.read_text(encoding="utf-8"))
    if content.version != version:
        raise ValueError(f"{path.name} holds version {content.version}")
    if len({t.template_id for t in content.templates}) != len(content.templates):
        raise ValueError(f"{path.name} repeats a template id")
    if len({t.kind for t in content.templates}) != len(content.templates):
        raise ValueError(f"{path.name} has two templates for one kind")
    return content


def content_versions() -> list[str]:
    return sorted(m[1] for p in CONTENT_DIR.iterdir() if (m := _FILE_NAME.fullmatch(p.name)))


def _template(kind: NotificationKind) -> NotificationTemplate:
    found = [t for t in read_content().templates if t.kind == kind]
    if not found:
        raise LookupError(f"no approved notification text for {kind}")
    return found[0]


def text_for(notification: Notification) -> str:
    """The approved in-app text of the notification's template and version."""
    content = read_content(notification.template_version)
    for template in content.templates:
        if template.template_id == notification.template_id:
            if template.kind != notification.kind:
                raise ValueError(f"template {template.template_id} is not for {notification.kind}")
            return template.text
    raise LookupError(f"no notification template {notification.template_id!r}")


def queue_link(base_url: str) -> str:
    """The only link an email carries; raises unless the base URL makes an allowed link."""
    link = base_url.rstrip("/") + emails.NURSE_QUEUE_PATH
    emails.render(EMAIL_TEMPLATE, "check@demo.local", link=link)
    return link


def _find(store: Store, account_id: str, notification_id: str) -> Notification | None:
    return next(
        (n for n in store.list_notifications(account_id) if n.notification_id == notification_id),
        None,
    )


def mark_read(store: Store, notification: Notification, now: datetime) -> Notification:
    """Read once: a later call keeps the first `read_at`."""
    current = _find(store, notification.recipient_account_id, notification.notification_id)
    current = current or notification
    if current.read_at is not None:
        return current
    read = current.evolve(read_at=now)
    store.save_notification(read)
    audit.record(store, now, read.recipient_account_id, "notification_read",
                 target=f"notification:{read.notification_id}")  # fmt: skip
    return read


def unread_count(store: Store, account_id: str) -> int:
    return sum(n.read_at is None for n in store.list_notifications(account_id))


FAILED = "notification_failed"
COMPLETED = "notification_completed"


def failure_open(store: Store, alert_id: str) -> bool:
    """True while the alert's last notification attempt failed (no later completion); the
    nurse's Unresolved list shows it until a retry completes it."""
    outcomes = [
        e.action for e in store.list_audit(f"alert:{alert_id}") if e.action in (FAILED, COMPLETED)
    ]
    return bool(outcomes) and outcomes[-1] == FAILED


def notification_id(alert_id: str, account_id: str, kind: NotificationKind) -> str:
    digest = hashlib.sha256(f"{alert_id}|{account_id}|{kind}".encode()).hexdigest()
    return f"nt-{digest[:40]}"


def patient_link(base_url: str) -> str:
    """The only link a patient's email carries: the app home. Raises unless allowed."""
    link = base_url.rstrip("/") + emails.PATIENT_HOME_PATH
    emails.render(INVITATION_EMAIL, "check@demo.local", patient_link=link)
    return link


def _email(
    store: Store, account: Account, note_id: str, email: tuple[str, dict[str, str]], now: datetime
) -> str:
    """Enqueue the account's email once; returns its outbox id (the same on a retry)."""
    key = f"notification:{note_id}:email"
    outbox_id = f"email-{hashlib.sha256(key.encode()).hexdigest()[:32]}"
    template, values = email
    job = emails.render(template, account.email, **values)
    store.enqueue(OutboxItem(outbox_id=outbox_id, idempotency_key=key, created_at=now, job=job))
    return outbox_id


def _notify(
    store: Store,
    about: str,
    account: Account,
    kind: NotificationKind,
    link: str,
    now: datetime,
    email: tuple[str, dict[str, str]] | None,
) -> Notification:
    """`about` is what the notification is for (an alert id, or an invitation's key); with
    account and kind it makes the id, so a retry adds nothing. `email` is (template, values)."""
    note_id = notification_id(about, account.account_id, kind)
    deliveries = [Delivery(channel="in_app", at=now)]
    outbox_id = None
    if email is not None:
        outbox_id = _email(store, account, note_id, email, now)
        deliveries.append(Delivery(channel="email", at=now, outbox_id=outbox_id))
    found = _find(store, account.account_id, note_id)
    if found is not None:
        return found  # a retry: stored already, read_at untouched
    template = _template(kind)
    notification = Notification(
            notification_id=note_id,
            recipient_account_id=account.account_id,
            kind=kind,
            template_id=template.template_id,
            template_version=read_content().version,
            link=link,
        created_at=now,
        deliveries=deliveries,
    )
    store.save_notification(notification)
    audit.record(store, now, SYSTEM, "notification_created", target=f"notification:{note_id}",
                 kind=kind, recipient=account.account_id, email_outbox=outbox_id,
                 **{"invitation" if about.startswith(INVITATION) else "alert": about})  # fmt: skip
    return notification


def notify_alert(
    store: Store,
    alert: Alert,
    now: datetime,
    *,
    base_url: str,
    skip_emails: Collection[str] = (),
) -> None:
    """Notify the owning team's nurses (all but `skip_emails`, its fallback contacts) and the
    patient. Raises LookupError, after notifying everyone it can, when no nurse is left to notify
    (the alert still shows in the queue)."""
    kind = _KIND_FOR_PRIORITY[alert.priority]
    email = (EMAIL_TEMPLATE, {"link": queue_link(base_url)}) if alert.priority == "urgent" else None
    nurses = [n for n in store.list_team_accounts(alert.owner_team_id) if n.email not in skip_emails]
    for nurse in nurses:
        _notify(store, alert.alert_id, nurse, kind, f"/sestra/pripad/{alert.alert_id}", now, email)
    for patient in store.list_patient_accounts(alert.patient_id):
        _notify(store, alert.alert_id, patient, "team_notified", "/pacient", now, None)
    if not nurses:
        raise LookupError("the owning team has no nurse account to notify")


def notify_escalation(
    store: Store, alert: Alert, contacts: Sequence[Account], now: datetime, *, base_url: str
) -> None:
    """Tell the fallback contacts, in the app and by email, that `alert` is overdue. Raises
    LookupError when there is nobody to tell (the caller keeps that visible)."""
    if not contacts:
        raise LookupError("the owning team has no fallback contact")
    email = (ESCALATION_EMAIL, {"link": queue_link(base_url)})
    link = f"/sestra/pripad/{alert.alert_id}"
    for contact in contacts:
        _notify(store, alert.alert_id, contact, "alert_escalated", link, now, email)


def invite_to_checkin(
    store: Store,
    patient: Account,
    now: datetime,
    *,
    base_url: str,
    occasion: str | None = None,
    send_email: bool = True,
) -> Notification:
    """Ask the patient for a check-in (in the app and, unless `send_email` is False, by email).
    One invitation per patient and occasion (default: the demo-clock moment), so a double click,
    or a second app copy sending the same scheduled invitation, adds nothing."""
    if patient.role != "patient":
        raise ValueError("only a patient is invited to a check-in")
    about = f"{INVITATION}{occasion or now.isoformat()}"
    email = (INVITATION_EMAIL, {"patient_link": patient_link(base_url)}) if send_email else None
    return _notify(store, about, patient, "checkin_due", "/pacient", now, email)
