"""Train a LightGBM fire detector on recorded / generated data.

    python train_model.py recordings/offline recordings/live --out models/fire_lgbm.txt

Steps
1. Load scenario CSVs (dataset.py schema) and replay every scenario through
   features.FeatureExtractor, the same code the live agent uses.
2. Split by *scenario* (70 % train, 15 % validation, 15 % test), never by
   row: neighbouring rows of one fire are nearly identical and would leak.
3. Train a binary classifier: is this room burning right now?
4. Pick the decision threshold on the validation scenarios: the lowest
   threshold whose false-alarm rate stays within --max-false-alarms.
5. Report event metrics on the held-out test scenarios, for the model and
   for the fixed-limit rules on the same data:
     * detection rate and delay after ignition / after flaming starts
     * false alarms per 100 room-hours, broken down by cause (nuisance
       source, sensor fault, smoke from a fire next door)
6. Save the model plus a .json with the feature list, thresholds and
   metrics. building_agent.py loads both with DETECTOR=model.

Requires: pip install lightgbm numpy
"""
import argparse
import collections
import json
import multiprocessing
import os
import statistics
import time

import numpy as np

import building_agent
import common
import dataset
import features as feature_lib
import health

NUISANCE_NAMES = {0: "none", 1: "cooking", 2: "steam", 3: "dust", 4: "heater", 5: "smoking"}
FAULT_NAMES = {0: "none", 1: "smoke drift", 2: "temperature stuck", 3: "smoke spikes",
               4: "CO dead", 5: "sensor offline"}


# ---------------------------------------------------------------------------
# Feature building
# ---------------------------------------------------------------------------

SCENARIO_NEIGHBOURS = {}   # scenario id -> {zone: [zones]} (offline neighbourhoods)
BUILDING_NEIGHBOURS = {}   # zone -> [zones] for the whole building (live data)
CORRIDORS = set()          # corridor zone keys of the building


def load_neighbours(paths, data_dir=None):
    """Neighbour maps: per scenario from scenarios.jsonl where the generator
    stored its neighbourhood, otherwise the whole building's topology."""
    import glob

    for path in paths:
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        for log in glob.glob(os.path.join(folder, "scenarios.jsonl")):
            with open(log, encoding="utf-8") as f:
                for line in f:
                    try:
                        s = json.loads(line)
                    except ValueError:
                        continue
                    if s.get("connections") is not None and "zones" in s:
                        nb = {z: [] for z in s["zones"]}
                        for a, b in s["connections"]:
                            nb.setdefault(a, []).append(b)
                            nb.setdefault(b, []).append(a)
                        SCENARIO_NEIGHBOURS[int(s["id"])] = nb
    try:
        import topology
        topo = topology.Topology.load(common.BASE, data_dir)
        BUILDING_NEIGHBOURS.update(topo.neighbours)
        CORRIDORS.update(k for k, z in topo.zones.items() if z.type == "corridor")
    except Exception as exc:  # noqa: BLE001
        print(f"note: building topology unavailable ({exc}); live recordings "
              f"will have no neighbour features", flush=True)


def neighbour_map_for(scenario_id, zones):
    nb = SCENARIO_NEIGHBOURS.get(scenario_id) or BUILDING_NEIGHBOURS
    zone_set = set(zones)
    return {z: [n for n in nb.get(z, []) if n in zone_set] for z in zones}


def scenario_features(job):
    """Replay one scenario -> (X rows, metadata rows)."""
    rows, settle, scenario_nb, building_nb, corridors = job
    SCENARIO_NEIGHBOURS.update(scenario_nb)
    BUILDING_NEIGHBOURS.update(building_nb)
    for r in rows:
        r["zone"] = f"{r['level']}/{r['room']}"
    rooms = sorted({r["zone"] for r in rows})
    neighbours = neighbour_map_for(int(rows[0]["scenario_id"]), rooms)
    extractor = feature_lib.FeatureExtractor(rooms, neighbours, corridors)
    sensor_health = health.SensorHealth(rooms)
    rules = building_agent.RulesDetector()

    by_time = collections.defaultdict(dict)
    for r in rows:
        by_time[r["scenario_time"]][r["zone"]] = r

    X, meta = [], []
    for t in sorted(by_time):
        tick = by_time[t]
        readings = {room: {s: r[s] for s in feature_lib.SIGNALS} for room, r in tick.items()}
        stale = {room: health.signals_from_mask(r.get("stale_signals", 0))
                 for room, r in tick.items()}
        faults = sensor_health.update(t, readings, stale)
        room_features = extractor.update(t, readings, faults)
        rule_view = rules.assess(room_features, neighbours, faults)
        if t < settle:
            continue
        for room, f in room_features.items():
            r = tick[room]
            X.append(feature_lib.vector(f))
            meta.append((
                int(r["scenario_id"]), t, room, int(r["fire"]), int(r["fire_phase"]),
                int(r["nuisance_source"]), int(r["sensor_fault"]),
                rule_view[room]["p_fire"] >= 0.5, bool(rule_view[room]["alarm"]),
            ))
    return np.asarray(X, dtype=np.float32).reshape(-1, len(feature_lib.FEATURE_NAMES)), meta


def build(rows, settle, workers):
    scenarios = collections.defaultdict(list)
    for r in rows:
        scenarios[int(r["scenario_id"])].append(r)
    jobs = [(scenario_rows, settle, {sid: SCENARIO_NEIGHBOURS[sid]} if sid in SCENARIO_NEIGHBOURS
             else {}, BUILDING_NEIGHBOURS if sid not in SCENARIO_NEIGHBOURS else {},
             CORRIDORS)
            for sid, scenario_rows in sorted(scenarios.items())]
    with multiprocessing.Pool(workers) as pool:
        parts = pool.map(scenario_features, jobs, chunksize=4)
    X = np.concatenate([p[0] for p in parts]) if parts else np.zeros((0, 0))
    meta = [m for p in parts for m in p[1]]
    return X, meta


def split_of(scenario_id):
    """Deterministic 70/15/15 split by scenario id."""
    bucket = (scenario_id * 2654435761) % (2 ** 32) % 100
    return "train" if bucket < 70 else "valid" if bucket < 85 else "test"


# ---------------------------------------------------------------------------
# Event metrics
# ---------------------------------------------------------------------------

def debounce(flags, ticks):
    out, count = [], 0
    for flag in flags:
        count = count + 1 if flag else 0
        out.append(count >= ticks)
    return out


def evaluate(meta, decisions, confirm_ticks):
    """Event-level metrics for per-row boolean decisions."""
    groups = collections.defaultdict(list)
    for m, d in zip(meta, decisions):
        groups[(m[0], m[2])].append((m, d))

    scenario_has_fire = collections.defaultdict(bool)
    # First time each nuisance source appeared anywhere in the scenario.
    scenario_nuisance = collections.defaultdict(dict)
    for (sid, _), items in groups.items():
        if any(m[3] for m, _ in items):
            scenario_has_fire[sid] = True
        for m, _ in items:
            if m[5]:
                first = scenario_nuisance[sid].get(m[5])
                if first is None or m[1] < first:
                    scenario_nuisance[sid][m[5]] = m[1]

    def nuisance_elsewhere(sid, t):
        seen = [(first, code) for code, first in scenario_nuisance[sid].items() if first <= t]
        return max(seen)[1] if seen else 0

    delays_ignition, delays_flaming = [], []
    fires = detected = 0
    false_alarms = collections.Counter()
    quiet_seconds = 0.0

    for (sid, room), items in groups.items():
        items.sort(key=lambda x: x[0][1])
        times = [m[1] for m, _ in items]
        dt = statistics.median(np.diff(times)) if len(times) > 1 else 10.0
        alarm = debounce([d for _, d in items], confirm_ticks)
        burning = [m[3] for m, _ in items]
        t_ignite = next((m[1] for m, _ in items if m[3]), None)
        t_flame = next((m[1] for m, _ in items if m[3] and m[4] >= 2), None)

        previous = False
        last_nuisance = 0
        for (m, _), a in zip(items, alarm):
            before_fire = t_ignite is None or m[1] < t_ignite
            if m[5]:
                last_nuisance = m[5]
            if before_fire:
                quiet_seconds += dt
                if a and not previous:
                    if m[5]:
                        cause = f"nuisance: {NUISANCE_NAMES[m[5]]}"
                    elif last_nuisance:
                        cause = f"nuisance: {NUISANCE_NAMES[last_nuisance]} (lingering smoke)"
                    elif m[6]:
                        cause = f"fault: {FAULT_NAMES[m[6]]}"
                    elif nuisance_elsewhere(sid, m[1]):
                        cause = (f"nuisance in neighbouring room: "
                                 f"{NUISANCE_NAMES[nuisance_elsewhere(sid, m[1])]}")
                    elif scenario_has_fire[sid]:
                        cause = "fire in neighbouring room"
                    else:
                        cause = "no cause (noise)"
                    false_alarms[cause] += 1
            previous = a

        if t_ignite is not None:
            fires += 1
            t_detect = next(
                (m[1] for (m, _), a, b in zip(items, alarm, burning) if a and b), None
            )
            if t_detect is not None:
                detected += 1
                delays_ignition.append(t_detect - t_ignite)
                if t_flame is not None:
                    delays_flaming.append(t_detect - t_flame)

    def pct(values, q):
        return float(np.percentile(values, q)) if values else float("nan")

    room_hours = quiet_seconds / 3600.0
    total_fa = sum(false_alarms.values())
    return {
        "fires": fires,
        "detected": detected,
        "detection_rate": detected / fires if fires else float("nan"),
        "delay_after_ignition_median_s": pct(delays_ignition, 50),
        "delay_after_ignition_p90_s": pct(delays_ignition, 90),
        "delay_after_flaming_median_s": pct(delays_flaming, 50),
        "delay_after_flaming_p90_s": pct(delays_flaming, 90),
        "false_alarms": total_fa,
        "quiet_room_hours": room_hours,
        "false_alarms_per_100_room_hours": 100 * total_fa / room_hours if room_hours else float("nan"),
        "false_alarm_causes": dict(false_alarms.most_common()),
    }


def print_metrics(name, m):
    print(f"\n{name}")
    print(f"  fires detected        {m['detected']}/{m['fires']} ({100 * m['detection_rate']:.0f} %)")
    print(f"  delay after ignition  median {m['delay_after_ignition_median_s']:.0f} s, "
          f"p90 {m['delay_after_ignition_p90_s']:.0f} s")
    print(f"  delay after flaming   median {m['delay_after_flaming_median_s']:.0f} s, "
          f"p90 {m['delay_after_flaming_p90_s']:.0f} s")
    print(f"  false alarms          {m['false_alarms']} in {m['quiet_room_hours']:.0f} room-hours "
          f"({m['false_alarms_per_100_room_hours']:.2f} per 100 room-hours)")
    for cause, count in m["false_alarm_causes"].items():
        print(f"      {count:4d}  {cause}")


def auc(y, p):
    order = np.argsort(p)
    ranks = np.empty(len(p))
    ranks[order] = np.arange(1, len(p) + 1)
    positives = y.sum()
    negatives = len(y) - positives
    if positives == 0 or negatives == 0:
        return float("nan")
    return float((ranks[y == 1].sum() - positives * (positives + 1) / 2) / (positives * negatives))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    import lightgbm as lgb

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("data", nargs="+", help="CSV files or directories")
    parser.add_argument("--out", default="models/fire_lgbm.txt")
    parser.add_argument("--settle", type=float, default=300.0,
                        help="skip the first N simulated seconds of each scenario")
    parser.add_argument("--max-false-alarms", type=float, default=1.0,
                        help="false alarms per 100 quiet room-hours allowed on validation")
    parser.add_argument("--confirm-ticks", type=int, default=building_agent.CONFIRM_TICKS)
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    default_data = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "abuilding")
    parser.add_argument("--data-dir", default=os.environ.get("BUILDING_DATA_DIR") or (
        default_data if os.path.isdir(default_data) else None),
        help="building data folder for live recordings' neighbours (default ../data/abuilding)")
    args = parser.parse_args()

    started = time.time()
    rows = dataset.load(args.data)
    load_neighbours(args.data, args.data_dir)
    if not rows:
        raise SystemExit("no data found")
    print(f"loaded {len(rows)} rows from {len({r['scenario_id'] for r in rows})} scenarios")

    X, meta = build(rows, args.settle, args.workers)
    y = np.array([m[3] for m in meta], dtype=np.int8)
    split = np.array([split_of(m[0]) for m in meta])
    print(f"{X.shape[0]} samples x {X.shape[1]} features, {y.mean() * 100:.1f} % burning, "
          f"built in {time.time() - started:.0f}s")

    masks = {name: split == name for name in ("train", "valid", "test")}
    for name, mask in masks.items():
        print(f"  {name:5s} {mask.sum():7d} samples, "
              f"{len({m[0] for m, k in zip(meta, mask) if k})} scenarios")

    params = {
        "objective": "binary",
        "learning_rate": 0.05,
        "num_leaves": 31,
        "min_data_in_leaf": 50,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 1,
        "lambda_l2": 1.0,
        "metric": ["binary_logloss", "auc"],
        "verbose": -1,
    }
    train = lgb.Dataset(X[masks["train"]], y[masks["train"]],
                        feature_name=feature_lib.FEATURE_NAMES)
    valid = lgb.Dataset(X[masks["valid"]], y[masks["valid"]], reference=train)
    booster = lgb.train(
        params, train, num_boost_round=2000, valid_sets=[valid],
        callbacks=[lgb.early_stopping(50, verbose=False), lgb.log_evaluation(0)],
    )
    print(f"trained {booster.best_iteration} trees")

    p_all = booster.predict(X, num_iteration=booster.best_iteration)
    meta_valid = [m for m, k in zip(meta, masks["valid"]) if k]
    meta_test = [m for m, k in zip(meta, masks["test"]) if k]
    p_valid, p_test = p_all[masks["valid"]], p_all[masks["test"]]
    print(f"row-level AUC: valid {auc(y[masks['valid']], p_valid):.4f}, "
          f"test {auc(y[masks['test']], p_test):.4f}")

    # Threshold: the most sensitive one within the false-alarm budget.
    chosen = None
    for threshold in np.round(np.arange(0.05, 0.96, 0.05), 2):
        m = evaluate(meta_valid, p_valid >= threshold, args.confirm_ticks)
        if m["false_alarms_per_100_room_hours"] <= args.max_false_alarms:
            chosen = (float(threshold), m)
            break
    if chosen is None:
        chosen = (0.95, evaluate(meta_valid, p_valid >= 0.95, args.confirm_ticks))
        print("warning: no threshold met the false-alarm budget; using 0.95")
    threshold = chosen[0]
    alarm_threshold = round(threshold / 2, 3)
    print(f"release threshold p >= {threshold} (alarm p >= {alarm_threshold})")

    results = {
        "model": evaluate(meta_test, p_test >= threshold, args.confirm_ticks),
        "model_alarm": evaluate(meta_test, p_test >= alarm_threshold, args.confirm_ticks),
        "rules_release": evaluate(meta_test, [m[7] for m in meta_test], args.confirm_ticks),
        "rules_alarm": evaluate(meta_test, [m[8] for m in meta_test], args.confirm_ticks),
    }
    print("\n=== Held-out test scenarios ===")
    print_metrics(f"Model, sprinkler release (p >= {threshold})", results["model"])
    print_metrics("Rules, sprinkler release (68 °C / 8.3 °C/min)", results["rules_release"])
    print_metrics(f"Model, alarm (p >= {alarm_threshold})", results["model_alarm"])
    print_metrics("Rules, alarm (smoke 8 %/m / CO 50 ppm / heat)", results["rules_alarm"])

    gains = booster.feature_importance("gain")
    top = sorted(zip(feature_lib.FEATURE_NAMES, gains), key=lambda x: -x[1])[:15]
    total = sum(gains) or 1.0
    print("\nmost important features:")
    for name, gain in top:
        print(f"  {100 * gain / total:5.1f} %  {name}")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    booster.save_model(args.out, num_iteration=booster.best_iteration)
    meta_out = {
        "features": feature_lib.FEATURE_NAMES,
        "threshold": threshold,
        "alarm_threshold": alarm_threshold,
        "confirm_ticks": args.confirm_ticks,
        "settle_seconds": args.settle,
        "trees": booster.best_iteration,
        "data": args.data,
        "scenarios": len({m[0] for m in meta}),
        "samples": int(X.shape[0]),
        "test_metrics": results,
        "top_features": [[n, float(g / total)] for n, g in top],
        "trained_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    meta_path = os.path.splitext(args.out)[0] + ".json"
    with open(meta_path, "w", encoding="utf-8") as handle:
        json.dump(meta_out, handle, indent=2)
    print(f"\nsaved {args.out} and {meta_path} ({time.time() - started:.0f}s)")


if __name__ == "__main__":
    main()
