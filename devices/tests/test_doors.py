"""Doors: the door gateway's rules and door list, the agent's door policy,
and the fire model's response to doors opening and closing."""
import door_gateway as dg
import building_agent as ba
import common
from helpers import scenario


def door(**kw):
    d = {"id": "d1", "kind": "door", "state": "open", "lock_state": "unlocked",
         "normal_state": "open"}
    d.update(kw)
    return d


def test_door_list_covers_the_building(topo):
    doors = dg.build_doors(topo, scenario(door_open_prob=0.5, door_seed=3))
    kinds = [d["kind"] for d in doors.values()]
    assert kinds.count("door") >= 800 and kinds.count("fire_exit") >= 5
    assert len(doors) <= 2000                     # BuildSim's limit
    states = [d["state"] for d in doors.values() if d["kind"] == "door" and "lift" not in d]
    assert 0.3 < states.count("open") / len(states) < 0.7
    body = dg.snapshot(doors)
    assert {"zones", "normal_state", "fire_door"}.isdisjoint(body[0])


def test_close_lock_and_unlock():
    d = door()
    assert dg.apply_command(d, {"state": "closed", "lock_state": "locked"}) == ["state", "lock_state"]
    try:
        dg.apply_command(d, {"state": "open"})
        raise AssertionError("opened a locked door")
    except ValueError as exc:
        assert "locked" in str(exc)
    assert dg.apply_command(d, {"state": "open", "lock_state": "unlocked"})
    assert d["state"] == "open" and d["lock_state"] == "unlocked"


def test_normal_restores_the_scenario_state():
    d = door(state="closed", normal_state="open")
    dg.apply_command(d, {"state": "normal"})
    assert d["state"] == "open"


def test_jammed_doors_and_exits_are_protected():
    for bad, cmd in ((door(lock_state="jammed"), {"state": "closed"}),
                     (door(kind="fire_exit"), {"lock_state": "locked"}),
                     (door(kind="fire_exit"), {"state": "blocked"}),
                     (door(), {"state": "ajar"}),
                     (door(), {})):
        try:
            dg.apply_command(bad, cmd)
            raise AssertionError(f"accepted {cmd} on {bad}")
        except ValueError:
            pass


def test_fire_door_scope(topo):
    pair = next(iter(topo.conn_doors))
    assert dg.is_fire_door(topo, pair, "all")
    assert not dg.is_fire_door(topo, pair, "none")
    stair_pairs = [p for p in topo.conn_doors if dg.is_fire_door(topo, p, "stairs")]
    assert 0 < len(stair_pairs) < len(topo.conn_doors) / 4


def test_agent_door_policy_closes_the_floor_and_recalls_lifts():
    p = ba.BuildingPolicy(["level0/A", "level1/B"], {}, ba.RulesDetector())
    d = {"level0/A": {"state": "ALARM"}, "level1/B": {"state": "NORMAL"}}
    out = p.doors(0.0, d)
    assert out["levels"] == {"level0": "closed"} and out["lifts"] == "normal"
    d["level0/A"]["state"] = "SUPPRESSING"
    assert p.doors(10.0, d)["lifts"] == "blocked"
    d["level0/A"]["state"] = "NORMAL"
    assert p.doors(20.0, d)["levels"]["level0"] == "closed"        # not yet reset
    late = p.doors(20.0 + ba.DOOR_RESET_SECONDS, d)
    assert late["levels"]["level0"] == "normal" and late["lifts"] == "normal"


def test_closing_doors_changes_the_physics(quiet_sim, topo):
    model = quiet_sim.FireModel(scenario(["level0/A109"], door_open_prob=1.0), topo)
    room = model.cells["level0/A109"]
    q_open = room.q_vent
    pairs = [p for p in model.door_links if "level0/A109" in p]
    assert model.set_doors({p: False for p in pairs}) == len(pairs)
    assert room.q_vent < 0.5 * q_open
    assert model.set_doors({p: False for p in pairs}) == 0          # no change
    assert model.set_doors({p: True for p in pairs}) == len(pairs)
    assert abs(room.q_vent - q_open) < 1e-6
    assert common is not None


def test_snapshot_passes_buildsims_door_validation(topo, data_dir):
    """Every door the gateway publishes is accepted by BuildSim's rules
    (an entry node of the right room, a known room, valid kind/state/lock);
    one bad door makes BuildSim reject the whole PUT."""
    from fake_buildsim import FakeBuildSim
    fake = FakeBuildSim(data_dir=data_dir)
    try:
        doors = dg.snapshot(dg.build_doors(topo))
        errors = [e for e in (fake.door_error(d) for d in doors) if e]
        assert not errors, errors[:5]
        assert len(doors) > 800
    finally:
        fake.httpd.server_close()


# -- access control and the fire unlock (D1) --------------------------------

import simclock  # noqa: E402

MON_0930 = simclock.parse_start("mon 09:30")
MON_2200 = simclock.parse_start("mon 22:00")
SAT_1000 = simclock.parse_start("sat 10:00")


def test_building_clock():
    assert simclock.label(simclock.week_minute(3600, MON_0930)) == "Mon 10:30"
    assert simclock.in_hours(MON_0930) and not simclock.in_hours(MON_2200)
    assert not simclock.in_hours(SAT_1000)


def test_access_schedule_locks_after_hours(topo):
    doors = dg.build_doors(topo, None, MON_2200)
    by_access = {}
    for d in doors.values():
        by_access.setdefault(d.get("access") or d["kind"], []).append(d)
    assert set(by_access) >= {"stair", "office", "secure", "fire_exit"}
    assert all(d["lock_state"] == "locked" and d["state"] == "closed"
               for d in by_access["stair"] + by_access["office"] + by_access["secure"])
    assert all(d["lock_state"] == "unlocked" for d in by_access["fire_exit"])
    day = dg.build_doors(topo, None, MON_0930)
    assert all(d["lock_state"] == "unlocked" for d in day.values()
               if d.get("access") in ("stair", "office"))
    assert all(d["lock_state"] == "locked" for d in day.values() if d.get("access") == "secure")


def test_unlock_overrides_the_schedule_until_normal():
    d = door(access="stair", state="closed", lock_state="locked", normal_state="open")
    assert dg.apply_command(d, {"lock_state": "unlocked"}, MON_2200) == ["lock_state"]
    assert dg.apply_schedule(d, MON_2200) == []            # override holds
    dg.apply_command(d, {"lock_state": "normal"}, MON_2200)
    assert d["lock_state"] == "locked" and d["state"] == "closed"
    dg.apply_command(d, {"lock_state": "normal"}, MON_0930)
    assert d["lock_state"] == "unlocked"


def test_normal_never_opens_a_locked_door():
    d = door(access="office", state="closed", lock_state="locked", normal_state="open")
    dg.apply_command(d, {"state": "normal"}, MON_2200)
    assert d["state"] == "closed"


def test_agent_unlocks_egress_on_any_alarm():
    p = ba.BuildingPolicy(["level0/A"], {}, ba.RulesDetector())
    assert p.doors(0.0, {"level0/A": {"state": "NORMAL"}})["egress"] == "normal"
    assert p.doors(10.0, {"level0/A": {"state": "ALARM"}})["egress"] == "unlocked"
    assert p.doors(20.0, {"level0/A": {"state": "NORMAL"}})["egress"] == "unlocked"
    late = p.doors(20.0 + ba.DOOR_RESET_SECONDS, {"level0/A": {"state": "NORMAL"}})
    assert late["egress"] == "normal"


def test_door_equipment_for_access_controlled_doors(topo):
    doors = dg.build_doors(topo, None, MON_2200)
    items = [dg.door_equipment(d) for d in doors.values() if dg.has_equipment(d)]
    assert 50 < len(items) < 200
    eq = items[0]
    assert eq["type"] == "door_lock" and eq["sensors"][0]["data_type"] == "binary"
    assert eq["actuators"][0]["state"] in ("locked", "unlocked")
