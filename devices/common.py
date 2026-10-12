"""Tiny BuildSim REST helper (standard library only).

Shared by every device process (sensor, actuator, agent) and by the fire
simulator, so all of them agree on room geometry, ambient conditions, the
simulation time scale and the names of the physical room layers.
"""
import json
import os
import time
import urllib.error
import urllib.request

# Inside Compose the API is at http://buildsim:9090, NOT 127.0.0.1.
BASE = os.environ.get("BUILDSIM_URL", "http://buildsim:9090").rstrip("/")

LEVEL = os.environ.get("LEVEL", "level0")
ROOM = os.environ.get("ROOM", "A109")
TEMP_ID = f"{ROOM}-temp"
CO2_ID = f"{ROOM}-co2"
CO_ID = f"{ROOM}-co"
SMOKE_ID = f"{ROOM}-smoke"
DAMPER_ID = f"{ROOM}-damper"
SETPOINT_ID = f"{ROOM}-sprinkler"
SPRINKLER_ID = SETPOINT_ID
OCCUPANCY_KEY = f"{LEVEL}/{ROOM}"

FIRE_EFFECT_ID = f"fire-{ROOM}"
SMOKE_EFFECT_ID = f"smoke-{ROOM}"


def _env_float(name, default):
    return float(os.environ.get(name, str(default)))


# ---------------------------------------------------------------------------
# Shared simulation settings
# ---------------------------------------------------------------------------

# Simulated seconds that pass per real second. 1.0 = real time. A real fire
# takes several minutes to develop, so a demo typically speeds it up.
# Every process must use the same value (set it once in compose).
TIME_SCALE = _env_float("TIME_SCALE", 5.0)

AMBIENT_TEMP_C = _env_float("AMBIENT_TEMP_C", 21.0)
AMBIENT_CO2_PPM = _env_float("AMBIENT_CO2_PPM", 420.0)
AMBIENT_O2_PCT = 20.95

# Names of the room layers published by fire_simulator.py ("simulation
# truth"). Sensors read these and add their own instrument behaviour.
LAYER_HRR = "fire_hrr"                      # kW
LAYER_GAS_TEMP = "gas_temperature"          # degC at ceiling/detector height
LAYER_JET_VELOCITY = "ceiling_jet_velocity"  # m/s at detector
LAYER_OBSCURATION = "smoke_obscuration"     # %/m
LAYER_VISIBILITY = "visibility"             # m
LAYER_CO = "co"                             # ppm
LAYER_CO2 = "co2"                           # ppm
LAYER_O2 = "o2"                             # vol-%
LAYER_SMOKE_LAYER_HEIGHT = "smoke_layer_height"  # m above floor

# Ground-truth labels, also published as room layers (see scenarios.py for
# the numeric codes). Used for recording training data, never by sensors
# or agents.
LAYER_FIRE_PHASE = "fire_phase"
LAYER_FIRE_ORIGIN = "fire_origin"
LAYER_NUISANCE = "nuisance_source"
LAYER_SENSOR_FAULT = "sensor_fault"
LAYER_SCENARIO_ID = "scenario_id"
LAYER_SCENARIO_TIME = "scenario_time"


# ---------------------------------------------------------------------------
# Room geometry
# ---------------------------------------------------------------------------

def _parse_room_map(name):
    """Parse e.g. ROOM_AREAS="A109:30,A110:20" into {"A109": 30.0, ...}."""
    result = {}
    for item in os.environ.get(name, "").split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        key, value = item.rsplit(":", 1)
        try:
            result[key.strip()] = float(value)
        except ValueError:
            print(f"ignoring invalid {name} entry: {item}", flush=True)
    return result


DEFAULT_ROOM_AREA_M2 = _env_float("ROOM_AREA_M2", 25.0)
CEILING_HEIGHT_M = _env_float("CEILING_HEIGHT_M", 2.7)
DOOR_WIDTH_M = _env_float("DOOR_WIDTH_M", 0.9)
DOOR_HEIGHT_M = _env_float("DOOR_HEIGHT_M", 2.0)
# Background mechanical ventilation, air changes per hour.
VENTILATION_ACH = _env_float("VENTILATION_ACH", 2.0)

_ROOM_AREAS = _parse_room_map("ROOM_AREAS")


def room_area(room):
    return _ROOM_AREAS.get(room, DEFAULT_ROOM_AREA_M2)


def room_volume(room):
    return room_area(room) * CEILING_HEIGHT_M


def parse_rooms():
    """Rooms of the building, from ROOMS (e.g. "A109,A110")."""
    return [
        room.strip()
        for room in os.environ.get("ROOMS", "A109,A110").split(",")
        if room.strip()
    ]


def parse_connections(rooms):
    """Open doors between rooms, from CONNECTIONS (e.g. "A109-A110")."""
    connections = []
    for connection in os.environ.get("CONNECTIONS", "A109-A110").split(","):
        connection = connection.strip()
        if not connection:
            continue
        try:
            room_a, room_b = (part.strip() for part in connection.split("-", 1))
            if room_a not in rooms or room_b not in rooms:
                raise ValueError("room not in ROOMS")
            connections.append((room_a, room_b))
        except ValueError:
            print(f"Invalid connection: {connection}", flush=True)
    return connections


def neighbour_map(rooms, connections):
    """{room: [rooms sharing a door with it]}."""
    result = {room: [] for room in rooms}
    for room_a, room_b in connections:
        result[room_a].append(room_b)
        result[room_b].append(room_a)
    return result


def room_key(room, level=None):
    """Canonical BuildSim room key, e.g. "level0/A109"."""
    if "/" in room:
        return room
    return f"{level or LEVEL}/{room}"


def split_room(room):
    """"level1/B201" -> ("level1", "B201"); "A109" -> (LEVEL, "A109")."""
    if "/" in room:
        level, name = room.split("/", 1)
        return level, name
    return LEVEL, room


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------

def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def wait_until_healthy():
    """BuildSim keeps state in memory, so always retry instead of crashing."""
    while True:
        try:
            call("GET", "/healthz")
            return
        except Exception as exc:  # noqa: BLE001
            print(f"waiting for BuildSim at {BASE}: {exc}", flush=True)
            time.sleep(2)


def register_equipment():
    """Idempotent: safe to call again after BuildSim restarts (state is wiped).

    BuildSim skips equipment that already exists, so this never resets the
    values of a running sensor.
    """
    equipment = [
        {
            "id": f"fire_control-{ROOM}",
            "name": f"Fire Control {ROOM}",
            "type": "fire_safety",
            "category": "fire_safety",
            "level": LEVEL,
            "room": ROOM,
            "status": "running",
            "sensors": [
                {"id": TEMP_ID, "name": "Heat detector", "type": "temperature",
                 "data_type": "text", "unit": "°C",
                 "value": f"{AMBIENT_TEMP_C:.1f}"},
                {"id": SMOKE_ID, "name": "Smoke detector (photoelectric)",
                 "type": "smoke_obscuration", "data_type": "text",
                 "unit": "%/m", "value": "0.00"},
                {"id": CO_ID, "name": "CO (electrochemical)", "type": "co",
                 "data_type": "text", "unit": "ppm", "value": "0"},
                {"id": CO2_ID, "name": "CO2 (NDIR)", "type": "co2",
                 "data_type": "text", "unit": "ppm",
                 "value": f"{AMBIENT_CO2_PPM:.0f}"},
            ],
            "actuators": [
                {"id": SETPOINT_ID, "name": "sprinkler", "type": "sprinkler_status", "state": "off"},
                #{"id": DAMPER_ID, "name": "Ventilation damper", "type": "fan_speed", "state": "0"},
            ],
        }
    ]
    return call("POST", "/api/equipment/bulk", equipment)


def read_sensor(sensor_id):
    """Read a sensor from BuildSim."""
    return call(
        "GET",
        f"/api/sensors/{sensor_id}",
    )


def read_sensor_value(sensor_id, default=0.0):
    """Read a numeric sensor value."""
    try:
        sensor = read_sensor(sensor_id)
        return float(sensor["value"])
    except Exception as exc:  # noqa: BLE001
        print(
            f"failed to read sensor {sensor_id}: {exc}",
            flush=True,
        )
        return default


def set_sensor(sensor_id, value):
    return call("PUT", f"/api/sensors/{sensor_id}/value",
                {
                    "data_type": "text",
                    "value": str(value),
                },
                )


def read_actuator_state(actuator_id, default=None):
    """Return an actuator's raw state string, or default if unavailable."""
    try:
        return call("GET", f"/api/actuators/{actuator_id}")["state"]
    except Exception:  # noqa: BLE001
        return default


# ---------------------------------------------------------------------------
# Room layers (physical state published by the fire simulator)
# ---------------------------------------------------------------------------

def get_room_layers():
    """Return the current room-layer snapshot (list), or [] on failure."""
    try:
        return call("GET", "/api/room-layers") or []
    except Exception as exc:  # noqa: BLE001
        print(f"failed to read room layers: {exc}", flush=True)
        return []


def set_room_layers(layers):
    return call("PUT", "/api/room-layers", layers)


def room_layer_values(layers, room=ROOM, level=LEVEL):
    """Return {layer_id: value} for one room from a room-layer snapshot."""
    key = room_key(room, level)
    suffix = "/" + room
    result = {}
    for layer in layers or []:
        values = layer.get("values") or {}
        if key in values:
            result[layer.get("id")] = float(values[key])
            continue
        # Be tolerant of a differently canonicalised level prefix.
        for candidate, value in values.items():
            if candidate.endswith(suffix):
                result[layer.get("id")] = float(value)
                break
    return result


# ---------------------------------------------------------------------------
# Effects (visual only)
# ---------------------------------------------------------------------------

def set_effects(effects):
    return call("PUT", "/api/effects", effects,)


def fire_effect(
    intensity,
    radius=7,
    height=15,
    level=LEVEL,
    room=ROOM,
):
    """Build the room's fire effect."""
    return {
        "id": FIRE_EFFECT_ID,
        "type": "fire",
        "label": "Fire source",
        "level": level,
        "room": room,
        "radius": radius,
        "height": height,
        "intensity": max(0.0, min(1.0, intensity)),
    }


def smoke_effect(
    intensity,
    radius=8,
    height=18,
    level=LEVEL,
    room=ROOM
):
    """Build the room's smoke effect."""
    return {
        "id": SMOKE_EFFECT_ID,
        "type": "smoke",
        "level": level,
        "room": room,
        "radius": radius,
        "height": height,
        "intensity": max(0.0, min(1.0, intensity)),
    }


def update_fire_effect(
    fire_intensity,
    smoke_intensity,
    level=LEVEL,
    room=ROOM
):
    """
    Publish the current fire/smoke snapshot.

    BuildSim only animates the primitives. The simulator is responsible
    for changing their intensity over time.
    """
    effects = [
        fire_effect(fire_intensity, level=level, room=room),
        smoke_effect(smoke_intensity, level=level, room=room),
    ]

    return set_effects(effects)


def clear_fire_effects():
    """Remove the fire and smoke effects from BuildSim."""
    return set_effects([])


def get_effects():
    """Return the current BuildSim effect snapshot."""
    return call(
        "GET",
        "/api/effects",
    )


def get_room_effect(effects, room, effect_type):
    """Find an effect of a given type belonging to a room."""

    for effect in effects or []:
        if (
            effect.get("room") == room
            and effect.get("type") == effect_type
        ):
            return effect

    return None


def get_room_effect_intensity(
    effects,
    room,
    effect_type,
    default=0.0,
):
    """Get a room effect's intensity."""

    effect = get_room_effect(
        effects,
        room,
        effect_type,
    )

    if not effect:
        return default

    try:
        return float(
            effect.get(
                "intensity",
                default,
            )
        )

    except (TypeError, ValueError):
        return default
