"""Frozen and offline sensor detection (health.py)."""
import health
from helpers import reading


def test_frozen_reading_is_flagged_after_the_limit():
    h = health.SensorHealth(["z"])
    faults = {}
    t = 0.0
    while t <= health.FROZEN_SECONDS + 20:
        faults = h.update(t, {"z": reading(temperature=24.3, smoke=0.1 + t * 1e-3)})
        t += 10
    assert faults["z"].get("temperature") == "frozen"
    assert "smoke" not in faults["z"]


def test_values_at_the_end_of_the_range_are_not_frozen():
    h = health.SensorHealth(["z"])
    for t in range(0, 400, 10):
        faults = h.update(float(t), {"z": reading(temperature=21 + (t % 20) * 0.01, smoke=0.0)})
    assert "smoke" not in faults.get("z", {})


def test_stale_signals_are_offline():
    h = health.SensorHealth(["z"])
    faults = h.update(0.0, {"z": reading()}, stale={"z": set(health.SIGNALS)})
    assert set(faults["z"]) == set(health.SIGNALS)
