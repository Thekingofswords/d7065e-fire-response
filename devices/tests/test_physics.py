"""The fire and smoke model (fire_simulator.FireModel) against the
behaviour it is meant to show. Times are simulated seconds."""
import collections

import common
import scenarios
from helpers import scenario

FIRE_ROOM = "level0/A109"
CORRIDOR = "level0/A103+A112_B+A103"


def run(model, seconds, dt=10.0, each=None):
    t_end = model.time + seconds
    while model.time < t_end:
        if each:
            each(model)
        model.step(dt)


def obscuration(model, zone):
    return model.rooms[zone].outputs()[common.LAYER_OBSCURATION]


def test_unsuppressed_fast_fire_flashes_over(quiet_sim, topo):
    model = quiet_sim.FireModel(scenario([FIRE_ROOM]), topo)
    run(model, 900)
    phases = {c.phase for c in model.rooms[FIRE_ROOM].cells}
    assert quiet_sim.FULLY_DEVELOPED in phases or quiet_sim.DECAYING in phases


def test_sprinkler_puts_a_growing_fire_out(quiet_sim, topo):
    model = quiet_sim.FireModel(scenario([FIRE_ROOM]), topo)
    run(model, 120)
    assert model.rooms[FIRE_ROOM].hrr > 100
    model.rooms[FIRE_ROOM].sprinkler_on = True
    run(model, 600)
    assert model.rooms[FIRE_ROOM].hrr == 0
    assert model.rooms[FIRE_ROOM].phase == quiet_sim.EXTINGUISHED


def smoke_arrival(quiet_sim, topo, seconds=900, threshold=5.0, purge=False, **kw):
    """First time each zone exceeds `threshold` %/m: 120 s smouldering, then
    a fast flaming fire, sprinkler at 230 s (as in the fixed scenario)."""
    model = quiet_sim.FireModel(scenario([FIRE_ROOM], incipient=120.0, **kw), topo)
    first = {}

    def each(m):
        m.rooms[FIRE_ROOM].sprinkler_on = m.time >= 230
        for z in m.active_zones:
            o = obscuration(m, z)
            if purge:
                m.rooms[z].damper = 1.0 if o >= 3 else (0.0 if o <= 0.5 else m.rooms[z].damper)
            if o > threshold and z not in first:
                first[z] = m.time
    run(model, seconds, each=each)
    return model, first


def hops_from(topo, source):
    neighbours = {k: list(v) for k, v in topo.neighbours.items()}
    for a, b in topo.stairs:
        neighbours[a].append(b)
        neighbours[b].append(a)
    hops, queue = {source: 0}, collections.deque([source])
    while queue:
        k = queue.popleft()
        for n in neighbours[k]:
            if n not in hops:
                hops[n] = hops[k] + 1
                queue.append(n)
    return hops


def test_smoke_reaches_far_zones_later_and_thinner(quiet_sim, topo):
    """Issue: smoke used to reach every adjacent room at once."""
    _, first = smoke_arrival(quiet_sim, topo, door_open_prob=0.5, open_doors=[FIRE_ROOM])
    hops = hops_from(topo, FIRE_ROOM)
    by_hop = collections.defaultdict(list)
    for z, t in first.items():
        by_hop[hops[z]].append(t)
    median = {h: sorted(v)[len(v) // 2] for h, v in by_hop.items()}
    assert median[1] < median[3] < median[5]
    # Not every zone near the fire fills at the same moment.
    near = by_hop[2] + by_hop[3]
    assert max(near) - min(near) >= 30


def test_closed_doors_keep_smoke_out(quiet_sim, topo):
    _, open_all = smoke_arrival(quiet_sim, topo, door_open_prob=1.0)
    _, closed = smoke_arrival(quiet_sim, topo, door_open_prob=0.0, open_doors=[FIRE_ROOM])

    def rooms(first):
        return [z for z in first if topo.zones[z].type == "room"]
    assert len(rooms(closed)) < len(rooms(open_all)) / 2


def test_smoke_rises_to_the_floor_above(quiet_sim, topo):
    """Issue: smoke never reached other floors."""
    _, first = smoke_arrival(quiet_sim, topo, seconds=1200, threshold=1.0,
                             door_open_prob=0.5, open_doors=[FIRE_ROOM])
    assert any(z.startswith("level1/") for z in first)


def test_purge_clears_smoke(quiet_sim, topo):
    """Issue: smoke never dissipated."""
    plain, _ = smoke_arrival(quiet_sim, topo, seconds=1800, door_open_prob=0.5,
                             open_doors=[FIRE_ROOM])
    purged, _ = smoke_arrival(quiet_sim, topo, seconds=1800, purge=True, door_open_prob=0.5,
                              open_doors=[FIRE_ROOM])
    assert obscuration(purged, FIRE_ROOM) < 1.0
    assert obscuration(plain, FIRE_ROOM) > 3 * obscuration(purged, FIRE_ROOM)


def test_quiet_building_has_no_active_zones(quiet_sim, topo):
    model = quiet_sim.FireModel(scenario(), topo)
    run(model, 300)
    assert not model.active


def test_door_states_are_deterministic():
    s = scenario(door_open_prob=0.5, door_seed=7)
    a = [scenarios.door_is_open(s, "level0/A1", f"level0/B{i}") for i in range(200)]
    b = [scenarios.door_is_open(s, f"level0/B{i}", "level0/A1") for i in range(200)]
    assert a == b
    assert 60 < sum(a) < 140
    assert scenarios.door_is_open(dict(s, open_doors=["level0/A1"]), "level0/A1", "level0/X")
