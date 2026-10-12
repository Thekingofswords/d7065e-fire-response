"""One building clock for every service.

The fire simulator owns simulated time. It counts simulated seconds since it
started (across scenarios) and publishes, on sim/clock (retained, every
tick):

    {"scenario_id", "scenario_time",          # per scenario
     "sim_time",                              # simulated s since start
     "week_minute",                           # 0 = Monday 00:00
     "clock": "Mon 07:31",                    # for people
     "ts"}

The building's wall clock starts at BUILDING_CLOCK_START ("mon 07:30") and
runs at TIME_SCALE. Every other service follows that clock with `Follower`,
which falls back to its own monotonic clock x TIME_SCALE (and says so) when
the simulator is silent, so a restarted agent, the door schedule and the
occupants all agree on the time.
"""
import os
import threading
import time

import common

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEK_MINUTES = 7 * 1440
START = os.environ.get("BUILDING_CLOCK_START", "mon 07:30")
# A clock message older than this (real seconds) no longer counts.
CLOCK_STALE_SECONDS = float(os.environ.get("CLOCK_STALE_SECONDS", "10"))


def parse_start(text=START):
    """"mon 07:30" -> minute of the week."""
    parts = text.strip().lower().split()
    day = DAYS.index(parts[0][:3]) if len(parts) > 1 else 0
    hhmm = parts[-1]
    hours, minutes = hhmm.split(":")
    return day * 1440 + int(hours) * 60 + int(minutes)


def week_minute(sim_time, start_minute=None):
    start_minute = parse_start() if start_minute is None else start_minute
    return (start_minute + sim_time / 60.0) % WEEK_MINUTES


def label(minute):
    minute = int(minute) % WEEK_MINUTES
    return f"{DAYS[minute // 1440].capitalize()} {minute % 1440 // 60:02d}:{minute % 60:02d}"


def is_workday(minute):
    return int(minute % WEEK_MINUTES) // 1440 < 5


def day_minute(minute):
    return minute % 1440


def in_hours(minute, hours="07:00-18:00", weekdays_only=True):
    """Whether the week minute falls inside daily opening hours."""
    a, b = hours.split("-")
    open_m = int(a.split(":")[0]) * 60 + int(a.split(":")[1])
    close_m = int(b.split(":")[0]) * 60 + int(b.split(":")[1])
    if weekdays_only and not is_workday(minute):
        return False
    return open_m <= day_minute(minute) < close_m


def message(scenario_id, scenario_time, sim_time, start_minute=None):
    m = week_minute(sim_time, start_minute)
    return {"scenario_id": scenario_id, "scenario_time": round(scenario_time, 1),
            "sim_time": round(sim_time, 1), "week_minute": round(m, 2),
            "clock": label(m), "ts": time.time()}


class Follower:
    """Simulated time from sim/clock, with a local fallback.

        clock = simclock.Follower()
        bus.subscribe(mqtt_bus.CLOCK_TOPIC, clock.on_message, qos=0)
        clock.sim_time()      # simulated seconds since the simulator started
        clock.week_minute()   # building wall clock
        clock.following       # False while running on the local fallback
    """

    def __init__(self, time_scale=None):
        self.time_scale = float(time_scale or common.TIME_SCALE)
        self.lock = threading.Lock()
        self.base_sim = None      # sim_time of the last clock message
        self.base_real = None     # monotonic time it arrived
        self.local_origin = time.monotonic()
        self.offset = 0.0         # keeps the fallback continuous

    def on_message(self, topic, payload):
        if not isinstance(payload, dict) or "sim_time" not in payload:
            return
        try:
            sim = float(payload["sim_time"])
            age = max(0.0, time.time() - float(payload.get("ts", time.time())))
        except (TypeError, ValueError):
            return
        with self.lock:
            # A retained message from a simulator that has since stopped is
            # old: date it by its timestamp, not by its arrival.
            self.base_sim, self.base_real = sim, time.monotonic() - age

    @property
    def following(self):
        with self.lock:
            return self.base_real is not None and \
                time.monotonic() - self.base_real < CLOCK_STALE_SECONDS

    def _local(self):
        return (time.monotonic() - self.local_origin) * self.time_scale + self.offset

    def sim_time(self):
        with self.lock:
            fresh = self.base_real is not None and \
                time.monotonic() - self.base_real < CLOCK_STALE_SECONDS
            if fresh:
                value = self.base_sim + (time.monotonic() - self.base_real) * self.time_scale
                # Keep the fallback continuous should the simulator go quiet.
                self.offset = value - (time.monotonic() - self.local_origin) * self.time_scale
                return value
        return self._local()

    def week_minute(self):
        return week_minute(self.sim_time())

    def label(self):
        return label(self.week_minute())


class PolicyClock:
    """Monotonic simulated time for decision logic: follows the building
    clock, but never runs backwards and never jumps more than `max_step`
    simulated seconds per call (a restarted simulator must not make a
    sprinkler's minimum discharge time pass in an instant)."""

    def __init__(self, follower, max_step):
        self.follower = follower
        self.max_step = max_step
        self.value = None

    def now(self):
        target = self.follower.sim_time()
        if self.value is None:
            self.value = target
        else:
            self.value += min(max(target - self.value, 0.0), self.max_step)
        return self.value
