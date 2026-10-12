"""Train the anomaly detector and cause classifier (anomaly.py).

    python train_anomaly.py recordings/offline --out models/anomaly.pkl --report ../results/anomaly

Steps
1. Replay every scenario through the agent's feature extractor (the same
   code as train_model.py), split by scenario (70/15/15).
2. Normal data: zone-ticks of training scenarios before anything has
   happened anywhere in the building (no fire, nuisance or sensor fault yet).
3. Fit the scaler, an isolation forest and an MLP autoencoder on a sample of
   the normal training rows.
4. Thresholds on the validation scenarios' normal rows: the lowest quantile
   (both models) for which debounced anomalies stay within --max-false per
   100 zone-hours.
5. Cause classifier: LightGBM, classes normal / fire / nuisance / fault, on
   the full feature vector plus the anomaly score.
6. Fire release threshold for the hybrid detector: the most sensitive
   probability of "fire" (on anomalous rows) whose releases add at most
   --max-extra-false false releases per 100 quiet zone-hours to the rules'
   own on validation.
7. Held-out test report: share of fires, nuisances and sensor faults that are
   anomalous within --within simulated seconds; false anomalies on normal
   data; cause accuracy per class; rules vs hybrid sprinkler releases.
   Writes summary.md, metrics.json, anomaly_roc.png and cause_confusion.csv.

Requires: pip install scikit-learn lightgbm numpy matplotlib
"""
import argparse
import collections
import json
import os
import time

import numpy as np

import anomaly
import building_agent
import dataset
import features as feature_lib
import train_model as tm

KINDS = {"fire": 3, "nuisance": 5, "fault": 6}   # meta index of each label


def debounced_groups(meta, flags, ticks):
    """{(scenario, zone): [(time, debounced flag, meta)]} in time order."""
    groups = collections.defaultdict(list)
    for m, f in zip(meta, flags):
        groups[(m[0], m[2])].append((m[1], bool(f), m))
    out = {}
    for key, items in groups.items():
        items.sort(key=lambda x: x[0])
        flags_d = tm.debounce([f for _, f, _ in items], ticks)
        out[key] = [(t, d, m) for (t, _, m), d in zip(items, flags_d)]
    return out


def false_rate(meta, flags, ticks):
    """Debounced anomaly onsets per 100 zone-hours (rows are normal data)."""
    onsets, seconds = 0, 0.0
    for items in debounced_groups(meta, flags, ticks).values():
        times = [t for t, _, _ in items]
        dt = float(np.median(np.diff(times))) if len(times) > 1 else 10.0
        seconds += dt * len(items)
        previous = False
        for t, d, _ in items:
            if d and not previous:
                onsets += 1
            previous = d
    hours = seconds / 3600.0
    return (100.0 * onsets / hours if hours else float("nan")), onsets, hours


def event_detection(meta, flags, ticks, within):
    """Per kind: events and how many were anomalous within `within` s."""
    result = {}
    groups = debounced_groups(meta, flags, ticks)
    for kind, index in KINDS.items():
        events = detected = 0
        delays = []
        for items in groups.values():
            onset = next((t for t, _, m in items if m[index]), None)
            if onset is None or onset <= items[0][0]:
                continue    # no event, or it began before the data does
            events += 1
            hit = next((t for t, d, m in items if d and t >= onset), None)
            if hit is not None and hit - onset <= within:
                detected += 1
                delays.append(hit - onset)
        result[kind] = {"events": events, "detected": detected,
                        "rate": detected / events if events else float("nan"),
                        "delay_median_s": float(np.median(delays)) if delays else float("nan")}
    return result


def main():
    import lightgbm as lgb
    from sklearn.ensemble import IsolationForest
    from sklearn.neural_network import MLPRegressor

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("data", nargs="+")
    parser.add_argument("--out", default="models/anomaly.pkl")
    parser.add_argument("--report", default="../results/anomaly")
    parser.add_argument("--settle", type=float, default=300.0)
    parser.add_argument("--max-false", type=float, default=1.0,
                        help="false anomalies per 100 normal zone-hours (validation)")
    parser.add_argument("--max-extra-false", type=float, default=0.2,
                        help="false sprinkler releases per 100 quiet zone-hours the hybrid "
                             "may add to the rules' own (validation)")
    parser.add_argument("--within", type=float, default=60.0)
    parser.add_argument("--confirm-ticks", type=int, default=building_agent.CONFIRM_TICKS)
    parser.add_argument("--sample", type=int, default=150000, help="normal rows to fit on")
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--data-dir", default=os.environ.get("BUILDING_DATA_DIR"))
    args = parser.parse_args()
    started = time.time()
    rng = np.random.default_rng(args.seed)

    rows = dataset.load(args.data)
    tm.load_neighbours(args.data, args.data_dir)
    X, meta = tm.build(rows, args.settle, args.workers)
    del rows
    split = np.array([tm.split_of(m[0]) for m in meta])
    # Normal: before anything has happened in the scenario (no fire,
    # nuisance or fault yet anywhere; afterwards smoke lingers and spreads).
    first_event = {}
    for m in meta:
        if m[3] or m[5] or m[6]:
            first_event[m[0]] = min(first_event.get(m[0], m[1]), m[1])
    normal = np.array([m[1] < first_event.get(m[0], float("inf")) for m in meta])
    y = np.array([anomaly.label(m[3], m[5], m[6]) for m in meta], dtype=np.int32)
    print(f"{X.shape[0]} zone-ticks, {normal.mean() * 100:.1f} % normal "
          f"(built in {time.time() - started:.0f}s)", flush=True)

    model = anomaly.AnomalyModel()
    train_normal = np.flatnonzero(normal & (split == "train"))
    fit_rows = rng.choice(train_normal, min(args.sample, len(train_normal)), replace=False)
    model.fit_scaler(X[fit_rows])
    Z = np.clip(model.inputs(X[fit_rows]), -50.0, 50.0)
    model.forest = IsolationForest(n_estimators=200, max_samples=4096, random_state=args.seed,
                                   n_jobs=args.workers).fit(Z)
    width = Z.shape[1]
    model.autoencoder = MLPRegressor(hidden_layer_sizes=(16, 8, 16), activation="tanh",
                                     max_iter=80, early_stopping=True, random_state=args.seed,
                                     batch_size=512, learning_rate_init=1e-3).fit(Z, Z)
    print(f"fitted isolation forest and autoencoder ({width} -> 16 -> 8 -> 16 -> {width}) "
          f"on {len(fit_rows)} normal rows ({time.time() - started:.0f}s)", flush=True)

    # Raw scores for everything (in chunks to bound memory).
    forest_raw = np.empty(len(X))
    ae_raw = np.empty(len(X))
    for i in range(0, len(X), 200000):
        forest_raw[i:i + 200000], ae_raw[i:i + 200000] = model.raw_scores(X[i:i + 200000])

    valid_normal = normal & (split == "valid")
    meta_vn = [m for m, k in zip(meta, valid_normal) if k]
    chosen = None
    for q in (0.99, 0.995, 0.998, 0.999, 0.9995, 0.9998, 0.9999, 0.99995, 1.0):
        ft = float(np.quantile(forest_raw[valid_normal], q)) * (1.0 if q < 1 else 1.05)
        at = float(np.quantile(ae_raw[valid_normal], q)) * (1.0 if q < 1 else 1.05)
        flags = np.maximum(forest_raw[valid_normal] / ft, ae_raw[valid_normal] / at) >= 1.0
        rate, onsets, hours = false_rate(meta_vn, flags, args.confirm_ticks)
        print(f"  quantile {q}: {rate:.2f} false anomalies per 100 zone-hours", flush=True)
        chosen = (q, ft, at, rate)
        if rate <= args.max_false:
            break
    q, model.forest_threshold, model.ae_threshold, valid_rate = chosen
    score = np.maximum(forest_raw / model.forest_threshold, ae_raw / model.ae_threshold)
    anomalous = score >= 1.0

    # Cause classifier: all event rows plus a sample of the normal ones.
    A = np.column_stack([X, score]).astype(np.float32)
    names = feature_lib.FEATURE_NAMES + ["anomaly_score"]

    def pick(name, normal_cap):
        idx = np.flatnonzero(split == name)
        events = idx[y[idx] > 0]
        quiet = idx[y[idx] == 0]
        quiet = rng.choice(quiet, min(normal_cap, len(quiet)), replace=False)
        return np.concatenate([events, quiet])

    tr, va = pick("train", 400000), pick("valid", 100000)
    cause = lgb.train(
        {"objective": "multiclass", "num_class": len(anomaly.CLASSES), "learning_rate": 0.05,
         "num_leaves": 31, "min_data_in_leaf": 50, "feature_fraction": 0.8,
         "bagging_fraction": 0.8, "bagging_freq": 1, "verbose": -1},
        lgb.Dataset(A[tr], y[tr], feature_name=names),
        num_boost_round=1000, valid_sets=[lgb.Dataset(A[va], y[va])],
        callbacks=[lgb.early_stopping(40, verbose=False), lgb.log_evaluation(0)])
    model.cause = cause
    probs = np.empty((len(X), len(anomaly.CLASSES)))
    for i in range(0, len(X), 200000):
        probs[i:i + 200000] = cause.predict(A[i:i + 200000], num_iteration=cause.best_iteration)
    print(f"cause classifier: {cause.best_iteration} trees ({time.time() - started:.0f}s)",
          flush=True)

    # Hybrid release threshold on validation.
    vmask = split == "valid"
    meta_v = [m for m, k in zip(meta, vmask) if k]
    rules_v = np.array([m[7] for m in meta_v])
    p_fire = probs[:, 1]
    rules_fa = tm.evaluate(meta_v, rules_v, args.confirm_ticks)["false_alarms_per_100_room_hours"]
    release = None
    for thr in (0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 0.98, 0.99, 1.01):
        flags = rules_v | (anomalous[vmask] & (p_fire[vmask] >= thr))
        m = tm.evaluate(meta_v, flags, args.confirm_ticks)
        release = (thr, m)
        if m["false_alarms_per_100_room_hours"] - rules_fa <= args.max_extra_false:
            break
    model.cause_fire_release = None if release[0] > 1 else float(release[0])
    print(f"hybrid release: anomalous and P(fire) >= {release[0]}", flush=True)

    # Held-out test report.
    tmask = split == "test"
    meta_t = [m for m, k in zip(meta, tmask) if k]
    detection = event_detection(meta_t, anomalous[tmask], args.confirm_ticks, args.within)
    detection_5min = event_detection(meta_t, anomalous[tmask], args.confirm_ticks, 300.0)
    test_normal = normal & tmask
    meta_tn = [m for m, k in zip(meta, test_normal) if k]
    test_rate, test_onsets, test_hours = false_rate(meta_tn, anomalous[test_normal],
                                                    args.confirm_ticks)
    predicted = probs[tmask].argmax(axis=1)
    confusion = np.zeros((len(anomaly.CLASSES),) * 2, dtype=int)
    for a, b in zip(y[tmask], predicted):
        confusion[a, b] += 1
    per_class = {c: float(confusion[i, i] / confusion[i].sum()) if confusion[i].sum() else None
                 for i, c in enumerate(anomaly.CLASSES)}
    rules_t = np.array([m[7] for m in meta_t])
    thr = model.cause_fire_release if model.cause_fire_release is not None else 9.0
    hybrid_t = rules_t | (anomalous[tmask] & (p_fire[tmask] >= thr))
    releases = {"rules": tm.evaluate(meta_t, rules_t, args.confirm_ticks),
                "hybrid": tm.evaluate(meta_t, hybrid_t, args.confirm_ticks)}
    event_rows = y[tmask] > 0
    roc_auc = tm.auc(event_rows.astype(int), score[tmask])

    model.meta = {"features": feature_lib.FEATURE_NAMES, "quantile": q,
                  "valid_false_per_100h": valid_rate, "test_false_per_100h": test_rate,
                  "detection_within_s": args.within, "detection": detection,
                  "detection_300s": detection_5min,
                  "cause_accuracy": per_class, "auc_event_vs_normal": roc_auc,
                  "releases": releases, "scenarios": len({m[0] for m in meta}),
                  "trained_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    model.save(args.out)

    os.makedirs(args.report, exist_ok=True)
    with open(os.path.join(args.report, "metrics.json"), "w", encoding="utf-8") as f:
        json.dump(model.meta, f, indent=2, default=float)
    with open(os.path.join(args.report, "cause_confusion.csv"), "w", encoding="utf-8") as f:
        f.write("true," + ",".join(anomaly.CLASSES) + "\n")
        for i, c in enumerate(anomaly.CLASSES):
            f.write(c + "," + ",".join(str(v) for v in confusion[i]) + "\n")
    lines = [
        f"Anomaly detection, held-out scenarios (threshold: quantile {q} of normal "
        f"validation scores)", "",
        f"* False anomalies on normal data: {test_rate:.2f} per 100 zone-hours "
        f"({test_onsets} in {test_hours:.0f} zone-hours); validation {valid_rate:.2f}",
        f"* AUC, event rows vs normal rows: {roc_auc:.3f}", "",
        f"| Event | Events | Anomalous within {args.within:.0f} s | within 300 s | Median delay |",
        "|---|---|---|---|---|",
    ]
    for kind, d in detection.items():
        lines.append(f"| {kind} | {d['events']} | {100 * d['rate']:.0f} % | "
                     f"{100 * detection_5min[kind]['rate']:.0f} % | {d['delay_median_s']:.0f} s |")
    lines += ["", "Cause classifier, share of rows classified correctly:", "",
              "| Class | Accuracy |", "|---|---|"]
    lines += [f"| {c} | {100 * a:.1f} % |" if a is not None else f"| {c} | - |"
              for c, a in per_class.items()]
    lines += ["", f"Sprinkler releases (hybrid: rules, or anomalous with P(fire) >= "
              f"{model.cause_fire_release}):", "",
              "| Detector | Fires released | Median delay after ignition | False releases "
              "per 100 zone-h |", "|---|---|---|---|"]
    for name, r in releases.items():
        lines.append(f"| {name} | {r['detected']}/{r['fires']} | "
                     f"{r['delay_after_ignition_median_s']:.0f} s | "
                     f"{r['false_alarms_per_100_room_hours']:.2f} |")
    with open(os.path.join(args.report, "summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        s_t = score[tmask]
        fig, ax = plt.subplots(figsize=(5, 4))
        for kind, cls in (("fire", 1), ("nuisance", 2), ("fault", 3)):
            sel = (y[tmask] == cls) | (y[tmask] == 0)
            yy = (y[tmask][sel] == cls).astype(int)
            order = np.argsort(-s_t[sel])
            tp = np.cumsum(yy[order]) / max(1, yy.sum())
            fp = np.cumsum(1 - yy[order]) / max(1, (1 - yy).sum())
            ax.plot(fp, tp, label=f"{kind} (AUC {tm.auc(yy, s_t[sel]):.2f})")
        ax.plot([0, 1], [0, 1], "k:", lw=0.8)
        ax.set_xscale("log")
        ax.set_xlim(1e-4, 1)
        ax.set_xlabel("false positive rate (normal rows)")
        ax.set_ylabel("true positive rate")
        ax.set_title("Anomaly score, held-out scenarios")
        ax.legend(loc="lower right")
        fig.tight_layout()
        fig.savefig(os.path.join(args.report, "anomaly_roc.png"), dpi=150)
    except Exception as exc:  # noqa: BLE001
        print(f"no plot: {exc}")
    print(f"saved {args.out} ({time.time() - started:.0f}s)")


if __name__ == "__main__":
    main()
