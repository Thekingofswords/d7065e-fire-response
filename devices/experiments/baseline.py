"""Baseline comparison over many random scenarios: no system vs the rules
agent vs the trained model.

Each scenario is a random neighbourhood of the real building (as in
generate_dataset.py) with a random fire, nuisance source and/or sensor fault,
and every policy runs on exactly the same scenario.

    python experiments/baseline.py --scenarios 200 --out results/baseline
    python experiments/baseline.py --faults --scenarios 80    # sensor fault in the
                                                               # fire room before ignition

Metrics per policy:
    fires                    scenarios with a fire
    flashovers               fires that reached flashover (fully developed)
    release delay            sprinkler release after flaming starts (median, p90)
    spread                   zones that caught fire besides the origin
    smoke exposure           zone-minutes above 10 %/m (true obscuration)
    false releases           sprinkler released in a zone that never burned
    water                    sprinkler-minutes (water damage proxy)

Outputs: baseline.csv (one row per scenario and policy), summary.md,
baseline.png.
"""
import argparse
import multiprocessing
import os
import random
import statistics

import closed_loop as cl
import scenarios as scenario_lib

DT = 10.0


def make_jobs(topo, n, zones, seed, faults):
    rng = random.Random(seed)
    rooms_all = [k for k, z in topo.zones.items() if z.type == "room" and topo.neighbours[k]]
    jobs = []
    while len(jobs) < n:
        keys = topo.neighbourhood(rng.choice(rooms_all), zones, rng)
        sub = topo.subgraph(keys)
        rooms = [k for k in keys if sub.zones[k].type == "room" and sub.neighbours[k]]
        corridors = [k for k in keys if sub.zones[k].type == "corridor"]
        if not rooms:
            continue
        sc = scenario_lib.random_scenario(len(jobs) + 1, rooms, rng, corridors=corridors)
        if faults:
            if not sc["fires"]:
                continue
            fire = sc["fires"][0]
            sc["sensor_faults"] = [{"room": fire["room"],
                                    "type": rng.choice(["temp_stuck", "offline"]),
                                    "start": max(0.0, fire["start"] - 60.0)}]
        sc["duration"] = min(sc["duration"], 2400.0)
        jobs.append((sc, sub.to_json()))
    return jobs


def evaluate(job):
    sc, sub_json, policies, model_path = job
    import topology
    sub = topology.Topology.from_json(sub_json)
    rows = []
    for policy in policies:
        try:
            r = cl.run(sc, sub, policy, duration=sc["duration"], dt=DT, model_path=model_path)
        except RuntimeError:
            continue
        burned = {z for z, s in r.series.items() if any(p["burning"] for p in s)}
        origin = sc["fires"][0]["room"] if sc["fires"] else None
        flaming = cl.flaming_start(sc) if sc["fires"] else None
        release = r.release.get(origin) if origin else None
        rows.append({
            "scenario": sc["id"], "kind": sc["kind"], "policy": policy,
            "fire": int(bool(origin)),
            "fault": sc["sensor_faults"][0]["type"] if sc["sensor_faults"] else "",
            "flashover": int(bool(r.flashover)),
            "release_delay": "" if release is None or flaming is None else release - flaming,
            "detected": int(release is not None) if origin else "",
            "spread_zones": max(0, len(burned) - (1 if origin in burned else 0)),
            "smoke_zone_minutes": sum(1 for s in r.series.values() for p in s
                                      if p["true_smoke"] > 10) * DT / 60,
            "false_releases": len({z for z in r.release if z not in burned}),
            "water_minutes": sum(p["sprinkler"] for s in r.series.values() for p in s) * DT / 60,
        })
    return rows


def summarise(rows, policies):
    lines = ["| Policy | Fires | Detected | Flashovers | Release delay median / p90 (s) "
             "| Spread (zones) | Smoke (zone-min > 10 %/m) | False releases | Water (sprinkler-min) |",
             "|---|---|---|---|---|---|---|---|---|"]
    stats = {}
    for policy in policies:
        mine = [r for r in rows if r["policy"] == policy]
        if not mine:
            continue
        fires = [r for r in mine if r["fire"]]
        delays = sorted(r["release_delay"] for r in fires if r["release_delay"] != "")
        p90 = delays[int(0.9 * (len(delays) - 1))] if delays else float("nan")
        s = {
            "fires": len(fires),
            "detected": sum(1 for r in fires if r["detected"]),
            "flashovers": sum(r["flashover"] for r in fires),
            "delay_median": statistics.median(delays) if delays else float("nan"),
            "delay_p90": p90,
            "spread": sum(r["spread_zones"] for r in mine),
            "smoke": sum(r["smoke_zone_minutes"] for r in mine),
            "false": sum(r["false_releases"] for r in mine),
            "water": sum(r["water_minutes"] for r in mine),
        }
        stats[policy] = s
        lines.append(
            f"| {policy} | {s['fires']} | {s['detected']} | {s['flashovers']} "
            f"| {s['delay_median']:.0f} / {s['delay_p90']:.0f} | {s['spread']} "
            f"| {s['smoke']:.0f} | {s['false']} | {s['water']:.0f} |")
    return lines, stats


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--scenarios", type=int, default=200)
    parser.add_argument("--zones", type=int, default=12)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--policies", default="none,rules,model")
    parser.add_argument("--faults", action="store_true",
                        help="every scenario has a fire and a sensor fault in the fire room")
    parser.add_argument("--model", default=None)
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    parser.add_argument("--out", default="results/baseline")
    parser.add_argument("--data-dir")
    args = parser.parse_args()
    os.makedirs(args.out, exist_ok=True)

    topo = cl.load_topology(args.data_dir)
    policies = args.policies.split(",")
    jobs = [(sc, sub, policies, args.model)
            for sc, sub in make_jobs(topo, args.scenarios, args.zones, args.seed, args.faults)]
    rows = []
    with multiprocessing.Pool(args.workers) as pool:
        for i, part in enumerate(pool.imap_unordered(evaluate, jobs), start=1):
            rows.extend(part)
            if i % 25 == 0:
                print(f"{i}/{len(jobs)} scenarios", flush=True)
    rows.sort(key=lambda r: (r["scenario"], r["policy"]))
    cl.save_csv(os.path.join(args.out, "baseline.csv"), rows)
    lines, stats = summarise(rows, policies)
    title = ("Sensor fault in the fire room before ignition" if args.faults
             else f"{args.scenarios} random scenarios")
    with open(os.path.join(args.out, "summary.md"), "w", encoding="utf-8") as f:
        f.write(f"{title}\n\n" + "\n".join(lines) + "\n")
    print(title)
    print("\n".join(lines))

    plt = cl.pyplot()
    if plt and stats:
        names = list(stats)
        fig, axes = plt.subplots(1, 4, figsize=(13, 3.4))
        for ax, (key, label) in zip(axes, [("flashovers", "Flashovers"),
                                           ("smoke", "Smoke exposure (zone-min)"),
                                           ("false", "False sprinkler releases"),
                                           ("water", "Water (sprinkler-min)")]):
            values = [stats[n][key] for n in names]
            ax.bar(names, values, color=["#dc2626", "#2563eb", "#16a34a"][:len(names)])
            for x, v in enumerate(values):
                ax.text(x, v, f"{v:.0f}", ha="center", va="bottom", fontsize=8)
            ax.set_title(label, fontsize=10)
        fig.suptitle(title, fontsize=10)
        fig.tight_layout()
        fig.savefig(os.path.join(args.out, "baseline.png"), dpi=150)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
