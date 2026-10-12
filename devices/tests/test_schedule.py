"""The occupants' daily schedule (populate_evacuation.Schedule)."""
import importlib.util
import os
import random

import pytest

PATH = os.environ.get("E2E_EVACUATION_SCRIPT") or os.path.join(
    os.path.dirname(__file__), "..", "..", "examples", "api", "scenario", "populate_evacuation.py")


@pytest.fixture(scope="module")
def evac():
    if not os.path.exists(PATH):
        pytest.skip("populate_evacuation.py not found")
    spec = importlib.util.spec_from_file_location("populate_evacuation", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def present_share(evac, schedule, plans, minute_of_day, day=0):
    sim_time = ((day * 1440 + minute_of_day) - schedule.origin) * 60.0
    return sum(schedule.wants_in(p, sim_time) for p in plans) / len(plans)


def test_weekday_profile(evac):
    schedule = evac.Schedule("00:00", "mon")
    rng = random.Random(1)
    plans = [schedule.plan_day(rng) for _ in range(400)]
    assert present_share(evac, schedule, plans, 3 * 60) == 0          # night
    assert present_share(evac, schedule, plans, 7 * 60 + 30) < 0.25   # arriving
    assert present_share(evac, schedule, plans, 10 * 60) > 0.95       # working
    lunch = present_share(evac, schedule, plans, 12 * 60)
    assert 0.6 < lunch < 0.95                                         # lunch dip
    assert present_share(evac, schedule, plans, 17 * 60) < 0.7        # leaving
    assert present_share(evac, schedule, plans, 19 * 60) == 0


def test_weekend_is_empty(evac):
    schedule = evac.Schedule("00:00", "mon")
    plans = [schedule.plan_day(random.Random(i)) for i in range(50)]
    assert present_share(evac, schedule, plans, 10 * 60, day=5) == 0


def test_schedule_off_keeps_everyone_in(evac):
    schedule = evac.Schedule("03:00", "sun", enabled=False)
    assert schedule.wants_in(schedule.plan_day(random.Random(1)), 0.0)


def test_clock_formatting(evac):
    schedule = evac.Schedule("07:30", "fri")
    assert evac.fmt_clock(schedule.minute(0.0)) == "Fri 07:30"
    assert evac.fmt_clock(schedule.minute(17 * 3600)) == "Sat 00:30"
