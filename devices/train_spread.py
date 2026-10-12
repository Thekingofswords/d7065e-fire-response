"""Train and evaluate the fire-spread prediction model (spread_model.py).

    python train_spread.py --scenarios 600 --out ../models/spread_lgbm.txt \
        --report ../results/spread

1. Simulates random fires in random neighbourhoods of the real building
   (fire_simulator + sensor instruments + the building agent's policy, half
   of the runs with the agent and its fire-door policy, half without), and
   records, per zone and tick, the spread features the live agent computes
   (SpreadFeatures) plus the ground truth.
2. Label: the zone is tenable now and becomes untenable (burning, > 60 °C or
   > 10 %/m smoke) within the next HORIZON_S (300) simulated seconds.
3. Trains a LightGBM classifier on a scenario-grouped split and picks the
   threshold with the best F1 on the validation scenarios.
4. Reports on the held-out scenarios, for the model and for the baseline
   rule (neighbour of a fire or of a hot zone): AUC, precision and recall,
   and the share of onsets flagged at least 60 s in advance (lead time).
"""
import argparse
import json
import multiprocessing
import os
import random
import time

import numpy as np

import building_agent
import fire_simulator
import scenarios
import sensor
import spread_model
import topology

fire_simulator.print = lambda *a, **k: None

DT = 10.0


def simulate(job):
    sc, sub_json, seed, with_agent = job
    rng = random.Random(seed)
    sub = topology.Topology.from_json(sub_json)
    model = fire_simulator.FireModel(sc, sub)
    zones = list(model.rooms)
    instruments = {z: sensor.Instruments(sub.zones[z].name, random.Random(rng.random()),
                                         sub.zones[z].volume) for z in zones}
    corridors = {z for z in zones if sub.zones[z].type == "corridor"}
    policy = building_agent.BuildingPolicy(zones, sub.neighbour_map(zones),
                                           building_agent.RulesDetector(), corridors)
    features = spread_model.SpreadFeatures(sub, zones)
    rows, truth_flags = [], []
    while model.time < sc["duration"]:
        model.step(DT)
        readings, now_untenable = {}, {}
        for z, view in model.rooms.items():
            truth = view.outputs()
            truth.update(model.labels(z))
            inst = instruments[z]
            inst.step(truth, DT, 0, view.damper)
            readings[z] = inst.readings()
            now_untenable[z] = spread_model.untenable(truth)
        decisions = policy.step(model.time, readings)
        if with_agent:
            for z, d in decisions.items():
                model.rooms[z].sprinkler_on = d["sprinkler"]
                model.rooms[z].damper = d.get("damper", 0.0)
            doors = policy.doors(model.time, decisions)
            closed_levels = {lv for lv, st in doors["levels"].items() if st == "closed"}
            model.set_doors({pair: (topology.split(pair[0])[0] not in closed_levels
                                    and scenarios.door_is_open(sc, *pair))
                             for pair in model.door_links})
        door_open = dict(model.door_open)
        # The agent observes in both cases; it only acts in the "with agent" runs.
        tick_rows = features.rows(policy.last_features, decisions, door_open)
        rows.append({z: r for z, r in tick_rows.items()})
        truth_flags.append(now_untenable)
    # Labels: tenable now, untenable within the horizon.
    horizon = int(spread_model.HORIZON_S / DT)
    X, y, meta = [], [], []
    onset = {}
    for i, flags in enumerate(truth_flags):
        for z, bad in flags.items():
            if bad and z not in onset:
                onset[z] = i
    for i, tick in enumerate(rows):
        for z, r in tick.items():
            if truth_flags[i][z]:
                continue
            o = onset.get(z)
            label = int(o is not None and i < o <= i + horizon)
            X.append(r)
            y.append(label)
            meta.append((sc["id"], z, i, -1 if o is None else o))
    return X, y, meta


def make_jobs(topo, n, zones, seed):
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
        sc = scenarios.random_scenario(len(jobs) + 1, rooms, rng, corridors=corridors)
        if not sc["fires"]:
            continue
        sc["nuisances"], sc["sensor_faults"] = [], []
        sc["duration"] = min(sc["duration"], 1800.0)
        jobs.append((sc, sub.to_json(), rng.randrange(2 ** 31), rng.random() < 0.5))
    return jobs


def split_of(scenario_id):
    bucket = (scenario_id * 2654435761) % (2 ** 32) % 100
    return "train" if bucket < 70 else "valid" if bucket < 85 else "test"


def auc(y, p):
    order = np.argsort(p)
    ranks = np.empty(len(p))
    ranks[order] = np.arange(1, len(p) + 1)
    pos = y == 1
    n_pos, n_neg = pos.sum(), (~pos).sum()
    if not n_pos or not n_neg:
        return float("nan")
    return float((ranks[pos].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def event_metrics(meta, p, threshold, lead_ticks):
    """Share of onsets flagged >= lead_ticks ticks before onset, precision
    and recall at the row level."""
    flagged_early = {}
    for (sid, z, i, o), prob in zip(meta, p):
        if o < 0:
            continue
        key = (sid, z)
        flagged_early.setdefault(key, False)
        if prob >= threshold and o - lead_ticks >= i >= o - int(spread_model.HORIZON_S / DT):
            flagged_early[key] = True
    onsets = len(flagged_early)
    return onsets, (sum(flagged_early.values()) / onsets if onsets else float("nan"))


def main():
    import lightgbm as lgb
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--scenarios", type=int, default=600)
    parser.add_argument("--zones", type=int, default=25)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    parser.add_argument("--out", default="../models/spread_lgbm.txt")
    parser.add_argument("--report", default="../results/spread")
    default_data = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "abuilding")
    parser.add_argument("--data-dir", default=os.environ.get("BUILDING_DATA_DIR") or default_data)
    args = parser.parse_args()
    started = time.time()

    topo = topology.Topology.from_files(args.data_dir)
    jobs = make_jobs(topo, args.scenarios, args.zones, args.seed)
    X, y, meta = [], [], []
    with multiprocessing.Pool(args.workers) as pool:
        for i, (xs, ys, ms) in enumerate(pool.imap_unordered(simulate, jobs), start=1):
            X += xs
            y += ys
            meta += ms
            if i % 50 == 0:
                print(f"{i}/{len(jobs)} scenarios, {len(y)} rows", flush=True)
    X = np.asarray(X, dtype=np.float32)
    y = np.asarray(y, dtype=np.int8)
    split = np.array([split_of(m[0]) for m in meta])
    print(f"{len(y)} rows, {y.mean() * 100:.2f} % positive, built in {time.time() - started:.0f}s")

    masks = {s: split == s for s in ("train", "valid", "test")}
    train = lgb.Dataset(X[masks["train"]], y[masks["train"]], feature_name=spread_model.FEATURES)
    valid = lgb.Dataset(X[masks["valid"]], y[masks["valid"]], reference=train)
    params = {"objective": "binary", "learning_rate": 0.05, "num_leaves": 31,
              "min_data_in_leaf": 100, "feature_fraction": 0.9, "bagging_fraction": 0.8,
              "bagging_freq": 1, "metric": ["auc"], "verbose": -1}
    booster = lgb.train(params, train, num_boost_round=1000, valid_sets=[valid],
                        callbacks=[lgb.early_stopping(50, verbose=False)])
    p = booster.predict(X, num_iteration=booster.best_iteration)
    base = np.array([spread_model.baseline_risk(r) for r in X])

    # Threshold: best F1 on validation.
    pv, yv = p[masks["valid"]], y[masks["valid"]]
    best = (0.0, 0.5)
    for t in np.arange(0.05, 0.95, 0.05):
        tp = ((pv >= t) & (yv == 1)).sum()
        prec = tp / max(1, (pv >= t).sum())
        rec = tp / max(1, (yv == 1).sum())
        f1 = 2 * prec * rec / max(1e-9, prec + rec)
        if f1 > best[0]:
            best = (f1, float(round(t, 2)))
    threshold = best[1]

    def report(name, scores, thr):
        st, yt = scores[masks["test"]], y[masks["test"]]
        tp = ((st >= thr) & (yt == 1)).sum()
        prec = tp / max(1, (st >= thr).sum())
        rec = tp / max(1, (yt == 1).sum())
        mt = [m for m, k in zip(meta, masks["test"]) if k]
        onsets, early = event_metrics(mt, st, thr, lead_ticks=int(60 / DT))
        return {"name": name, "auc": auc(yt, st), "threshold": thr, "precision": float(prec),
                "recall": float(rec), "onsets": onsets, "flagged_60s_ahead": float(early)}

    results = [report("model", p, threshold), report("baseline", base, 0.5)]
    os.makedirs(args.report, exist_ok=True)
    lines = [f"Fire-spread prediction, horizon {spread_model.HORIZON_S:.0f} s, held-out scenarios",
             "", "| Predictor | AUC | Threshold | Precision | Recall | Onsets | Flagged ≥ 60 s ahead |",
             "|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['name']} | {r['auc']:.3f} | {r['threshold']} | {r['precision']:.2f} | "
                     f"{r['recall']:.2f} | {r['onsets']} | {r['flagged_60s_ahead'] * 100:.0f} % |")
    gains = booster.feature_importance("gain")
    top = sorted(zip(spread_model.FEATURES, gains), key=lambda x: -x[1])[:8]
    lines += ["", "Most important features: " + ", ".join(
        f"{n} ({100 * g / max(1e-9, sum(gains)):.0f} %)" for n, g in top)]
    with open(os.path.join(args.report, "summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    booster.save_model(args.out, num_iteration=booster.best_iteration)
    with open(os.path.splitext(args.out)[0] + ".json", "w", encoding="utf-8") as f:
        json.dump({"features": spread_model.FEATURES, "threshold": threshold,
                   "horizon_s": spread_model.HORIZON_S, "results": results,
                   "scenarios": len(jobs), "rows": int(len(y)),
                   "trained_at": time.strftime("%Y-%m-%d %H:%M:%S")}, f, indent=2)

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(5, 4))
        for name, scores, colour in (("model", p, "#2563eb"), ("baseline rule", base, "#f59e0b")):
            st, yt = scores[masks["test"]], y[masks["test"]]
            pts = []
            for t in np.unique(np.round(st, 3))[::max(1, len(np.unique(st)) // 200)]:
                tpr = ((st >= t) & (yt == 1)).sum() / max(1, (yt == 1).sum())
                fpr = ((st >= t) & (yt == 0)).sum() / max(1, (yt == 0).sum())
                pts.append((fpr, tpr))
            pts = sorted(pts + [(0, 0), (1, 1)])
            ax.plot([a for a, _ in pts], [b for _, b in pts], label=name, color=colour)
        ax.plot([0, 1], [0, 1], color="#999", ls=":", lw=1)
        ax.set_xlabel("False positive rate")
        ax.set_ylabel("True positive rate")
        ax.set_title("Fire-spread prediction (held-out)", fontsize=10)
        ax.legend(fontsize=8)
        fig.tight_layout()
        fig.savefig(os.path.join(args.report, "spread_roc.png"), dpi=150)
    except ImportError:
        pass
    print(f"saved {args.out} ({time.time() - started:.0f}s)")


if __name__ == "__main__":
    main()
