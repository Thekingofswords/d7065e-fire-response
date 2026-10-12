"""Fire / nuisance / sensor-fault scenarios for the fire simulator.

A scenario is a plain dict, so it can be logged as JSON next to the recorded
data:

    {
      "id": 1730812345,
      "kind": "fire+nuisance",
      "duration": 1800.0,              # simulated seconds
      "alpha": 0.0469,                 # t-squared growth, kW/s2
      "growth": "fast",
      "fuel_load": 420.0,              # MJ/m2
      "external_doors": 1,
      "ach": 2.0,                      # background ventilation
      "door_open_prob": 0.5,           # share of doors standing open
      "door_seed": 1730812345,         # which doors (deterministic)
      "open_doors": ["level0/A109"],   # zones whose doors are all open
      "fires":      [{"room": "A109", "start": 420.0, "incipient": 180.0}],
      "nuisances":  [{"room": "A110", "type": "cooking", "start": 600.0,
                      "duration": 600.0, "strength": 1.3}],
      "sensor_faults": [{"room": "A110", "type": "smoke_drift",
                         "start": 900.0}]
    }

Nuisance sources are the hard negatives a learned detector needs: they make
smoke, heat or CO without a fire.
"""
import math
import os

GROWTH_CLASSES = {
    "slow": 0.00293,
    "medium": 0.01172,
    "fast": 0.0469,
    "ultrafast": 0.1876,
}

# Numeric label codes (published as room layers and recorded as columns).
PHASE_CODES = {
    "idle": 0,
    "smouldering": 1,
    "growing": 2,
    "fully_developed": 3,
    "decaying": 4,
    "extinguished": 5,
    "burnt_out": 6,
}
BURNING_PHASE_CODES = (1, 2, 3, 4)

NUISANCE_CODES = {
    "none": 0,
    "cooking": 1,
    "steam": 2,
    "dust": 3,
    "heater": 4,
    "smoking": 5,
}

FAULT_CODES = {
    "none": 0,
    "smoke_drift": 1,   # dirty smoke chamber, reading creeps up
    "temp_stuck": 2,    # temperature reading frozen
    "smoke_spikes": 3,  # insects / electrical noise in the smoke chamber
    "co_dead": 4,       # CO cell at end of life, reads 0
    "offline": 5,       # sensor process crashed: no new values at all
}

# Physical description of each nuisance source at strength 1.0.
#   aerosol g/s, sigma m2/g (optical extinction per gram), heat kW,
#   co g/s, co2 g/s, duration range in simulated seconds.
NUISANCE_SOURCES = {
    "cooking": dict(aerosol=0.03, sigma=4.0, heat=2.5, co=0.0003, co2=0.10,
                    duration=(300, 1200)),
    "steam": dict(aerosol=0.05, sigma=1.5, heat=1.0, co=0.0, co2=0.0,
                  duration=(300, 900)),
    "dust": dict(aerosol=0.15, sigma=2.5, heat=0.0, co=0.0, co2=0.0,
                 duration=(30, 120)),
    "heater": dict(aerosol=0.0, sigma=0.0, heat=2.5, co=0.0, co2=0.0,
                   duration=(1200, 3600)),
    "smoking": dict(aerosol=0.003, sigma=8.0, heat=0.1, co=0.0008, co2=0.01,
                    duration=(300, 900)),
}

# Quiet time at the start of every random scenario. Sensors settle from the
# previous scenario during it; the training script can skip it.
SETTLE_SECONDS = float(os.environ.get("SCENARIO_SETTLE_SECONDS", "300"))

# Share of random fires that start in a corridor rather than a room.
CORRIDOR_FIRE_SHARE = float(os.environ.get("CORRIDOR_FIRE_SHARE", "0.15"))


# Share of room doors standing open (the rest are closed and leak a little).
DOOR_OPEN_PROB = float(os.environ.get("DOOR_OPEN_PROB", "0.5"))


def door_is_open(scenario, zone_a, zone_b):
    """Whether the door between two zones is open in this scenario. Drawn
    from the scenario's seed, so every process and re-run agrees."""
    import random
    open_doors = scenario.get("open_doors") or ()
    if zone_a in open_doors or zone_b in open_doors:
        return True
    p = scenario.get("door_open_prob", 1.0)
    if p >= 1.0:
        return True
    a, b = sorted((zone_a, zone_b))
    return random.Random(f"{scenario.get('door_seed', scenario.get('id', 0))}|{a}|{b}").random() < p


def _env_float(name, default):
    return float(os.environ.get(name, str(default)))


def _parse_rooms(name, default):
    return [r.strip() for r in os.environ.get(name, default).split(",") if r.strip()]


def fixed_scenario(scenario_id=0):
    """The single scenario described by the environment variables."""
    growth = os.environ.get("FIRE_GROWTH", "fast").strip().lower()
    alpha = GROWTH_CLASSES.get(growth) or float(growth)
    start = _env_float("FIRE_START_DELAY_SECONDS", 0.0)
    incipient = _env_float("INCIPIENT_SECONDS", 120.0)
    return {
        "id": scenario_id,
        "kind": "fixed",
        "duration": math.inf,
        "alpha": alpha,
        "growth": growth,
        "fuel_load": _env_float("FUEL_LOAD_MJ_PER_M2", 420.0),
        "external_doors": int(_env_float("EXTERNAL_DOORS", 1)),
        "ach": _env_float("VENTILATION_ACH", 2.0),
        "door_open_prob": DOOR_OPEN_PROB,
        "door_seed": int(_env_float("DOOR_SEED", 1)),
        # The fire room's door stands open (FIRE_ROOM_DOOR_OPEN=0 to close it).
        "open_doors": [
            room if "/" in room else f"{os.environ.get('LEVEL', 'level0')}/{room}"
            for room in _parse_rooms("START_FIRE_ROOMS", "A109")
        ] if os.environ.get("FIRE_ROOM_DOOR_OPEN", "1") != "0" else [],
        "fires": [
            {"room": room if "/" in room else f"{os.environ.get('LEVEL', 'level0')}/{room}",
             "start": start, "incipient": incipient}
            for room in _parse_rooms("START_FIRE_ROOMS", "A109")
        ],
        "nuisances": [],
        "sensor_faults": [],
    }


def random_scenario(scenario_id, rooms, rng, corridors=()):
    """Draw a randomised scenario for data collection.

    rooms / corridors: zone keys ("level0/A109"). Fires start in a room, or
    in a corridor (CORRIDOR_FIRE_SHARE of fires, e.g. stored rubbish);
    nuisance sources are in rooms; sensor faults anywhere.
    """
    corridors = list(corridors)
    kind = rng.choices(
        ["fire", "nuisance", "fire+nuisance", "quiet", "fault"],
        weights=[0.40, 0.30, 0.10, 0.10, 0.10],
    )[0]

    growth = rng.choices(
        list(GROWTH_CLASSES), weights=[0.2, 0.35, 0.35, 0.1]
    )[0]

    duration = rng.uniform(1500.0, 2700.0)
    scenario = {
        "id": scenario_id,
        "kind": kind,
        "duration": round(duration, 1),
        "alpha": GROWTH_CLASSES[growth],
        "growth": growth,
        "fuel_load": round(rng.uniform(250.0, 800.0), 1),
        # Open windows in rooms (doorways come from the building topology).
        "external_doors": rng.choice([0, 0, 1]),
        "ach": round(rng.uniform(0.5, 4.0), 2),
        "door_open_prob": round(rng.uniform(0.2, 0.9), 2),
        "door_seed": rng.randrange(2**31),
        "open_doors": [],
        "fires": [],
        "nuisances": [],
        "sensor_faults": [],
    }

    if "fire" in kind:
        smoulder = rng.random() < 0.6
        in_corridor = corridors and rng.random() < CORRIDOR_FIRE_SHARE
        scenario["fires"].append(
            {
                "room": rng.choice(corridors if in_corridor else rooms),
                "start": round(rng.uniform(SETTLE_SECONDS, SETTLE_SECONDS + 600.0), 1),
                "incipient": round(rng.uniform(60.0, 900.0), 1) if smoulder else 0.0,
            }
        )
        if rng.random() < 0.7:
            scenario["open_doors"].append(scenario["fires"][0]["room"])

    if "nuisance" in kind:
        for _ in range(rng.choice([1, 1, 2])):
            source = rng.choice(list(NUISANCE_SOURCES))
            low, high = NUISANCE_SOURCES[source]["duration"]
            scenario["nuisances"].append(
                {
                    "room": rng.choice(rooms),
                    "type": source,
                    "start": round(rng.uniform(SETTLE_SECONDS, duration - 300.0), 1),
                    "duration": round(rng.uniform(low, high), 1),
                    "strength": round(rng.uniform(0.5, 2.0), 2),
                }
            )

    if kind == "fault" or rng.random() < 0.05:
        fault = rng.choice([f for f in FAULT_CODES if f != "none"])
        scenario["sensor_faults"].append(
            {
                "room": rng.choice(rooms + corridors),
                "type": fault,
                "start": round(rng.uniform(SETTLE_SECONDS, duration * 0.6), 1),
            }
        )
    elif scenario["fires"] and rng.random() < 0.15:
        # A broken sensor in the room that then catches fire: the case a
        # detector most needs to survive.
        fire = scenario["fires"][0]
        scenario["sensor_faults"].append(
            {
                "room": fire["room"],
                "type": rng.choice([f for f in FAULT_CODES if f != "none"]),
                "start": round(rng.uniform(SETTLE_SECONDS * 0.5, fire["start"]), 1),
            }
        )

    return scenario


def to_json(scenario):
    """JSON-safe copy (an endless fixed scenario has duration infinity)."""
    out = dict(scenario)
    if not math.isfinite(out.get("duration", 0)):
        out["duration"] = None
    return out


def active_nuisances(scenario, room, time_s):
    """Nuisance sources active in a room at a simulated time."""
    return [
        n for n in scenario["nuisances"]
        if n["room"] == room and n["start"] <= time_s < n["start"] + n["duration"]
    ]


def active_fault(scenario, room, time_s):
    """Code of the sensor fault active in a room (0 = none)."""
    for fault in scenario["sensor_faults"]:
        if fault["room"] == room and time_s >= fault["start"]:
            return FAULT_CODES[fault["type"]]
    return 0
