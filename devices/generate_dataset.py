"""Generate a labelled training dataset offline, much faster than real time.

Runs the same fire model (fire_simulator.py), the same instrument models
(sensor.py) and the same building policy (building_agent.py) as the live
system, but without BuildSim and without waiting: thousands of scenarios
take minutes. The output has the same schema as recorder.py (dataset.py),
so offline and live recordings can be mixed for training.

    python generate_dataset.py --scenarios 2000 --out ../recordings/offline

Sprinkler control during generation (--policy):
    mixed  (default) 70 % of scenarios controlled by the rules policy
           (sprinklers, purge dampers and the fire-door policy),
           30 % uncontrolled so the data also contains flashover
    rules  always controlled by the building agent's rules policy
    model  controlled by a trained model (--model PATH)
    none   sprinklers never operate

Each scenario simulates a random neighbourhood of the real building (up to
--zones zones around a random room or corridor, from topology.py), so the
neighbour features match what the live agent sees. The neighbourhood is
stored with the scenario in scenarios.jsonl. The building comes from
--data-dir (the repository's data/abuilding) or a running BuildSim.
"""
import argparse
import json
import multiprocessing
import os
import random
import time

import building_agent
import common
import dataset
import fire_simulator
import health
import scenarios
import sensor
import topology


def run_scenario(job):
    scenario, seed, dt, policy_kind, model_path, sub_json = job
    rng = random.Random(seed)

    sub = topology.Topology.from_json(sub_json)
    model = fire_simulator.FireModel(scenario, sub)
    rooms = list(model.rooms)
    instruments = {
        room: sensor.Instruments(sub.zones[room].name, random.Random(rng.random()),
                                 sub.zones[room].volume)
        for room in rooms
    }
    people = {room: rng.choice([0, 0, 0, 1, 2, 3, 5]) for room in rooms}

    policy = None
    if policy_kind != "none":
        detector = building_agent.make_detector(
            "model" if policy_kind == "model" else "rules", model_path
        )
        corridors = {k for k in rooms if sub.zones[k].type == "corridor"}
        policy = building_agent.BuildingPolicy(rooms, sub.neighbour_map(rooms), detector,
                                               corridors)

    # An offline sensor is noticed once its BuildSim timestamp is older than
    # the agent's limit (real seconds, converted to simulated time).
    stale_after = building_agent.STALE_SECONDS * common.TIME_SCALE

    rows = []
    while not model.finished:
        model.step(dt)
        readings, stale = {}, {}
        for name, room in model.rooms.items():
            truth = room.outputs()
            truth.update(model.labels(name))
            instrument = instruments[name]
            instrument.step(truth, dt, people[name], room.damper)
            readings[name] = instrument.readings()
            if instrument.offline and instrument.fault_age >= stale_after:
                stale[name] = set(health.SIGNALS)
            rows.append(
                dataset.make_row(
                    name, scenario["id"], model.time, readings[name],
                    room.sprinkler_on, truth,
                    stale_mask=health.mask_from_signals(stale.get(name)),
                )
            )
        if policy is not None:
            decisions = policy.step(model.time, readings, stale)
            for name, decision in decisions.items():
                model.rooms[name].sprinkler_on = decision["sprinkler"]
                model.rooms[name].damper = decision.get("damper", 0.0)
            # The agent's fire-door policy, as the door gateway applies it.
            model.set_doors(door_states(model, sub, scenario,
                                        policy.doors(model.time, decisions)))
    return scenario, rows


def door_states(model, topo, scenario, doors):
    """{pair: open} from the agent's door decisions: fire doors of closed
    levels and the doors of closed zones shut, the rest as in the scenario."""
    import door_gateway
    states = {}
    for pair in model.door_links:
        level = topology.split(pair[0])[0]
        closed = (doors["levels"].get(level) == "closed"
                  and door_gateway.is_fire_door(topo, pair)) or any(
            doors["zones"].get(z) == "closed" for z in pair)
        states[pair] = False if closed else scenarios.door_is_open(scenario, *pair)
    return states


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--scenarios", type=int, default=500)
    parser.add_argument("--out", default="recordings/offline")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--start-id", type=int, default=1)
    parser.add_argument("--dt", type=float,
                        default=float(os.environ.get("RECORD_INTERVAL", "2")) * common.TIME_SCALE,
                        help="simulated seconds between samples (default: live "
                             "RECORD_INTERVAL x TIME_SCALE, so data matches recordings)")
    parser.add_argument("--policy", choices=["mixed", "rules", "model", "none"],
                        default="mixed")
    parser.add_argument("--model", default=None, help="model path for --policy model")
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    parser.add_argument("--zones", type=int, default=12,
                        help="zones per simulated neighbourhood")
    default_data = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "data", "abuilding")
    parser.add_argument("--data-dir", default=os.environ.get("BUILDING_DATA_DIR") or (
        default_data if os.path.isdir(default_data) else None),
        help="building data folder (default: ../data/abuilding, else BuildSim)")
    args = parser.parse_args()

    topo = topology.Topology.load(common.BASE, args.data_dir)
    seeds_rooms = [k for k, z in topo.zones.items() if z.type == "room" and topo.neighbours[k]]
    seeds_corridors = [k for k, z in topo.zones.items()
                       if z.type == "corridor" and topo.neighbours[k]]
    print(f"building: {len(topo.zones)} zones; neighbourhoods of up to {args.zones}", flush=True)

    rng = random.Random(args.seed)
    jobs = []
    for i in range(args.scenarios):
        seed_zone = rng.choice(seeds_corridors if rng.random() < 0.15 else seeds_rooms)
        keys = topo.neighbourhood(seed_zone, args.zones, rng)
        sub = topo.subgraph(keys)
        rooms = [k for k in keys if sub.zones[k].type == "room" and sub.neighbours[k]]
        corridors = [k for k in keys if sub.zones[k].type == "corridor"]
        if not rooms:
            rooms = list(keys)
        scenario = scenarios.random_scenario(args.start_id + i, rooms, rng, corridors=corridors)
        policy = args.policy
        if policy == "mixed":
            policy = "rules" if rng.random() < 0.7 else "none"
        scenario["policy"] = policy
        scenario["source"] = "offline"
        scenario["zones"] = keys
        scenario["connections"] = [[a, b] for a, b, _, _ in sub.connections]
        jobs.append((scenario, rng.randrange(2**31), args.dt, policy, args.model, sub.to_json()))

    os.makedirs(args.out, exist_ok=True)
    started = time.time()
    total_rows = 0
    log_path = os.path.join(args.out, "scenarios.jsonl")

    # Fire-model log lines are noise here.
    fire_simulator.print = lambda *a, **k: None

    with open(log_path, "a", encoding="utf-8") as log, \
            multiprocessing.Pool(args.workers) as pool:
        for done, (scenario, rows) in enumerate(
                pool.imap_unordered(run_scenario, jobs), start=1):
            dataset.write_scenario_file(args.out, scenario["id"], rows)
            log.write(json.dumps(scenarios.to_json(scenario)) + "\n")
            total_rows += len(rows)
            if done % 50 == 0 or done == len(jobs):
                print(f"{done}/{len(jobs)} scenarios, {total_rows} rows, "
                      f"{time.time() - started:.0f}s", flush=True)

    print(f"wrote {args.out}", flush=True)


if __name__ == "__main__":
    main()
