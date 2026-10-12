"""Occupant movement and viewer behaviour (populate_evacuation.py)."""
import importlib.util
import os
import types

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


def _stair_path():
    return [{"name": "stair2", "level": "level2", "x": 10.0, "y": 10.0},
            {"name": "stair1", "level": "level1", "x": 10.0, "y": 10.0},
            {"name": "exit", "level": "level1", "x": 20.0, "y": 10.0}]


def test_replanning_on_the_stairs_keeps_the_progress(evac):
    """Re-plans every 2 s used to reset the stair timer: people upstairs never
    got down (live run, 7 of 8 people not out)."""
    p = evac.Person("p", "Ann", "woman", "level2", "stair2", 10.0, 10.0)
    p.state = "evacuating"
    p.follow(_stair_path(), speed=6.0)
    for _ in range(15):                  # 15 x (2 s walking + a re-plan)
        p.step(2.0, stair_seconds=20.0)
        p.follow(_stair_path()[1:] if p.level == "level1" else _stair_path(), 6.0,
                 keep_stair=p.stair_timer > 0)
    assert p.level == "level1"


def test_entity_positions_stay_on_the_floor_page(evac):
    evac.PAGES["level1"] = (384.56, 365.92)
    p = evac.Person("p", "Ann", "woman", "level1", None, 390.0, -3.0)
    x, y = p.entity(types.SimpleNamespace(is_room=lambda *a: False), 600)["position"]
    assert 0 < x < 384.56 and 0 < y < 365.92
    room = {"center": [400.0, 100.0], "polygon": [[370, 90], [420, 90], [420, 110], [370, 110]]}
    import random
    for seed in range(20):
        x, y = evac.random_point_in_room(room, random.Random(seed), level="level1")
        assert evac.on_page("level1", x, y)


def _evacuation(evac, mode):
    e = evac.Evacuation.__new__(evac.Evacuation)
    e.args = types.SimpleNamespace(route_display=mode, route_cycle=30.0)
    e.shown_route = None
    e.route_focus = None
    e.last_route_put = float("-inf")
    e.route_cycle = 0
    return e


def _evacuees(evac):
    people = []
    for i, x in enumerate((50.0, 90.0)):
        p = evac.Person(f"p{i}", "N", "man", "level1", None, x, 10.0)
        p.follow(_stair_path()[1:], 6.0)
        p.exit = "exit"
        people.append(p)
    return people


def test_route_is_sent_once_so_the_camera_stays_free(evac):
    """BuildSim's viewer flies to every route it receives; the default sends
    one route per evacuation, not one every few seconds."""
    e = _evacuation(evac, "once")
    people = _evacuees(evac)
    sent = [e.route_to_show(people, now=t)[0] is not None for t in range(0, 600, 3)]
    assert sum(sent) == 1
    route, clear = e.route_to_show([], now=601)
    assert route is None and clear, "the route is cleared after the evacuation"
    off = _evacuation(evac, "off")
    assert all(off.route_to_show(people, now=t)[0] is None for t in range(0, 60, 3))
    cycle = _evacuation(evac, "cycle")
    assert sum(cycle.route_to_show(people, now=t)[0] is not None
               for t in range(0, 120, 3)) == 4
