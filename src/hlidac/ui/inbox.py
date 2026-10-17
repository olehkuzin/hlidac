"""The notification list (WP16), shared by the patient and the clinician app.

- A notification is looked up only among the session account's own (`recipient_account_id`), so
  another account's id is a 404, never a page or a mark-read.
- The text on screen comes only from `notifications.text_for` (approved templates), never from
  findings or a model.
- Opening one marks it read (a POST with the CSRF token) and redirects to its link, but only to
  a page of the account's own app; any other link falls back to the list.
- The email delivery state is the outbox row's status only, never the body or the address.
"""

from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

from hlidac import notifications
from hlidac.clock import PRAGUE
from hlidac.schemas import Account, Notification
from hlidac.store import Store

EMAIL_STATUS = {"pending": "čeká na odeslání", "done": "odesláno", "failed": "selhalo"}
EMAIL_ICON = {"pending": "◐", "done": "✔", "failed": "⚠"}


@dataclass(frozen=True)
class Entry:
    notification: Notification
    text: str
    when: str
    unread: bool
    in_app: str
    email: tuple[str, str] | None  # (icon, status word), or None without an email channel


def czech_time(moment: datetime) -> str:
    local = moment.astimezone(PRAGUE)
    return f"{local.day}. {local.month}. {local.year} v {local.hour}:{local:%M}"


def _email_state(store: Store, notification: Notification) -> tuple[str, str] | None:
    email = next((d for d in notification.deliveries if d.channel == "email"), None)
    if email is None or email.outbox_id is None:
        return None
    item = store.get_outbox_item(email.outbox_id)
    if item is None:  # not queued (yet): never claim it was sent
        return "⚠", "nenalezeno ve frontě"
    return EMAIL_ICON[item.status], EMAIL_STATUS[item.status]


def entries(store: Store, account: Account) -> list[Entry]:
    """The account's notifications, newest first."""
    found = sorted(
        store.list_notifications(account.account_id), key=lambda n: n.created_at, reverse=True
    )
    return [
        Entry(
            notification=n,
            text=notifications.text_for(n),
            when=czech_time(n.created_at),
            unread=n.read_at is None,
            in_app="doručeno" if any(d.channel == "in_app" for d in n.deliveries) else "—",
            email=_email_state(store, n),
        )
        for n in found
    ]


def unread(store: Store, account: Account) -> int:
    return notifications.unread_count(store, account.account_id)


def own(store: Store, account: Account, notification_id: str) -> Notification:
    found = store.list_notifications(account.account_id)
    notification = next((n for n in found if n.notification_id == notification_id), None)
    if notification is None:
        raise HTTPException(status_code=404)
    return notification


def open_one(
    request: Request, account: Account, notification_id: str, home: str
) -> RedirectResponse:
    """Mark the notification read and go to its link, if it is a page under `home`."""
    store: Store = request.app.state.store
    notification = own(store, account, notification_id)
    if notification.read_at is None:
        notification = notifications.mark_read(
            store, notification, request.app.state.clock.now()
        )  # audited there
    link = notification.link
    inside = link == home or link.startswith(f"{home}/")
    return RedirectResponse(link if inside else f"{home}/oznameni", status_code=303)
