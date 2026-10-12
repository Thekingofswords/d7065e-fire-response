"""Sensor gateway: the fire detectors of many rooms, published over MQTT.

The fire simulator writes the *true* physical state of every zone into
BuildSim as room layers. A gateway plays the part of the physical
instruments in every room and corridor it serves (one gateway per floor by
default, like a real Zigbee/LoRa/BACnet gateway): each tick it reads the
environment from BuildSim (one GET /api/room-layers), passes it through a
model of how each real device responds and publishes one reading per zone
on building/<level>/<room>/sensors. Zones missing from the layers are at
ambient conditions.

Instrument models (per zone):

* Heat detector / temperature sensor: thermal lag of the sensing element,
  dT_d/dt = sqrt(u) / RTI * (T_gas - T_d)  (Response Time Index model used
  for sprinkler bulbs and heat detectors; u = ceiling-jet velocity).
* Photoelectric smoke detector: smoke has to drift into the chamber, a first
  order lag with time constant L / u (Heskestad characteristic length L).
* Electrochemical CO cell and NDIR CO2 sensor: first-order response
  (T90 of about one and two minutes respectively).
* CO2 from occupants: a well-mixed room balance between people breathing and
  ventilation, on top of the fire's CO2 (people counted from BuildSim's
  moving entities and occupancy).
* Every reading has noise, a finite resolution and a measuring range.
* Sensor faults belong to the device, not the world: the gateway injects
  them itself, following the fault schedule of the current scenario
  (sim/scenario, with the shared clock on sim/clock): a drifting smoke
  chamber, a frozen temperature reading, spikes in the smoke reading, a dead
  CO cell, or a device that stops reporting altogether.

The gateway's MQTT last will marks it offline on
building/gateways/<name>/status if it crashes, so the agent knows every
room it served has gone dark.

Settings: GATEWAY_NAME, GATEWAY_LEVELS (e.g. "level0"; default all floors),
TICK_SECONDS, SENSOR_SCOPE (rooms or rooms+corridors, see topology.py).
"""
import math
import os
import random
import time

import common
import lifecycle
import mqtt_bus
import scenarios
import topology


def env_float(name, default):
    return float(os.environ.get(name, str(default)))


INTERVAL = env_float("TICK_SECONDS", 2.0)
# Real seconds without a successful environment read before the gateway
# stops publishing and reports itself degraded (its readings would be stale).
ENV_STALE_SECONDS = env_float("ENV_STALE_SECONDS", 10.0)
SUBSTEP_S = 1.0  # simulated seconds per integration step

# Heat detector. RTI in (m s)^0.5: <50 fast response, 80-350 standard.
HEAT_RTI = env_float("HEAT_DETECTOR_RTI", 50.0)
TEMP_RANGE = (-20.0, env_float("TEMP_SENSOR_MAX_C", 150.0))
TEMP_NOISE = 0.1

# Photoelectric smoke detector.
SMOKE_CHAR_LENGTH_M = env_float("SMOKE_DETECTOR_LENGTH_M", 1.8)
SMOKE_RANGE = (0.0, env_float("SMOKE_SENSOR_MAX_PCT_M", 30.0))
SMOKE_NOISE = 0.05

# Electrochemical CO cell.
CO_TAU_S = env_float("CO_SENSOR_TAU_SECONDS", 25.0)
CO_RANGE = (0.0, env_float("CO_SENSOR_MAX_PPM", 1000.0))
CO_NOISE = 1.0

# NDIR CO2 sensor.
CO2_TAU_S = env_float("CO2_SENSOR_TAU_SECONDS", 50.0)
CO2_RANGE = (0.0, env_float("CO2_SENSOR_MAX_PPM", 10000.0))
CO2_NOISE = 10.0

# Sensor faults.
SMOKE_DRIFT_PCT_M_PER_H = 6.0     # creep of a dirty smoke chamber
SMOKE_SPIKE_PROBABILITY = 0.08    # per reading
FAULT_SMOKE_DRIFT = 1
FAULT_TEMP_STUCK = 2
FAULT_SMOKE_SPIKES = 3
FAULT_CO_DEAD = 4
FAULT_OFFLINE = 5

# CO2 exhaled per person, m3/s (~0.31 L/min for office work).
CO2_PER_PERSON_M3S = 5.2e-6
SMOKE_EXTRACT_M3S = env_float("SMOKE_EXTRACT_M3S", 1.0)


PERSON_ENTITY_TYPES = {"man": 1, "woman": 1, "group": 4}

PHYSICAL_LAYERS = (common.LAYER_GAS_TEMP, common.LAYER_JET_VELOCITY,
                   common.LAYER_OBSCURATION, common.LAYER_CO, common.LAYER_CO2)


def read_environment(zones):
    """{zone key: {layer: value}} from BuildSim's room layers, for the zones
    this gateway serves. Raises if BuildSim cannot be read."""
    truth = {}
    for layer in common.call("GET", "/api/room-layers") or []:
        layer_id = layer.get("id")
        if layer_id not in PHYSICAL_LAYERS:
            continue
        for k, value in (layer.get("values") or {}).items():
            if k in zones:
                try:
                    truth.setdefault(k, {})[layer_id] = float(value)
                except (TypeError, ValueError):
                    pass
    return truth


def read_people():
    """People per zone key from BuildSim's moving entities and occupancy
    (one request each per tick for the whole gateway)."""
    people = {}
    try:
        for entity in common.call("GET", "/api/entities") or []:
            if entity.get("room"):
                k = f"{entity.get('level', common.LEVEL)}/{entity['room']}"
                people[k] = people.get(k, 0) + PERSON_ENTITY_TYPES.get(entity.get("type"), 0)
    except Exception:  # noqa: BLE001
        pass
    try:
        for k, occ in (common.call("GET", "/api/occupancy") or {}).items():
            people[k] = people.get(k, 0) + len(occ.get("persons", []))
    except Exception:  # noqa: BLE001
        pass
    return people


def clamp(value, minimum, maximum):
    return min(max(value, minimum), maximum)


def lag(current, target, dt, tau):
    """Exact first-order lag step."""
    return target + (current - target) * math.exp(-dt / max(tau, 1e-6))


class Instruments:
    """Internal state of one room's physical sensing elements.

    Also used by generate_dataset.py, so offline and live data come from the
    same instrument model.
    """

    def __init__(self, room=None, rng=None, volume=None):
        self.room = room or common.ROOM
        self.volume = volume or common.room_volume(self.room)
        self.rng = rng or random.Random()
        self.fault = 0
        self.fault_age = 0.0
        self.stuck_temperature = None
        self.offline_readings = None
        self.last_readings = None
        self.heat_element = common.AMBIENT_TEMP_C
        self.smoke_chamber = 0.0
        self.co_cell = 0.0
        self.co2_cell = common.AMBIENT_CO2_PPM
        self.occupant_co2 = 0.0  # ppm above background from people

    def step(self, truth, dt, people, damper):
        """Advance the instruments by dt simulated seconds."""
        gas_temp = truth.get(common.LAYER_GAS_TEMP, common.AMBIENT_TEMP_C)
        velocity = max(truth.get(common.LAYER_JET_VELOCITY, 0.1), 0.1)
        obscuration = truth.get(common.LAYER_OBSCURATION, 0.0)
        co = truth.get(common.LAYER_CO, 0.0)
        fire_co2 = truth.get(common.LAYER_CO2, common.AMBIENT_CO2_PPM)

        volume = self.volume
        ventilation = (
            common.VENTILATION_ACH * volume / 3600.0
            + damper * SMOKE_EXTRACT_M3S
        )

        steps = max(1, int(math.ceil(dt / SUBSTEP_S)))
        h = dt / steps
        for _ in range(steps):
            self.heat_element = lag(
                self.heat_element, gas_temp, h, HEAT_RTI / math.sqrt(velocity)
            )
            self.smoke_chamber = lag(
                self.smoke_chamber, obscuration, h,
                SMOKE_CHAR_LENGTH_M / velocity,
            )
            self.co_cell = lag(self.co_cell, co, h, CO_TAU_S)

            # People breathing vs. ventilation, well mixed.
            self.occupant_co2 += h * (
                CO2_PER_PERSON_M3S * people * 1e6 / volume
                - ventilation / volume * self.occupant_co2
            )
            self.co2_cell = lag(
                self.co2_cell, fire_co2 + self.occupant_co2, h, CO2_TAU_S
            )

        fault = int(truth.get(common.LAYER_SENSOR_FAULT, 0))
        if fault != self.fault:
            self.fault = fault
            self.fault_age = 0.0
            self.stuck_temperature = None
            self.offline_readings = None
        else:
            self.fault_age += dt

    def _reading(self, value, noise, value_range):
        return clamp(value + self.rng.gauss(0.0, noise), *value_range)

    def readings(self):
        temperature = self._reading(self.heat_element, TEMP_NOISE, TEMP_RANGE)
        smoke = self.smoke_chamber
        co = self._reading(self.co_cell, CO_NOISE, CO_RANGE)

        if self.fault == FAULT_OFFLINE:
            # Nothing new is published; consumers keep seeing the last value.
            if self.offline_readings is None:
                self.offline_readings = self.last_readings
            if self.offline_readings is not None:
                return dict(self.offline_readings)
        if self.fault == FAULT_TEMP_STUCK:
            if self.stuck_temperature is None:
                self.stuck_temperature = temperature
            temperature = self.stuck_temperature
        elif self.fault == FAULT_SMOKE_DRIFT:
            smoke += SMOKE_DRIFT_PCT_M_PER_H * self.fault_age / 3600.0
        elif self.fault == FAULT_SMOKE_SPIKES:
            if self.rng.random() < SMOKE_SPIKE_PROBABILITY:
                smoke += self.rng.uniform(5.0, 25.0)
        elif self.fault == FAULT_CO_DEAD:
            co = 0.0

        values = {
            "temperature": temperature,
            "smoke": self._reading(smoke, SMOKE_NOISE, SMOKE_RANGE),
            "co": co,
            "co2": self._reading(self.co2_cell, CO2_NOISE, CO2_RANGE),
        }
        self.last_readings = values
        return values

    @property
    def offline(self):
        return self.fault == FAULT_OFFLINE


def main():
    common.wait_until_healthy()   # BuildSim serves the floor plans

    levels = [lv.strip() for lv in os.environ.get("GATEWAY_LEVELS", "").split(",") if lv.strip()]
    name = os.environ.get("GATEWAY_NAME") or "sensors-" + ("-".join(levels) or "all")
    topo = topology.Topology.load(common.BASE)
    zones = topo.sensor_zones(levels or None)
    instruments = {
        k: Instruments(topo.zones[k].name, random.Random(hash((name, k))), topo.zones[k].volume)
        for k in zones
    }
    zone_set = set(zones)
    meta = {"scenario": None, "clock": {}}

    def on_scenario(topic, payload):
        if isinstance(payload, dict):
            meta["scenario"] = payload

    def on_clock(topic, payload):
        if isinstance(payload, dict):
            meta["clock"] = payload

    life = lifecycle.Lifecycle(name, status_topic=mqtt_bus.gateway_status_topic(name))
    bus = life.bus
    bus.subscribe(mqtt_bus.SCENARIO_TOPIC, on_scenario)
    bus.subscribe(mqtt_bus.CLOCK_TOPIC, on_clock, qos=0)
    life.start()
    print(f"gateway {name}: {len(zones)} detectors on {levels or 'all floors'} "
          f"(time scale x{common.TIME_SCALE})", flush=True)

    last = time.monotonic()
    tick = 0
    truth = {}
    read_failures = 0
    last_read = time.monotonic()
    while not life.stopping:
        try:
            life.beat()
            try:
                truth = read_environment(zone_set)
                read_failures = 0
                last_read = time.monotonic()
                if life.state == lifecycle.SYNCING:
                    life.running(f"{len(zones)} detectors")
                else:
                    life.ok()
            except Exception as exc:  # noqa: BLE001
                read_failures += 1
                if read_failures % 10 == 1:
                    print(f"{name}: cannot read the environment from BuildSim: {exc}",
                          flush=True)
                if time.monotonic() - last_read > ENV_STALE_SECONDS:
                    # Publishing would pass stale values off as fresh ones:
                    # go quiet and say so. The agent treats a degraded
                    # gateway's zones as offline.
                    life.degraded(f"cannot read the environment from BuildSim: {exc}")
                    life.sleep(INTERVAL)
                    continue
            scenario, clock = meta["scenario"], meta["clock"]
            scenario_time = float(clock.get("scenario_time") or 0.0)
            if scenario and clock.get("scenario_id") not in (None, scenario.get("id")):
                scenario = None   # clock already belongs to the next scenario
            people = read_people()
            now = time.monotonic()
            dt = (now - last) * common.TIME_SCALE
            last = now
            stamp = time.time()
            offline = 0
            for k, instrument in instruments.items():
                env = dict(truth.get(k, {}))
                if scenario:
                    env[common.LAYER_SENSOR_FAULT] = scenarios.active_fault(
                        scenario, k, scenario_time)
                instrument.step(env, dt, people.get(k, 0), 0.0)
                values = instrument.readings()
                if instrument.offline:
                    offline += 1   # simulated dead device: publishes nothing
                    continue
                level, room = topology.split(k)
                bus.publish(mqtt_bus.sensors_topic(level, room), {
                    "ts": stamp, "gateway": name,
                    "temperature": round(values["temperature"], 1),
                    "smoke": round(values["smoke"], 2),
                    "co": round(values["co"], 0),
                    "co2": round(values["co2"], 0),
                }, qos=0, retain=True)
            tick += 1
            if tick % 15 == 1 or offline:
                print(f"{name}: published {len(instruments) - offline} readings, "
                      f"{len(truth)} zones with simulated hazards, {offline} offline (fault)",
                      flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"gateway tick failed: {exc}", flush=True)

        life.sleep(INTERVAL)
    life.stop()


if __name__ == "__main__":
    main()
