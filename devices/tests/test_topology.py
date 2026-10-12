"""The building graph derived from BuildSim's floor plans (topology.py)."""
import json

import topology


def test_whole_building_is_loaded(topo):
    assert len(topo.zones) == 956
    assert {z.level for z in topo.zones.values()} == {"level0", "level1", "level2"}
    assert len(topo.connections) > 900


def test_known_doorways(topo):
    corridor = "level0/A103+A112_B+A103"
    assert corridor in topo.neighbours["level0/A109"]
    assert "level0/A104" in topo.neighbours[corridor]


def test_stairs_link_adjacent_floors(topo):
    assert len(topo.stairs) >= 20
    assert ("level0/A104", "level1/A204a") in topo.stairs
    for a, b in topo.stairs:
        assert topo.zones[a].level != topo.zones[b].level


def test_long_corridors_are_split_into_cells(topo):
    cells = topo.cells["level0/A103+A112_B+A103"]
    assert len(cells) >= 3
    assert abs(sum(c["share"] for c in cells) - 1.0) < 1e-6
    # Doors along the corridor open into different cells.
    ends = {topo.conn_cells[p][0] for p in topo.conn_cells if p[0] == "level0/A103+A112_B+A103"}
    assert len(ends) >= 3


def test_json_round_trip_and_subgraph(topo):
    copy = topology.Topology.from_json(json.loads(json.dumps(topo.to_json())))
    assert copy.zones.keys() == topo.zones.keys()
    assert copy.conn_cells == topo.conn_cells
    keys = topo.neighbourhood("level0/A109", 8)
    sub = topo.subgraph(keys)
    assert set(sub.zones) == set(keys)
    assert all(a in sub.zones and b in sub.zones for a, b, _, _ in sub.connections)


def test_sensor_scope_covers_rooms_and_corridors(topo):
    zones = topo.sensor_zones()
    assert len(zones) == 956
    assert set(topo.sensor_zones(["level1"])) <= {k for k in zones if k.startswith("level1/")}
