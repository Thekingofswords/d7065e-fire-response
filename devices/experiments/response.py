"""Response over time: one fire, with and without the system.

The fixed demo scenario (fast fire in A109 after 120 s of smouldering, half
of the doors open) on the whole building, run three ways: no system
(baseline), the rules agent, and the trained model. For a few zones along
the smoke's path it records the true gas temperature, the true smoke
obscuration, what the detector reported, and the sprinkler and damper.

    python experiments/response.py --out results/response

Outputs (in --out):
    response.csv              every recorded tick, all runs
    response_<zone>.csv       pgfplots-friendly: time + one column per run
    response.png              the figure for the report
    summary.md                release time, flashover, peak smoke, smoke-free time
"""
import argparse
import os

import closed_loop as cl

ZONES = {
    "level0/A109": "fire room A109",
    "level0/A103+A112_B+A103": "corridor outside",
    "level0/A110": "room across the corridor",
    "level1/A204a": "level 1, top of the stairs",
}


def scenario(fire_room, door_open_prob):
    return {
        "id": 1, "kind": "experiment", "duration": float("inf"),
        "alpha": 0.0469, "growth": "fast", "fuel_load": 420.0, "external_doors": 0, "ach": 2.0,
        "door_open_prob": door_open_prob, "door_seed": 1, "open_doors": [fire_room],
        "fires": [{"room": fire_room, "start": 0.0, "incipient": 120.0}],
        "nuisances": [], "sensor_faults": [],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", default="results/response")
    parser.add_argument("--duration", type=float, default=2400.0)
    parser.add_argument("--policies", default="none,rules_no_doors,rules,model",
                        help="none, rules, model; *_no_doors without the fire-door policy")
    parser.add_argument("--door-open-prob", type=float, default=0.5)
    parser.add_argument("--data-dir")
    args = parser.parse_args()
    os.makedirs(args.out, exist_ok=True)

    topo = cl.load_topology(args.data_dir)
    sc = scenario("level0/A109", args.door_open_prob)
    runs = {}
    for policy in args.policies.split(","):
        try:
            runs[policy] = cl.run(sc, topo, policy, duration=args.duration, record=ZONES)
            print(f"{policy}: done")
        except RuntimeError as exc:
            print(f"{policy}: skipped ({exc})")

    rows = []
    for policy, r in runs.items():
        for zone in ZONES:
            for point in r.series[zone]:
                rows.append(dict(point, policy=policy, zone=zone))
    cl.save_csv(os.path.join(args.out, "response.csv"), rows,
                ["policy", "zone", "time", "true_temperature", "true_smoke", "temperature",
                 "smoke", "co", "hrr", "sprinkler", "damper", "phase"])
    for zone in ZONES:
        table = []
        for i, point in enumerate(next(iter(runs.values())).series[zone]):
            row = {"time": point["time"]}
            for policy, r in runs.items():
                p = r.series[zone][i]
                row[f"{policy}_temperature"] = round(p["true_temperature"], 1)
                row[f"{policy}_smoke"] = round(p["true_smoke"], 2)
            table.append(row)
        cl.save_csv(os.path.join(args.out, f"response_{zone.replace('/', '_')}.csv"), table)

    flaming = cl.flaming_start(sc)
    lines = ["| Run | Sprinkler released (s after flaming) | Flashover | Peak fire-room temp (°C) "
             "| Peak smoke, corridor (%/m) | Zones above 10 %/m | Smoke below 1 %/m everywhere from |",
             "|---|---|---|---|---|---|---|"]
    for policy, r in runs.items():
        release = r.release.get("level0/A109")
        peak_t = max(p["true_temperature"] for p in r.series["level0/A109"])
        peak_s = max(p["true_smoke"] for p in r.series["level0/A103+A112_B+A103"])
        model = r.model
        smoky = sum(1 for z in r.zones if z in r.series and
                    max(p["true_smoke"] for p in r.series[z]) > 10)
        clear_from = None
        for i, point in enumerate(r.series["level0/A109"]):
            if all(r.series[z][i]["true_smoke"] < 1.0 for z in ZONES) and point["time"] > 300:
                clear_from = clear_from or point["time"]
            else:
                clear_from = None
        lines.append(
            f"| {policy} | {release - flaming:.0f}" if release else f"| {policy} | never")
        lines[-1] += (f" | {'yes' if 'level0/A109' in r.flashover else 'no'} | {peak_t:.0f} "
                      f"| {peak_s:.1f} | {smoky} of {len(ZONES)} tracked "
                      f"| {f'{clear_from:.0f} s' if clear_from else 'not within the run'} |")
        del model
    with open(os.path.join(args.out, "summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))

    plt = cl.pyplot()
    if plt:
        fig, axes = plt.subplots(2, 2, figsize=(11, 7), sharex=True)
        styles = {"none": ("#dc2626", "no system"),
                  "rules_no_doors": ("#f59e0b", "rules agent, doors left as they are"),
                  "rules": ("#2563eb", "rules agent + fire doors"),
                  "model": ("#16a34a", "model agent + fire doors")}
        panels = [("level0/A109", "true_temperature", "Gas temperature, fire room (°C)"),
                  ("level0/A103+A112_B+A103", "true_smoke", "Smoke, corridor (%/m)"),
                  ("level0/A110", "true_smoke", "Smoke, room across the corridor (%/m)"),
                  ("level1/A204a", "true_smoke", "Smoke, level 1 at the stairs (%/m)")]
        for ax, (zone, field, title) in zip(axes.flat, panels):
            for policy, r in runs.items():
                colour, label = styles.get(policy, ("#555", policy))
                t = [p["time"] / 60 for p in r.series[zone]]
                ax.plot(t, [p[field] for p in r.series[zone]], color=colour, label=label)
                if policy != "none" and zone == "level0/A109" and r.release.get(zone):
                    ax.axvline(r.release[zone] / 60, color=colour, ls=":", lw=1)
            ax.set_title(title, fontsize=10)
            ax.grid(alpha=0.3)
        axes[0][0].axvline(flaming / 60, color="#888", ls="--", lw=1)
        axes[0][0].legend(fontsize=8)
        for ax in axes[1]:
            ax.set_xlabel("Simulated minutes")
        fig.suptitle("Fast fire in A109: response with and without the system "
                     "(dotted: sprinkler released, dashed: flaming starts)", fontsize=10)
        fig.tight_layout()
        fig.savefig(os.path.join(args.out, "response.png"), dpi=150)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
