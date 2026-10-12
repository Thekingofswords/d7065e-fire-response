"""Feature extraction shared by training and the live building agent.

Both train_model.py and building_agent.py feed sensor readings tick by tick
through the same FeatureExtractor, so the model sees exactly the same
features in training and in operation (no train/serve skew).

Per room it computes, from the last few minutes of readings:

* current temperature, smoke, CO and CO2
* rates of change over 30/60/120/300 simulated seconds (per minute)
* rise above the room's own recent baseline (rolling minimum)
* rolling maximum and standard deviation (spiky vs. sustained signals)
* simple ratios, e.g. CO per unit smoke (real fires make CO with their
  smoke; steam and dust do not)
* the same key signals for the neighbouring rooms, and the difference to
  them, so a model can tell "fire here" from "smoke drifting in from next
  door"
* whether the zone is a corridor (corridors collect smoke from every room
  along them, so the same reading means something different there)

Only standard library code, so the agent image needs nothing extra for it.
"""
import bisect
import math

SIGNALS = ("temperature", "smoke", "co", "co2")
RATE_WINDOWS = (30, 60, 120, 300)   # simulated seconds
BASELINE_WINDOW = 300
STD_WINDOW = 120
HISTORY_SECONDS = max(RATE_WINDOWS + (BASELINE_WINDOW,)) + 60

NEIGHBOUR_FEATURES = (
    "temperature", "smoke", "co", "temperature_rate_60", "smoke_rate_60",
    "temperature_excess",
)

NAN = float("nan")


def _room_feature_names():
    names = list(SIGNALS)
    for signal in SIGNALS:
        for window in RATE_WINDOWS:
            names.append(f"{signal}_rate_{window}")
        names.append(f"{signal}_excess")
        names.append(f"{signal}_max_{BASELINE_WINDOW}")
        names.append(f"{signal}_std_{STD_WINDOW}")
    names += ["co_per_smoke", "temperature_rate_per_smoke_rate"]
    return names


ROOM_FEATURES = _room_feature_names()
HEALTH_FEATURES = [f"{signal}_ok" for signal in SIGNALS]
FEATURE_NAMES = ROOM_FEATURES + [
    name
    for feature in NEIGHBOUR_FEATURES
    for name in (f"nb_max_{feature}", f"nb_diff_{feature}")
] + HEALTH_FEATURES + ["is_corridor"]

# Features that depend on each signal, blanked (NaN) when it is faulty.
SIGNAL_FEATURES = {
    signal: [n for n in ROOM_FEATURES if n == signal or n.startswith(signal + "_")]
    for signal in SIGNALS
}
SIGNAL_FEATURES["smoke"] += ["co_per_smoke", "temperature_rate_per_smoke_rate"]
SIGNAL_FEATURES["co"] += ["co_per_smoke"]


def _nanmax(values):
    values = [v for v in values if not math.isnan(v)]
    return max(values) if values else NAN


class _History:
    """Time-ordered readings of one room with fast window lookups."""

    def __init__(self):
        self.times = []
        self.values = {signal: [] for signal in SIGNALS}

    def add(self, time_s, values):
        self.times.append(time_s)
        for signal in SIGNALS:
            self.values[signal].append(values[signal])
        if time_s - self.times[0] > 2 * HISTORY_SECONDS:
            start = bisect.bisect_left(self.times, time_s - HISTORY_SECONDS)
            self.times = self.times[start:]
            for signal in SIGNALS:
                self.values[signal] = self.values[signal][start:]

    def index_at(self, time_s):
        """Index of the last sample at or before time_s, or -1."""
        return bisect.bisect_right(self.times, time_s + 1e-6) - 1

    def index_since(self, time_s):
        return bisect.bisect_left(self.times, time_s - 1e-6)


class FeatureExtractor:
    def __init__(self, rooms, neighbours, corridors=()):
        """neighbours: {room: [connected rooms]}; corridors: zones that are
        corridors."""
        self.rooms = list(rooms)
        self.corridors = set(corridors)
        self.neighbours = {room: list(neighbours.get(room, [])) for room in self.rooms}
        self.history = {room: _History() for room in self.rooms}

    def reset(self):
        self.history = {room: _History() for room in self.rooms}

    def update(self, time_s, readings, faults=None):
        """Add one tick of readings and return {room: {feature: value}}.

        readings: {room: {"temperature": .., "smoke": .., "co": .., "co2": ..}}
        faults:   {room: {signal: reason}} from health.SensorHealth; the
                  features of a faulty signal are NaN and <signal>_ok is 0.
        Rooms without a reading this tick are skipped.
        """
        faults = faults or {}
        own = {}
        for room in self.rooms:
            reading = readings.get(room)
            if reading is None:
                continue
            values = {s: float(reading.get(s, NAN)) for s in SIGNALS}
            history = self.history[room]
            history.add(time_s, values)
            features = self._room_features(time_s, values, history)
            room_faults = faults.get(room, {})
            for signal in SIGNALS:
                features[f"{signal}_ok"] = 0.0 if signal in room_faults else 1.0
                if signal in room_faults:
                    for name in SIGNAL_FEATURES[signal]:
                        features[name] = NAN
            features["is_corridor"] = 1.0 if room in self.corridors else 0.0
            own[room] = features

        result = {}
        for room, features in own.items():
            neighbours = [own[n] for n in self.neighbours[room] if n in own]
            for feature in NEIGHBOUR_FEATURES:
                if neighbours:
                    nb_max = _nanmax([n[feature] for n in neighbours])
                    features[f"nb_max_{feature}"] = nb_max
                    features[f"nb_diff_{feature}"] = features[feature] - nb_max
                else:
                    features[f"nb_max_{feature}"] = NAN
                    features[f"nb_diff_{feature}"] = NAN
            result[room] = features
        return result

    @staticmethod
    def _room_features(time_s, values, history):
        features = dict(values)
        past_index = {w: history.index_at(time_s - w) for w in RATE_WINDOWS}
        base_start = history.index_since(time_s - BASELINE_WINDOW)
        std_start = history.index_since(time_s - STD_WINDOW)

        for signal in SIGNALS:
            series = history.values[signal]
            current = values[signal]
            for window, index in past_index.items():
                features[f"{signal}_rate_{window}"] = (
                    (current - series[index]) / window * 60.0
                    if index >= 0 else NAN
                )
            recent = series[base_start:]
            features[f"{signal}_excess"] = current - min(recent)
            features[f"{signal}_max_{BASELINE_WINDOW}"] = max(recent)
            recent_std = series[std_start:]
            if len(recent_std) >= 2:
                mean = sum(recent_std) / len(recent_std)
                features[f"{signal}_std_{STD_WINDOW}"] = math.sqrt(
                    sum((v - mean) ** 2 for v in recent_std) / len(recent_std)
                )
            else:
                features[f"{signal}_std_{STD_WINDOW}"] = 0.0

        features["co_per_smoke"] = values["co"] / (values["smoke"] + 0.5)
        temp_rate = features["temperature_rate_60"]
        smoke_rate = features["smoke_rate_60"]
        features["temperature_rate_per_smoke_rate"] = (
            temp_rate / (abs(smoke_rate) + 0.5)
            if not (math.isnan(temp_rate) or math.isnan(smoke_rate)) else NAN
        )
        return features


def vector(features):
    """Feature dict -> list in FEATURE_NAMES order (model input)."""
    return [features.get(name, NAN) for name in FEATURE_NAMES]
