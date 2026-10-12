"""Fire-spread prediction: which zones become untenable next.

For every zone and decision tick the model estimates the probability that
the zone becomes *untenable* (burning, or gas above 60 °C, or smoke above
10 %/m) within the next HORIZON_S simulated seconds, from what the agent can
observe:

* the room adjacency graph (doorways, corridor openings, stairwells,
  topology.py) and the door states (open, closed) from the door gateway
* temperature gradients: each neighbour's temperature minus the zone's own,
  weighted by how open the connection is, plus rates of rise
* smoke and CO around the zone, the agent's per-zone state (fire confirmed,
  alarm), graph distance to the nearest confirmed fire, sprinklers

`SpreadFeatures` turns one tick of agent data into a feature row per zone;
`SpreadModel` wraps the trained LightGBM classifier (train_spread.py) and
falls back to a transparent baseline (neighbour of a fire or of a hot zone)
when no model is available. The agent publishes the result on building/risk.
"""
import collections
import json
import math
import os

import common

HORIZON_S = float(os.environ.get("SPREAD_HORIZON_SECONDS", "300"))
UNTENABLE_TEMP_C = 60.0
UNTENABLE_SMOKE = 10.0      # %/m
MAX_HOPS = 8
CLOSED_DOOR = 0.03          # how much a closed door still passes (leak, gaps)
STAIR_LINK = 0.5

FEATURES = [
    "temperature", "temperature_rate_60", "temperature_excess", "smoke", "smoke_rate_60",
    "co", "sprinkler",
    "nb_max_temperature", "nb_max_gradient", "nb_max_gradient_open", "nb_max_rate",
    "nb_max_smoke", "nb_max_smoke_open", "nb_fire_open", "nb_fire_any", "nb_alarm_any",
    "fire_hops", "is_corridor", "is_stair", "neighbours",
]


def _v(features, name, default=0.0):
    value = (features or {}).get(name, default)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(value) else value


def untenable(truth):
    """Ground truth used for the labels (training only)."""
    return (truth.get(common.LAYER_GAS_TEMP, 0.0) >= UNTENABLE_TEMP_C
            or truth.get(common.LAYER_OBSCURATION, 0.0) >= UNTENABLE_SMOKE
            or truth.get(common.LAYER_HRR, 0.0) > 1.0)


class SpreadFeatures:
    def __init__(self, topo, zones):
        self.zones = list(zones)
        zone_set = set(self.zones)
        self.links = collections.defaultdict(list)   # zone -> [(other, pair, kind)]
        door_pairs = set(topo.conn_doors)
        for a, b, _, _ in topo.connections:
            if a in zone_set and b in zone_set:
                pair = tuple(sorted((a, b)))
                kind = "door" if pair in door_pairs else "opening"
                self.links[a].append((b, pair, kind))
                self.links[b].append((a, pair, kind))
        for a, b in topo.stairs:
            if a in zone_set and b in zone_set:
                self.links[a].append((b, None, "stair"))
                self.links[b].append((a, None, "stair"))
        stair_zones = {z for link in topo.stairs for z in link}
        self.is_corridor = {z: float(topo.zones[z].type == "corridor") for z in self.zones}
        self.is_stair = {z: float(z in stair_zones) for z in self.zones}

    def openness(self, pair, kind, door_open):
        if kind == "stair":
            return STAIR_LINK
        if kind == "opening":
            return 1.0
        return 1.0 if door_open.get(pair, True) else CLOSED_DOOR

    def hops(self, sources):
        dist = {z: 0 for z in sources}
        queue = collections.deque(sources)
        while queue:
            z = queue.popleft()
            if dist[z] >= MAX_HOPS:
                continue
            for other, _, _ in self.links.get(z, ()):
                if other not in dist:
                    dist[other] = dist[z] + 1
                    queue.append(other)
        return dist

    def rows(self, room_features, decisions, door_open=None):
        """{zone: [feature values in FEATURES order]} for zones with features.

        room_features: features.FeatureExtractor output; decisions: the
        policy's {zone: {"state", "sprinkler", "p_fire", ...}};
        door_open: {(zone a, zone b) sorted: bool} (missing = open)."""
        door_open = door_open or {}
        decisions = decisions or {}
        burning = {z for z, d in decisions.items()
                   if d.get("state") == "SUPPRESSING" or d.get("p_fire", 0) >= 0.5}
        alarmed = {z for z, d in decisions.items() if d.get("state") in ("ALARM", "SUPPRESSING")}
        dist = self.hops(burning)
        out = {}
        for z in self.zones:
            f = room_features.get(z)
            if f is None:
                continue
            temp = _v(f, "temperature", common.AMBIENT_TEMP_C)
            nb_t, nb_g, nb_go, nb_r, nb_s, nb_so = [], [], [], [], [], []
            fire_open = fire_any = alarm_any = 0.0
            for other, pair, kind in self.links.get(z, ()):
                g = room_features.get(other)
                if g is None:
                    continue
                k = self.openness(pair, kind, door_open)
                t = _v(g, "temperature", common.AMBIENT_TEMP_C)
                s = _v(g, "smoke")
                nb_t.append(t)
                nb_g.append(t - temp)
                nb_go.append((t - temp) * k)
                nb_r.append(_v(g, "temperature_rate_60"))
                nb_s.append(s)
                nb_so.append(s * k)
                if other in burning:
                    fire_any = 1.0
                    fire_open = max(fire_open, k)
                if other in alarmed:
                    alarm_any = 1.0
            d = decisions.get(z, {})
            out[z] = [
                temp, _v(f, "temperature_rate_60"), _v(f, "temperature_excess"),
                _v(f, "smoke"), _v(f, "smoke_rate_60"), _v(f, "co"),
                1.0 if d.get("sprinkler") else 0.0,
                max(nb_t, default=temp), max(nb_g, default=0.0), max(nb_go, default=0.0),
                max(nb_r, default=0.0), max(nb_s, default=0.0), max(nb_so, default=0.0),
                fire_open, fire_any, alarm_any,
                float(dist.get(z, MAX_HOPS + 1)), self.is_corridor[z], self.is_stair[z],
                float(len(self.links.get(z, ()))),
            ]
        return out


def baseline_risk(row):
    """Transparent rule: next to a confirmed fire through an open
    connection, or next to a markedly hotter zone."""
    f = dict(zip(FEATURES, row))
    if f["fire_hops"] == 0:
        return 0.0
    if f["nb_fire_open"] >= 0.5:
        return 0.8
    if f["nb_max_gradient_open"] >= 10.0 or f["nb_max_smoke_open"] >= 5.0:
        return 0.6
    if f["nb_fire_any"]:
        return 0.3
    return 0.02


class SpreadModel:
    """The trained classifier, or the baseline when there is none."""

    def __init__(self, path=None):
        path = path or os.environ.get("SPREAD_MODEL_PATH", "/models/spread_lgbm.txt")
        self.name = "baseline"
        self.booster = None
        self.threshold = float(os.environ.get("SPREAD_RISK_THRESHOLD", "0.3"))
        try:
            import lightgbm
            with open(os.path.splitext(path)[0] + ".json", encoding="utf-8") as f:
                meta = json.load(f)
            if meta.get("features") != FEATURES:
                raise ValueError("spread model trained with other features")
            self.booster = lightgbm.Booster(model_file=path)
            self.threshold = float(os.environ.get("SPREAD_RISK_THRESHOLD", meta["threshold"]))
            self.horizon = meta.get("horizon_s", HORIZON_S)
            self.name = "lightgbm"
        except Exception as exc:  # noqa: BLE001
            print(f"spread model: baseline rule ({exc})", flush=True)

    def predict(self, rows):
        """{zone: probability} for {zone: feature row}."""
        if not rows:
            return {}
        zones = list(rows)
        if self.booster is None:
            return {z: baseline_risk(rows[z]) for z in zones}
        probs = self.booster.predict([rows[z] for z in zones])
        return {z: float(p) for z, p in zip(zones, probs)}

    def at_risk(self, probabilities, decisions=None):
        """Zones at risk: above the threshold and not already burning."""
        decisions = decisions or {}
        return {z: round(p, 3) for z, p in probabilities.items()
                if p >= self.threshold and decisions.get(z, {}).get("state") != "SUPPRESSING"}
