"""Anomaly detection and cause classification for the hybrid detector.

Stage 1, *anomaly*: is this zone behaving unlike a normal building? Two
unsupervised models are trained on feature vectors from normal periods only
(no fire, nuisance or sensor fault anywhere in the building at that tick):

* an isolation forest (scikit-learn), and
* a small autoencoder (scikit-learn MLPRegressor, inputs -> 16 -> 8 -> 16 ->
  inputs) whose reconstruction error grows for patterns it never saw.

Each score is divided by its own threshold, chosen on held-out normal data for
at most --max-false 1 anomaly per 100 zone-hours after debouncing, and the
anomaly score is the larger of the two: >= 1 means anomalous.

Stage 2, *cause*: a LightGBM multi-class model (normal, fire, nuisance,
sensor fault) over the agent's full feature vector plus the anomaly score
says what the anomaly most likely is. The hybrid detector
(building_agent.HybridDetector, DETECTOR=hybrid) uses both on top of the
fixed-limit rules: an anomaly whose cause is fire raises the alarm (and
releases water when the classifier is confident), any other anomaly raises a
"check zone" alert instead of water. The rules' own decisions stay in force.

Inputs are per zone, without neighbours (an anomaly is about the zone
itself): the zone's signal levels, rates, excesses, maxima and spreads, plus
the sensor-health flags (a faulty signal is blanked and its flag is 0, which a
normal building never shows). Trained by train_anomaly.py.
"""
import json
import math
import os
import pickle

import features as feature_lib

CLASSES = ["normal", "fire", "nuisance", "fault"]
INPUTS = feature_lib.ROOM_FEATURES + feature_lib.HEALTH_FEATURES
_INDEX = {n: i for i, n in enumerate(feature_lib.FEATURE_NAMES)}
INPUT_INDEX = [_INDEX[n] for n in INPUTS]
DEFAULT_PATH = os.environ.get("ANOMALY_MODEL_PATH", "/models/anomaly.pkl")


def label(fire, nuisance, fault):
    """Training class of one zone-tick (fire wins, then fault, then nuisance)."""
    if fire:
        return 1
    if fault:
        return 3
    if nuisance:
        return 2
    return 0


class AnomalyModel:
    """Scaler + isolation forest + autoencoder (+ optional cause model)."""

    def __init__(self):
        self.fill = None          # per-input value used for NaN (normal median)
        self.mean = None
        self.scale = None
        self.forest = None
        self.autoencoder = None
        self.forest_threshold = None
        self.ae_threshold = None
        self.cause = None         # lightgbm.Booster (multi-class) or None
        self.cause_fire_release = None
        self.meta = {}

    # -- preprocessing -------------------------------------------------------

    def inputs(self, X):
        """Full feature matrix (FEATURE_NAMES order) -> scaled model inputs."""
        import numpy as np

        A = np.asarray(X, dtype=np.float64)[:, INPUT_INDEX]
        A = np.where(np.isnan(A), self.fill, A)
        return (A - self.mean) / self.scale

    def fit_scaler(self, X_normal):
        import numpy as np

        A = np.asarray(X_normal, dtype=np.float64)[:, INPUT_INDEX]
        self.fill = np.nanmedian(A, axis=0)
        self.fill = np.where(np.isnan(self.fill), 0.0, self.fill)
        A = np.where(np.isnan(A), self.fill, A)
        self.mean = A.mean(axis=0)
        # Robust spread with a floor: features that never move in a normal
        # building (health flags) must not divide by ~0.
        q75, q25 = np.percentile(A, [75, 25], axis=0)
        self.scale = np.maximum(np.maximum(q75 - q25, A.std(axis=0)), 1e-2)

    # -- raw scores ----------------------------------------------------------

    def raw_scores(self, X):
        """(isolation forest score, autoencoder error); higher = more unusual."""
        import numpy as np

        Z = self.inputs(X)
        Z = np.clip(Z, -50.0, 50.0)
        forest = -self.forest.score_samples(Z)
        recon = self.autoencoder.predict(Z)
        error = np.mean((recon - Z) ** 2, axis=1)
        return forest, error

    def scores(self, X):
        """Anomaly score per row: >= 1 is anomalous."""
        import numpy as np

        forest, error = self.raw_scores(X)
        return np.maximum(forest / self.forest_threshold, error / self.ae_threshold)

    def causes(self, X, scores):
        """Class probabilities per row (CLASSES order), or None."""
        import numpy as np

        if self.cause is None:
            return None
        A = np.column_stack([np.asarray(X, dtype=np.float64), scores])
        return self.cause.predict(A)

    # -- per-zone use in the agent ---------------------------------------------

    def assess(self, room_features):
        """{zone: (anomaly score, {class: probability} or None)}."""
        import numpy as np

        zones = list(room_features)
        if not zones:
            return {}
        X = np.array([feature_lib.vector(room_features[z]) for z in zones], dtype=np.float64)
        s = self.scores(X)
        p = self.causes(X, s)
        out = {}
        for i, z in enumerate(zones):
            probs = None if p is None else {c: float(p[i][k]) for k, c in enumerate(CLASSES)}
            out[z] = (float(s[i]), probs)
        return out

    # -- persistence --------------------------------------------------------------

    def save(self, path):
        cause_text = self.cause.model_to_string() if self.cause is not None else None
        state = dict(self.__dict__, cause=None)
        with open(path, "wb") as f:
            pickle.dump({"state": state, "cause": cause_text}, f)
        with open(os.path.splitext(path)[0] + ".json", "w", encoding="utf-8") as f:
            json.dump(dict(self.meta, inputs=INPUTS, classes=CLASSES,
                           forest_threshold=float(self.forest_threshold),
                           ae_threshold=float(self.ae_threshold),
                           cause_fire_release=self.cause_fire_release), f, indent=2)

    @classmethod
    def load(cls, path=None):
        path = path or DEFAULT_PATH
        with open(path, "rb") as f:
            blob = pickle.load(f)  # noqa: S301 - our own model file
        model = cls()
        model.__dict__.update(blob["state"])
        if model.meta.get("features") != feature_lib.FEATURE_NAMES:
            raise ValueError("anomaly model trained with another feature set; retrain it")
        if blob.get("cause"):
            import lightgbm

            model.cause = lightgbm.Booster(model_str=blob["cause"])
        return model


def top_cause(probs):
    if not probs:
        return None, 0.0
    cause = max(probs, key=probs.get)
    return cause, probs[cause]


def finite(value, default=0.0):
    return value if isinstance(value, (int, float)) and math.isfinite(value) else default
