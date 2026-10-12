"""Decision logic of the building agent (building_agent.BuildingPolicy)."""
import building_agent as ba
from helpers import reading

ROOMS = ["fire", "next", "far"]
NEIGHBOURS = {"fire": ["next"], "next": ["fire", "far"], "far": ["next"]}


def policy():
    return ba.BuildingPolicy(ROOMS, NEIGHBOURS, ba.RulesDetector())


def drive(p, frames, start=0.0, dt=10.0):
    """frames: list of {room: reading}; returns the last decisions."""
    t, d = start, {}
    for frame in frames:
        full = {r: frame.get(r, reading(temperature=21.0 + (t % 3) * 0.01)) for r in ROOMS}
        d = p.step(t, full)
        t += dt
    return d, t


def test_fire_releases_the_sprinkler_and_only_there():
    p = policy()
    frames = [{}] * 3 + [{"fire": reading(temperature=21 + 6 * i, smoke=4 * i, co=20 * i)}
                         for i in range(1, 12)]
    d, _ = drive(p, frames)
    assert d["fire"]["state"] == "SUPPRESSING" and d["fire"]["sprinkler"]
    assert not d["next"]["sprinkler"] and not d["far"]["sprinkler"]


def test_hot_smoke_from_next_door_is_attributed_not_released():
    p = policy()
    frames = [{}] * 3
    for i in range(1, 10):
        frames.append({"fire": reading(temperature=30 + 12 * i, smoke=20, co=200),
                       "next": reading(temperature=21 + 4 * i, smoke=6, co=30)})
    d, _ = drive(p, frames)
    assert d["fire"]["sprinkler"]
    assert not d["next"]["sprinkler"]


def test_sprinkler_runs_its_minimum_time_then_stops_when_cool():
    p = policy()
    hot = [{"fire": reading(temperature=95, smoke=20, co=200)}] * 4
    d, t = drive(p, hot)
    assert d["fire"]["sprinkler"]
    cool = [{"fire": reading(temperature=24 + (i % 2) * 0.1, smoke=1, co=5)} for i in range(10)]
    d, t = drive(p, cool, start=t)
    assert d["fire"]["sprinkler"], "must run MIN_DISCHARGE_SECONDS"
    d, t = drive(p, cool * 3, start=t)
    assert not d["fire"]["sprinkler"]


def test_smoke_purge_opens_and_closes_with_hysteresis():
    p = policy()
    d, t = drive(p, [{"next": reading(smoke=4.0)}] * 2)
    assert d["next"]["damper"] == 1.0
    d, t = drive(p, [{"next": reading(smoke=1.0)}] * 20, start=t)   # between the limits
    assert d["next"]["damper"] == 1.0
    d, t = drive(p, [{"next": reading(smoke=0.2)}] * 5, start=t)
    assert d["next"]["damper"] == 0.0


def test_faulty_thermometer_falls_back_to_smoke_and_co():
    p = policy()
    frames = [{}] * 3 + [{"fire": reading(temperature=21.0, smoke=15 + i % 3 * 0.1,
                                          co=60 + i % 2)} for i in range(25)]
    d, _ = drive(p, frames)
    # The frozen 21.0 degC is detected (no change for FROZEN_SECONDS) and the
    # dense smoke + CO then releases the sprinkler.
    assert d["fire"]["faults"].get("temperature") == "frozen"
    assert d["fire"]["sprinkler"]


def test_offline_zone_does_not_crash_the_policy():
    p = policy()
    d = p.step(0.0, {"fire": reading(), "next": reading()}, stale={"far": {"temperature"}})
    assert "far" not in d or d["far"]["state"] == "NORMAL"


def test_safety_net_releases_even_when_the_detector_says_no():
    class Blind:
        name = "blind"

        def assess(self, room_features, neighbours, faults=None):
            return {r: {"p_fire": 0.0, "fire": False, "alarm": False, "alarm_clear": True,
                        "reason": ""} for r in room_features}

    p = ba.BuildingPolicy(ROOMS, NEIGHBOURS, Blind())
    d, _ = drive(p, [{}] * 2 + [{"fire": reading(temperature=95 + i * 0.3)} for i in range(4)])
    assert d["fire"]["sprinkler"]
    assert "safety net" in d["fire"]["reason"]


def test_blind_zone_gets_water_when_smoke_leaks_next_door():
    """Fire doors closed, fire-room sensors offline: only leaking smoke next
    door shows the fire; the blind zone must still get water."""
    p = policy()
    t, d = 0.0, {}
    for i in range(8):
        full = {"fire": reading(),   # its last value, repeated: the device is offline
                "next": reading(temperature=21 + i * 0.01, smoke=0.5 + 0.4 * i, co=2 + i),
                "far": reading(temperature=21 + i * 0.02)}
        d = p.step(t, full, stale={"fire": {"temperature", "smoke", "co", "co2"}})
        t += 10
    assert d["fire"]["sprinkler"], d["fire"]
    assert not d["next"]["sprinkler"]


# --- the evacuation plan (D4) ----------------------------------------------

def _plan_policy():
    rooms = [f"level{lv}/r{i}" for lv in range(3) for i in range(2)]
    return ba.BuildingPolicy(rooms, {r: [] for r in rooms}, ba.RulesDetector()), rooms


def _decisions(rooms, **states):
    return {r: {"state": states.get(r, "NORMAL")} for r in rooms}


def test_plan_is_phased_then_full_then_all_clear():
    p, rooms = _plan_policy()
    quiet = {r: reading() for r in rooms}
    assert p.evacuation_plan(0, _decisions(rooms), quiet)["phase"] == "normal"
    alarm = _decisions(rooms, **{"level1/r0": "ALARM"})
    plan = p.evacuation_plan(10, alarm, quiet)
    assert plan["phase"] == "evacuate"
    assert plan["evacuate_levels"] == ["level1", "level2"], "alarm floor and above first"
    plan = p.evacuation_plan(10 + ba.FULL_EVACUATION_SECONDS, alarm, quiet)
    assert plan["evacuate_levels"] == ["level0", "level1", "level2"]
    # The alarm clears: the evacuation holds, then the agent gives the all-clear.
    t = 20 + ba.FULL_EVACUATION_SECONDS
    plan = p.evacuation_plan(t, _decisions(rooms), quiet)
    assert plan["phase"] == "evacuate" and len(plan["evacuate_levels"]) == 3, "never shrinks"
    plan = p.evacuation_plan(t + ba.ALL_CLEAR_SECONDS + 1, _decisions(rooms), quiet)
    assert plan["phase"] == "all_clear"
    assert p.evacuation_plan(t + ba.ALL_CLEAR_SECONDS + 11, _decisions(rooms), quiet)["phase"] \
        == "normal"


def test_confirmed_fire_evacuates_every_floor_and_lists_risk():
    p, rooms = _plan_policy()
    readings = {r: reading() for r in rooms}
    readings["level2/r1"] = reading(smoke=25)
    plan = p.evacuation_plan(0, _decisions(rooms, **{"level2/r0": "SUPPRESSING"}), readings,
                             risk={"level2/r1": 0.9, "level2/r0": 0.99})
    assert plan["evacuate_levels"] == ["level0", "level1", "level2"]
    assert plan["fire_zones"] == ["level2/r0"]
    assert plan["smoke_zones"] == ["level2/r1"]
    assert plan["risk_zones"] == ["level2/r1"], "burning zones are fire, not risk"


def test_level_order_is_natural():
    assert sorted(["level10", "level2", "level0"], key=ba._level_order) == \
        ["level0", "level2", "level10"]
