"""Scaling experiment: where does the system break as the building grows?

Measures, for buildings of increasing size (the real 956-zone building
copied side by side, up to tens of thousands of zones):

    agent      time for one BuildingPolicy.step over all zones (rules and
               model detectors), against the decision interval (2 s)
    physics    time for the fire model to advance one 2 s tick with a fire
               burning, against the simulator tick (2 s real)
    bridge     HTTP requests per second the BuildSim bridge would need to
               mirror every reading without deadbands, against its cap

The breaking point is the first size at which a component no longer fits
its budget.

    python experiments/scaling.py --out results/scaling
"""
import argparse
import os
import random
import statistics
import time

import closed_loop as cl
import building_agent
import topology

DECISION_INTERVAL_S = float(os.environ.get("DECISION_INTERVAL", "2"))
SIM_TICK_S = float(os.environ.get("FIRE_TICK_SECONDS", "2"))
TIME_SCALE = float(os.environ.get("TIME_SCALE", "5"))
BRIDGE_CAP_RPS = float(os.environ.get("MAX_REQUESTS_PER_SECOND", "150"))


def replicate(topo, copies):
    """The building `copies` times over, as one big topology (copies are
    linked through one corridor each so it stays one graph)."""
    zones, conns, stairs, cells, cell_links, conn_cells = {}, [], [], {}, [], {}
    for c in range(copies):
        def k(key):
            level, name = topology.split(key)
            return f"{level}/{name}" if c == 0 else f"{level}/{name}~{c}"
        for key, z in topo.zones.items():
            level, name = topology.split(k(key))
            zones[k(key)] = topology.Zone(level, name, z.type, z.area, z.height, z.center)
        conns += [(k(a), k(b), w, h) for a, b, w, h in topo.connections]
        stairs += [(k(a), k(b)) for a, b in topo.stairs]
        cells.update({k(key): v for key, v in topo.cells.items()})
        cell_links += [(k(z), i, j, w, h) for z, i, j, w, h in topo.cell_links]
        conn_cells.update({(k(a), k(b)): v for (a, b), v in topo.conn_cells.items()})
    return topology.Topology(zones, conns, stairs, cells, cell_links, conn_cells)


def time_agent(big, detector_kind, ticks, model_path):
    zones = big.sensor_zones()
    detector = building_agent.make_detector(detector_kind, model_path)
    if detector.name != detector_kind:
        return None
    policy = building_agent.BuildingPolicy(zones, big.neighbour_map(zones), detector)
    rng = random.Random(1)
    hot = set(rng.sample(zones, max(1, len(zones) // 100)))
    times = []
    for t in range(ticks):
        readings = {z: {"temperature": 21 + rng.random() * 0.2 + (3 * t if z in hot else 0),
                        "smoke": rng.random() * 0.1 + (2 * t if z in hot else 0),
                        "co": rng.random(), "co2": 420 + rng.random() * 10} for z in zones}
        start = time.perf_counter()
        policy.step(t * DECISION_INTERVAL_S * TIME_SCALE, readings)
        times.append(time.perf_counter() - start)
    return statistics.median(times[2:]), max(times[2:])


def time_physics(big, ticks):
    import fire_simulator
    sc = {"id": 1, "kind": "x", "duration": float("inf"), "alpha": 0.0469, "growth": "fast",
          "fuel_load": 420.0, "external_doors": 0, "ach": 2.0, "door_open_prob": 0.5,
          "door_seed": 1, "open_doors": [],
          "fires": [{"room": "level0/A109", "start": 0.0, "incipient": 0.0}],
          "nuisances": [], "sensor_faults": []}
    model = fire_simulator.FireModel(sc, big)
    model.step(600.0)   # let the smoke spread first
    times = []
    for _ in range(ticks):
        start = time.perf_counter()
        model.step(SIM_TICK_S * TIME_SCALE)
        times.append(time.perf_counter() - start)
    return statistics.median(times), len(model.active)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--copies", default="1,2,4,8,16")
    parser.add_argument("--ticks", type=int, default=8)
    parser.add_argument("--detectors", default="rules,model")
    parser.add_argument("--model", default=None)
    parser.add_argument("--out", default="results/scaling")
    parser.add_argument("--data-dir")
    args = parser.parse_args()
    os.makedirs(args.out, exist_ok=True)
    topo = cl.load_topology(args.data_dir)

    rows = []
    for copies in [int(c) for c in args.copies.split(",")]:
        big = replicate(topo, copies)
        n = len(big.sensor_zones())
        row = {"zones": n}
        for kind in args.detectors.split(","):
            result = time_agent(big, kind, args.ticks, args.model)
            if result:
                row[f"agent_{kind}_median_s"] = round(result[0], 4)
                row[f"agent_{kind}_max_s"] = round(result[1], 4)
        physics, active = time_physics(big, max(3, args.ticks // 2))
        row["physics_tick_s"] = round(physics, 4)
        row["physics_active_cells"] = active
        # Every reading changes every tick without a deadband: 4 per zone.
        row["bridge_rps_no_deadband"] = round(4 * n / 2.0)
        rows.append(row)
        print(row, flush=True)
    cl.save_csv(os.path.join(args.out, "scaling.csv"), rows)

    lines = ["| Zones | Agent step, rules (s) | Agent step, model (s) | Physics tick (s) "
             "| Bridge req/s without deadband |", "|---|---|---|---|---|"]
    breaks = {}
    for r in rows:
        lines.append(f"| {r['zones']} | {r.get('agent_rules_median_s', '–')} "
                     f"| {r.get('agent_model_median_s', '–')} | {r['physics_tick_s']} "
                     f"| {r['bridge_rps_no_deadband']} |")
        for key, budget in (("agent_rules_median_s", DECISION_INTERVAL_S),
                            ("agent_model_median_s", DECISION_INTERVAL_S),
                            ("physics_tick_s", SIM_TICK_S),
                            ("bridge_rps_no_deadband", BRIDGE_CAP_RPS)):
            if key in r and r[key] > budget and key not in breaks:
                breaks[key] = r["zones"]
    lines.append("")
    lines.append(f"Budgets: decision interval {DECISION_INTERVAL_S} s, simulator tick "
                 f"{SIM_TICK_S} s, bridge {BRIDGE_CAP_RPS:.0f} requests/s.")
    for key, zones in breaks.items():
        lines.append(f"* {key} exceeds its budget at {zones} zones")
    if not breaks:
        lines.append("* nothing exceeded its budget in the sizes tested")
    with open(os.path.join(args.out, "summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))

    plt = cl.pyplot()
    if plt:
        fig, ax = plt.subplots(figsize=(7, 4))
        x = [r["zones"] for r in rows]
        for key, label, colour in (("agent_rules_median_s", "agent step (rules)", "#2563eb"),
                                   ("agent_model_median_s", "agent step (model)", "#16a34a"),
                                   ("physics_tick_s", "physics tick", "#dc2626")):
            if all(key in r for r in rows):
                ax.plot(x, [r[key] for r in rows], marker="o", label=label, color=colour)
        ax.axhline(DECISION_INTERVAL_S, color="#555", ls="--", lw=1,
                   label=f"budget ({DECISION_INTERVAL_S:.0f} s)")
        ax.set_xlabel("Zones")
        ax.set_ylabel("Seconds per tick")
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.grid(alpha=0.3, which="both")
        ax.legend(fontsize=8)
        ax.set_title("Scaling: compute per tick vs building size", fontsize=10)
        fig.tight_layout()
        fig.savefig(os.path.join(args.out, "scaling.png"), dpi=150)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
