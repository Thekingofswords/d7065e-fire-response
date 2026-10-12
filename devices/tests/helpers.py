"""Small builders shared by the tests."""
import math


def scenario(fires=(), incipient=0.0, **overrides):
    """A fire scenario dict (see scenarios.py) without environment variables."""
    s = {
        "id": 1, "kind": "test", "duration": math.inf,
        "alpha": 0.0469, "growth": "fast", "fuel_load": 420.0,
        "external_doors": 0, "ach": 2.0,
        "door_open_prob": 1.0, "door_seed": 1, "open_doors": [],
        "fires": [{"room": r, "start": 0.0, "incipient": incipient} for r in fires],
        "nuisances": [], "sensor_faults": [],
    }
    s.update(overrides)
    return s


def reading(temperature=21.0, smoke=0.0, co=0.0, co2=420.0):
    return {"temperature": temperature, "smoke": smoke, "co": co, "co2": co2}
