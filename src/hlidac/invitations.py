"""Periodic check-in invitations (D57): every CHECKIN_INVITE_MINUTES, each patient of the team
gets the approved "checkin-due" invitation, in the app and by email with only a link to the app.

- The period runs on real time (an exception to principle 9, D57): the demo clock stands still
  unless the demo panel moves it. Records keep demo-clock timestamps, like everything else.
- One invitation per patient and period: the period's start is part of the notification id and
  of the email's outbox key, so a restart, or a second app copy with the same setting, sends
  nothing twice.
- Email only to deliverable addresses: the invented `@demo.local` accounts get the in-app notice
  only, so nothing bounces.
- Off unless CHECKIN_INVITE_MINUTES is set (only the Azure deployment sets it).
"""

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from hlidac import emails, notifications
from hlidac.store import Store

log = logging.getLogger(__name__)

POLL_SECONDS = 30


def period_start(at: datetime, every: timedelta) -> datetime:
    """The start of the period `at` falls in: periods are aligned to the Unix epoch, so every app
    copy agrees on them."""
    step = int(every.total_seconds())
    seconds = int(at.astimezone(UTC).timestamp())
    return datetime.fromtimestamp(seconds - seconds % step, UTC)


def invite_team(
    store: Store, team_id: str, period: datetime, now: datetime, *, base_url: str
) -> int:
    """Invite every patient of the team for `period` (a repeat in the same period adds nothing);
    returns how many patients it covered."""
    occasion = f"every:{period.isoformat()}"
    patients = [
        patient
        for plan in store.list_patient_plans(team_id)
        for patient in store.list_patient_accounts(plan.patient_id)
    ]
    for patient in patients:
        notifications.invite_to_checkin(
            store,
            patient,
            now,
            base_url=base_url,
            occasion=occasion,
            send_email=emails.deliverable(patient.email),
        )
    return len(patients)


def start_invitations(
    store: Store,
    real_now: Callable[[], datetime],
    demo_now: Callable[[], datetime],
    every: timedelta,
    team_id: str,
    base_url: str,
) -> threading.Event:
    """Invite the team once per period, the first time at once; set the event to stop."""
    stop = threading.Event()

    def loop() -> None:
        done: datetime | None = None
        while True:
            period = period_start(real_now(), every)
            if period != done:
                try:
                    count = invite_team(store, team_id, period, demo_now(), base_url=base_url)
                    log.info("check-in invitations for %s: %d patients", period.isoformat(), count)
                    done = period
                except Exception as error:  # noqa: BLE001 - retried on the next poll
                    log.warning("check-in invitations failed: %s", type(error).__name__)
            if stop.wait(POLL_SECONDS):
                return

    threading.Thread(target=loop, name="checkin-invitations", daemon=True).start()
    return stop
