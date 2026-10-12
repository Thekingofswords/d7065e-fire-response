"""Latency and recovery measured on a live run, from the database.

Run the stack (docker compose up -d), let a fire play out (fixed mode) or
several (SCENARIO_MODE=random), optionally kill a sensor gateway
(docker compose kill sensor-gateway-level1; docker compose start
sensor-gateway-level1), then:

    python experiments/latency.py --db postgresql://fire:fire@localhost:5433/fire \
        --since "1 hour" --out results/latency

Measures:
    detection        ignition / flaming -> sprinkler command, per fire
                     (ignition and flaming times from the recorded truth labels)
    actuation        command published -> state applied in BuildSim and
                     confirmed (commands joined with actuator_states on id)
    loop             sprinkler applied -> heat release falling in the room
    sensor outage    "offline" sensor-health event -> sensors OK again (the
                     downtime plus detection and recovery delays)
    data quality     rejected sensor messages by reason, rejected door commands
    evacuation       people out, people awaiting rescue, mean and last
                     evacuation time (occupants service)

All times are wall-clock seconds; multiply by TIME_SCALE for simulated time.
"""
import argparse
import collections
import math
import os
import statistics

import closed_loop as cl


def pct(values, q):
    """Nearest-rank percentile."""
    values = sorted(values)
    if not values:
        return float("nan")
    return values[min(len(values) - 1, max(0, math.ceil(q * len(values)) - 1))]


def describe(values):
    if not values:
        return "no data"
    return (f"n={len(values)}, median {statistics.median(values):.2f} s, "
            f"p90 {pct(values, 0.9):.2f} s, max {max(values):.2f} s")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=os.environ.get(
        "DATABASE_URL", "postgresql://fire:fire@localhost:5433/fire"))
    parser.add_argument("--since", default="24 hours", help="PostgreSQL interval")
    parser.add_argument("--time-scale", type=float, default=float(os.environ.get("TIME_SCALE", "5")))
    parser.add_argument("--out", default="results/latency")
    args = parser.parse_args()
    os.makedirs(args.out, exist_ok=True)

    import psycopg
    conn = psycopg.connect(args.db)
    since = f"now() - interval '{args.since}'"

    # -- detection: per origin zone, ignition / flaming -> first sprinkler on --
    fires = conn.execute(f"""
        SELECT scenario_id, level, room,
               min(time) FILTER (WHERE fire = 1)        AS ignition,
               min(time) FILTER (WHERE fire_phase >= 2) AS flaming
        FROM samples
        WHERE time > {since} AND fire_origin = 1
        GROUP BY scenario_id, level, room""").fetchall()
    detection = []
    for scenario_id, level, room, ignition, flaming in fires:
        row = conn.execute("""
            SELECT min(time) FROM commands
            WHERE level = %s AND room = %s AND actuator = 'sprinkler' AND state = 'on'
              AND time >= %s""", (level, room, ignition)).fetchone()
        command = row[0] if row else None
        detection.append({
            "scenario_id": scenario_id, "zone": f"{level}/{room}",
            "ignition": ignition, "flaming": flaming, "sprinkler_command": command,
            "after_ignition_s": (command - ignition).total_seconds() if command else "",
            "after_flaming_s": (command - flaming).total_seconds() if command and flaming else "",
        })
    cl.save_csv(os.path.join(args.out, "detection.csv"), detection)

    # -- actuation: command -> applied/confirmed ---------------------------------
    actuation = conn.execute(f"""
        SELECT c.actuator, c.state, extract(epoch FROM (s.time - c.time))
        FROM commands c JOIN actuator_states s ON s.command_id = c.command_id
        WHERE c.time > {since} AND c.command_id IS NOT NULL""").fetchall()
    by_actuator = collections.defaultdict(list)
    for actuator, _, seconds in actuation:
        by_actuator[actuator].append(float(seconds))
    cl.save_csv(os.path.join(args.out, "actuation.csv"),
                [{"actuator": a, "state": s, "latency_s": float(t)} for a, s, t in actuation])

    # -- loop: sprinkler applied -> heat release in the room falling ---------------
    loop = []
    for level, room, applied in conn.execute(f"""
            SELECT level, room, time FROM actuator_states
            WHERE time > {since} AND actuator = 'sprinkler' AND state = 'on'""").fetchall():
        rows = conn.execute("""
            SELECT time, true_fire_hrr FROM samples
            WHERE level = %s AND room = %s AND time BETWEEN %s AND %s + interval '5 minutes'
            ORDER BY time""", (level, room, applied, applied)).fetchall()
        hrr = [(t, h) for t, h in rows if h is not None]
        if len(hrr) < 3:
            continue
        peak_time, peak = max(hrr, key=lambda x: x[1])
        falling = next((t for t, h in hrr if t > peak_time and h < 0.5 * peak), None)
        if peak > 1 and falling:
            loop.append((falling - applied).total_seconds())

    # -- sensor recovery: offline -> OK ---------------------------------------
    recovery = []
    open_outage = {}
    for time_, level, room, message in conn.execute(f"""
            SELECT time, level, room, message FROM events
            WHERE time > {since} AND type = 'sensor_health' ORDER BY time""").fetchall():
        zone = f"{level}/{room}"
        if "offline" in (message or ""):
            open_outage.setdefault(zone, time_)
        elif zone in open_outage and "OK" in (message or ""):
            recovery.append((time_ - open_outage.pop(zone)).total_seconds())

    evacuations = [data or {} for (data,) in conn.execute(f"""
            SELECT data FROM events WHERE time > {since} AND type = 'evacuation_complete'
            ORDER BY time""").fetchall()]
    door_rejections = conn.execute(f"""
            SELECT count(*) FROM events
            WHERE time > {since} AND type = 'door_command_rejected'""").fetchone()[0]

    quality = collections.Counter()
    for (data,) in conn.execute(f"""
            SELECT data FROM events WHERE time > {since} AND type = 'data_quality'""").fetchall():
        for reason, n in (data or {}).get("rejected", {}).items():
            quality[reason] += n

    after_flaming = [d["after_flaming_s"] for d in detection if d["after_flaming_s"] != ""]
    after_ignition = [d["after_ignition_s"] for d in detection if d["after_ignition_s"] != ""]
    lines = [
        f"Window: last {args.since}; time scale x{args.time_scale:g} "
        f"(simulated = wall x {args.time_scale:g})", "",
        "| Measurement | Wall-clock |", "|---|---|",
        f"| Fires recorded | {len(detection)} ({sum(1 for d in detection if d['sprinkler_command'])} "
        f"with a sprinkler command) |",
        f"| Ignition -> sprinkler command | {describe(after_ignition)} |",
        f"| Flaming -> sprinkler command | {describe(after_flaming)} |",
    ]
    for actuator, values in sorted(by_actuator.items()):
        lines.append(f"| Command -> {actuator} accepted by BuildSim and confirmed "
                     f"| {describe(values)} |")
    lines += [
        f"| Sprinkler applied -> heat release halved | {describe(loop)} |",
        f"| Sensor outage seen by the agent, offline -> OK (includes the downtime) | {describe(recovery)} |",
        f"| Rejected sensor messages | {dict(quality) or 'none'} |",
        f"| Rejected door commands | {door_rejections} |",
    ]
    for e in evacuations:
        lines.append(
            f"| Evacuation | {e.get('evacuated')} out, {e.get('waiting')} awaiting rescue, "
            f"mean {e.get('mean_s') or 0:.0f} s, last out after {e.get('last_out_s') or 0:.0f} s "
            f"(simulated) |")
    with open(os.path.join(args.out, "summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))

    plt = cl.pyplot()
    if plt and (actuation or after_flaming):
        fig, axes = plt.subplots(1, 2, figsize=(10, 3.5))
        if after_flaming:
            axes[0].hist([v * args.time_scale for v in after_flaming], bins=20, color="#2563eb")
            axes[0].set_title("Flaming -> sprinkler command (simulated s)", fontsize=10)
        for actuator, values in sorted(by_actuator.items()):
            axes[1].hist([v * 1000 for v in values], bins=30, alpha=0.6, label=actuator)
        axes[1].set_title("Command -> applied (wall-clock ms)", fontsize=10)
        axes[1].legend(fontsize=8)
        fig.tight_layout()
        fig.savefig(os.path.join(args.out, "latency.png"), dpi=150)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
