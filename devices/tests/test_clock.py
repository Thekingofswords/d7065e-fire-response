"""One building clock (simclock.py, P3)."""
import time

import simclock


def test_follower_follows_the_simulator_and_dates_old_messages():
    clock = simclock.Follower(time_scale=10)
    assert not clock.following
    clock.on_message("sim/clock", simclock.message(1, 50.0, 1200.0))
    assert clock.following
    assert abs(clock.sim_time() - 1200.0) < 1.0
    # A retained message that is 3 real seconds old: 30 simulated seconds on.
    clock.on_message("sim/clock", dict(simclock.message(1, 50.0, 2000.0), ts=time.time() - 3))
    assert 2029.0 < clock.sim_time() < 2032.0


def test_follower_falls_back_without_jumping():
    clock = simclock.Follower(time_scale=10)
    old = dict(simclock.message(1, 0.0, 500.0), ts=time.time() - simclock.CLOCK_STALE_SECONDS - 1)
    clock.on_message("sim/clock", old)
    assert not clock.following          # too old to count
    fresh = simclock.message(1, 0.0, 900.0)
    clock.on_message("sim/clock", fresh)
    value = clock.sim_time()
    clock.base_real -= simclock.CLOCK_STALE_SECONDS + 1   # the simulator goes quiet
    # The local fallback continues from where the simulator's clock was.
    assert clock.sim_time() >= value


def test_week_minute_and_labels():
    clock = simclock.Follower(time_scale=1)
    start = simclock.parse_start("fri 23:59")
    assert simclock.label(simclock.week_minute(120, start)) == "Sat 00:01"
    clock.on_message("sim/clock", simclock.message(1, 0.0, 0.0))
    assert clock.label().startswith(simclock.label(simclock.parse_start())[:3])


def test_policy_clock_is_monotonic_and_bounded():
    class Fake:
        value = 100.0

        def sim_time(self):
            return self.value

    fake = Fake()
    clock = simclock.PolicyClock(fake, max_step=20)
    assert clock.now() == 100.0
    fake.value = 1000.0                  # a restarted simulator jumps ahead
    assert clock.now() == 120.0          # at most one step per call
    fake.value = 50.0                    # ... or back
    assert clock.now() == 120.0          # never backwards
