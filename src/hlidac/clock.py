"""The one controllable clock (principle 9).

Domain code receives `now` (or a `Clock`) as an argument and never reads the system time. The demo
starts at AS_OF and moves only when the demo panel advances it, so deadlines and "days since the
last dose" can be shown live.
"""

import threading
from datetime import date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

PRAGUE = ZoneInfo("Europe/Prague")


class Clock(Protocol):
    def now(self) -> datetime: ...


class DemoClock:
    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("the demo clock needs a timezone-aware start")
        self._now = start
        self._lock = threading.Lock()

    @classmethod
    def from_as_of(cls, as_of: date, at: time = time(8, 0)) -> "DemoClock":
        return cls(datetime.combine(as_of, at, tzinfo=PRAGUE))

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        if delta < timedelta(0):
            raise ValueError("the demo clock never goes back")
        with self._lock:
            self._now += delta
            return self._now


class SystemClock:
    """Real time, for security timers only (login-code expiry, rate limits): a frozen or advanced
    demo clock must never keep a code valid or lock someone out. Domain code uses `DemoClock`."""

    def now(self) -> datetime:
        return datetime.now(PRAGUE)
