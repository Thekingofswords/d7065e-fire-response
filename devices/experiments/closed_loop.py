"""Closed-loop runs without the network: the same fire model, instrument
models and building policy as the live services, stepped as fast as the CPU
allows. The experiment scripts build on this.

    run(scenario, topo, policy="rules")  ->  Run with .series and .zones

policy: "none" (sprinklers and dampers never operate), "rules", or "model"
(the trained LightGBM detector, models/fire_lgbm.txt by default).
"""
import collections
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEVICES = os.path.dirname(HERE)
sys.path.insert(0, DEVICES)

import building_agent  # noqa: E402
import common  # noqa: E402
import fire_simulator  # noqa: E402
import health  # noqa: E402
import sensor  # noqa: E402
import topology  # noqa: E402

fire_simulator.print = lambda *a, **k: None   # the model's console log is noise here

DEFAULT_DATA = os.path.join(DEVICES, "..", "data", "abuilding")
DEFAULT_MODEL = os.path.join(DEVICES, "..", "models", "fire_lgbm.txt")


def load_topology(data_dir=None):
    data_dir = data_dir or os.environ.get("BUILDING_DATA_DIR") or DEFAULT_DATA
    return topology.Topology.from_files(data_dir)


class Run:
    def __init__(self, policy, zones):
        self.policy = policy
        self.zones = zones
        # zone -> list of per-tick dicts
        self.series = collections.defaultdict(list)
        self.release = {}       # zone -> first sim time with sprinkler on
        self.purge = {}         # zone -> first sim time with damper open
        self.flashover = {}     # zone -> first sim time fully developed


def door_states(model, topo, scenario, doors):
    """Turn the agent's door decisions into {pair: open} for the model (what
    the door gateway would do): closed levels/zones close their doors,
    "normal" restores the scenario's door states."""
    import door_gateway
    import scenarios as scenario_lib
    states = {}
    for pair in model.door_links:
        level = topology.split(pair[0])[0]
        closed = (doors["levels"].get(level) == "closed"
                  and door_gateway.is_fire_door(topo, pair)) or any(
            doors["zones"].get(z) == "closed" for z in pair)
        states[pair] = False if closed else scenario_lib.door_is_open(scenario, *pair)
    return states


def run(scenario, topo, policy="rules", duration=1800.0, dt=10.0, seed=1,
        record=None, model_path=None, people=None, doors=True):
    """Simulate `duration` seconds. record: zones whose full time series is
    kept (default all); every zone's release/flashover times are kept.
    policy "<detector>_no_doors" (e.g. "rules_no_doors") runs the agent
    without its fire-door policy, for comparison."""
    if policy.endswith("_no_doors"):
        policy, doors = policy[:-len("_no_doors")], False
    rng = random.Random(seed)
    model = fire_simulator.FireModel(scenario, topo)
    zones = list(model.rooms)
    record = set(zones if record is None else record)
    instruments = {
        z: sensor.Instruments(topo.zones[z].name, random.Random(rng.random()), topo.zones[z].volume)
        for z in zones
    }
    people = people or {}
    agent = None
    if policy != "none":
        detector = building_agent.make_detector(
            policy, model_path or os.environ.get("MODEL_PATH") or DEFAULT_MODEL)
        if detector.name != policy:
            raise RuntimeError(f"could not load the {policy} detector")
        corridors = {z for z in zones if topo.zones[z].type == "corridor"}
        agent = building_agent.BuildingPolicy(zones, topo.neighbour_map(zones), detector,
                                              corridors)
    result = Run(policy, zones)
    stale_after = building_agent.STALE_SECONDS * common.TIME_SCALE

    while model.time < duration:
        model.step(dt)
        readings, stale = {}, {}
        for z, view in model.rooms.items():
            truth = view.outputs()
            truth.update(model.labels(z))
            inst = instruments[z]
            inst.step(truth, dt, people.get(z, 0), view.damper)
            readings[z] = inst.readings()
            if inst.offline and inst.fault_age >= stale_after:
                stale[z] = set(health.SIGNALS)
            if view.phase == fire_simulator.FULLY_DEVELOPED and z not in result.flashover:
                result.flashover[z] = model.time
            if z in record:
                result.series[z].append({
                    "time": model.time,
                    "true_temperature": truth[common.LAYER_GAS_TEMP],
                    "true_smoke": truth[common.LAYER_OBSCURATION],
                    "visibility": truth[common.LAYER_VISIBILITY],
                    "hrr": truth[common.LAYER_HRR],
                    "temperature": readings[z]["temperature"],
                    "smoke": readings[z]["smoke"],
                    "co": readings[z]["co"],
                    "sprinkler": int(view.sprinkler_on),
                    "damper": view.damper,
                    "phase": view.phase,
                    "burning": int(view.burning),
                })
        if agent is not None:
            for z, d in agent.step(model.time, readings, stale).items():
                view = model.rooms[z]
                view.sprinkler_on = d["sprinkler"]
                view.damper = d.get("damper", 0.0)
                if d["sprinkler"] and z not in result.release:
                    result.release[z] = model.time
                if d.get("damper", 0) > 0 and z not in result.purge:
                    result.purge[z] = model.time
            if doors:
                decisions = {z: {"state": agent.states[z].state} for z in zones}
                model.set_doors(door_states(model, topo, scenario,
                                            agent.doors(model.time, decisions)))
    result.model = model
    return result


def flaming_start(scenario):
    fire = scenario["fires"][0]
    return fire["start"] + fire.get("incipient", 0.0)


def save_csv(path, rows, columns=None):
    import csv
    rows = list(rows)
    if not rows:
        return
    columns = columns or list(rows[0])
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def pyplot():
    """matplotlib.pyplot with a file-only backend, or None if missing."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        return plt
    except ImportError:
        print("matplotlib not installed: CSV only (pip install matplotlib for plots)")
        return None
