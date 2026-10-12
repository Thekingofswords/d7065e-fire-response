"""Anomaly detection and the hybrid detector (anomaly.py, D2).

Uses the trained model in ../models/anomaly.pkl (train_anomaly.py) or
ANOMALY_MODEL_PATH; skipped when there is none."""
import os

import pytest

import building_agent as ba
from helpers import reading

ROOMS = ["level0/fire", "level0/next", "level0/far", "level0/cook"]
NEIGHBOURS = {"level0/fire": ["level0/next"], "level0/next": ["level0/fire", "level0/far"],
              "level0/far": ["level0/next"], "level0/cook": []}
MODEL = os.environ.get("ANOMALY_MODEL_PATH") or os.path.join(
    os.path.dirname(__file__), "..", "..", "models", "anomaly.pkl")


@pytest.fixture(scope="module")
def detector():
    if not os.path.exists(MODEL):
        pytest.skip("no trained anomaly model (run train_anomaly.py)")
    return ba.HybridDetector(MODEL)


def run(detector, frames, stale_frames=None, dt=10.0):
    """Feed frames {room: reading} (missing rooms are quiet); returns the
    assessment and decisions of every tick."""
    p = ba.BuildingPolicy(ROOMS, NEIGHBOURS, detector)
    out = []
    for i, frame in enumerate(frames):
        t = i * dt
        full = {r: frame.get(r, reading(temperature=21.0 + (i % 3) * 0.02, co2=420 + i % 2))
                for r in ROOMS}
        stale = (stale_frames or {}).get(i)
        d = p.step(t, full, stale)
        out.append((dict(p.last_assessment), d))
    return out


def test_normal_building_is_not_anomalous(detector):
    ticks = run(detector, [{}] * 60)
    for assessment, decisions in ticks[35:]:
        assert all(a["anomaly"] < 1.0 for a in assessment.values()), \
            {r: a["anomaly"] for r, a in assessment.items()}
        assert not any(d.get("check") for d in decisions.values())


def test_fire_is_anomalous_and_classified_as_fire(detector):
    frames = [{}] * 35 + [{"level0/fire": reading(temperature=21 + 4 * i, smoke=1.5 * i,
                                                    co=12 * i)} for i in range(1, 20)]
    ticks = run(detector, frames)
    hits = [a["level0/fire"] for a, _ in ticks[35:] if a["level0/fire"]["anomaly"] >= 1.0]
    assert hits, "a growing fire must be anomalous"
    assert any(h["cause"] == "fire" for h in hits)
    assert ticks[-1][1]["level0/fire"]["state"] in ("ALARM", "SUPPRESSING")


def test_cooking_is_anomalous_but_gets_no_water(detector):
    frames = [{}] * 35 + [{"level0/cook": reading(temperature=23.5, smoke=4.0 + (i % 3),
                                                    co=2.0)} for i in range(15)]
    ticks = run(detector, frames)
    assert any(a["level0/cook"]["anomaly"] >= 1.0 for a, _ in ticks[35:])
    assert not any(d["level0/cook"]["sprinkler"] for _, d in ticks)


def test_dead_sensor_is_anomalous(detector):
    stale = {i: {"level0/far": {"temperature", "smoke", "co", "co2"}} for i in range(35, 60)}
    ticks = run(detector, [{}] * 60, stale_frames=stale)
    assert any(a["level0/far"]["anomaly"] >= 1.0 for a, _ in ticks[40:])
    assert not any(d["level0/far"]["sprinkler"] for _, d in ticks)


def test_check_zone_alert():
    decisions = {"level0/A109": {"state": "NORMAL", "sprinkler": False, "damper": 0.0,
                                 "p_fire": 0.0, "reason": "", "faults": {}, "anomaly": 2.5,
                                 "cause": "nuisance", "check": "likely a nuisance source"}}
    alerts = ba.build_alerts(decisions, {"level0/A109": reading(smoke=4)})
    assert alerts[0]["id"] == "check-level0-A109" and alerts[0]["severity"] == "warning"
