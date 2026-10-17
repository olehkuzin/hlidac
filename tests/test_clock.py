from datetime import date, datetime, timedelta

import pytest

from hlidac.clock import PRAGUE, DemoClock


def test_starts_at_as_of_in_prague():
    clock = DemoClock.from_as_of(date(2026, 10, 16))
    assert clock.now() == datetime(2026, 10, 16, 8, 0, tzinfo=PRAGUE)


def test_moves_only_when_advanced():
    clock = DemoClock.from_as_of(date(2026, 10, 16))
    start = clock.now()
    assert clock.now() == start
    assert clock.advance(timedelta(hours=1)) == start + timedelta(hours=1)
    assert clock.now() == start + timedelta(hours=1)


def test_never_goes_back_and_needs_a_timezone():
    clock = DemoClock.from_as_of(date(2026, 10, 16))
    with pytest.raises(ValueError):
        clock.advance(timedelta(minutes=-1))
    with pytest.raises(ValueError):
        DemoClock(datetime(2026, 10, 16, 8, 0))
