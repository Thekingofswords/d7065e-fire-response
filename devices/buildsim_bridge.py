"""Bridge from the MQTT bus into BuildSim: observations and alerts.

* registers fire-detection equipment for every zone with sensors
  (temperature, smoke, CO, CO2), again whenever BuildSim has restarted and
  lost its in-memory state
* mirrors readings from building/+/+/sensors into BuildSim sensor values
  (corrupt or duplicate messages are dropped, see messages.py)
* mirrors the agent's alert list
* draws smoke spread as global coverage zones (PUT /api/coverage, the bridge
  is its only writer): one translucent disc per zone whose *measured* smoke
  is at least COVERAGE_ON_SMOKE %/m (off again below COVERAGE_OFF_SMOKE), its
  radius growing with the smoke up to the room's size, grey to near-black and
  more opaque as the smoke thickens, so the operator sees what the system
  sees. Refreshed every COVERAGE_SECONDS when it changes; cleared when the
  smoke has gone. (Room colours, red = fire, orange = risk, green = safe,
  are the session highlights written by the occupants service from the
  agent's evacuation plan.)

Actuator state is NOT mirrored here: the actuator gateway owns the
fire-suppression equipment and writes its state to BuildSim itself, and the
fire simulator reads it back from BuildSim, so the control loop closes
through BuildSim.

BuildSim updates one sensor value per HTTP request, and a building has
thousands of sensors, so readings are mirrored selectively: only when a
value moved by more than its deadband (bigger than the sensor noise), or
when it has not been refreshed for REFRESH_SECONDS. A value that drifts by
less than the deadband (smoke clearing slowly) is still sent once it is
SETTLE_BAND away from the shown value and that is SETTLE_SECONDS old, so
the viewer never keeps showing stale smoke. Requests go out with a
small thread pool and at most MAX_REQUESTS_PER_SECOND. The MQTT bus and the
database always have every reading; BuildSim is only the display.

BuildSim's ids: equipment fire_detection-<level>-<room> with sensors
<level>-<room>-temp/-smoke/-co/-co2. (fire_suppression-<level>-<room>, with
the sprinkler and damper actuators, belongs to actuator_gateway.py.)
"""
import concurrent.futures
import os
import threading
import time
import urllib.error
import urllib.parse

import common
import lifecycle
import messages
import mqtt_bus
import topology

DEADBAND = {"temperature": 0.3, "smoke": 0.3, "co": 3.0, "co2": 25.0}
# Only smoke: it is what the viewer shows clearing, and its noise (0.05 %/m,
# clipped at 0) is far below this band, so noise alone never triggers it.
SETTLE_BAND = {"smoke": 0.15}
SETTLE_SECONDS = float(os.environ.get("BRIDGE_SETTLE_SECONDS", "20"))
SUFFIX = {"temperature": "temp", "smoke": "smoke", "co": "co", "co2": "co2"}
REFRESH_SECONDS = float(os.environ.get("BRIDGE_REFRESH_SECONDS", "300"))
MAX_RPS = float(os.environ.get("MAX_REQUESTS_PER_SECOND", "150"))
WORKERS = int(os.environ.get("BRIDGE_WORKERS", "8"))
MAX_ALERTS = 100   # BuildSim's limit
COVERAGE_ON_SMOKE = float(os.environ.get("COVERAGE_ON_SMOKE", "2.0"))     # %/m
COVERAGE_OFF_SMOKE = float(os.environ.get("COVERAGE_OFF_SMOKE", "1.0"))
COVERAGE_FULL_SMOKE = 30.0      # %/m at which a disc is fully grown and darkest
COVERAGE_SECONDS = float(os.environ.get("COVERAGE_SECONDS", "3"))
COVERAGE_MAX_RADIUS_M = 8.0
LIGHT, DARK = (0x9c, 0xa3, 0xaf), (0x11, 0x18, 0x27)   # grey -> near-black


def coverage_zone(zone, smoke):
    """One BuildSim coverage disc for a smoky zone (valid per BuildSim's
    rules: positive radius, opacity within 0..1)."""
    k = max(0.0, min(1.0, (smoke - COVERAGE_OFF_SMOKE) / (COVERAGE_FULL_SMOKE - COVERAGE_OFF_SMOKE)))
    room_radius_m = min(COVERAGE_MAX_RADIUS_M, (max(zone.area, 1.0) / 3.14159) ** 0.5)
    radius = round(max(0.5, room_radius_m * (0.4 + 0.6 * k)) * topology.PLAN_UNITS_PER_M, 1)
    colour = "#" + "".join(f"{round(a + (b - a) * k):02x}" for a, b in zip(LIGHT, DARK))
    return {"id": f"smoke-{zone.level}-{zone.name}", "name": f"Smoke {zone.name} ({smoke:.0f} %/m)",
            "level": zone.level, "room": zone.name, "radius": radius, "color": colour,
            "opacity": round(0.15 + 0.5 * k, 2)}


def sensor_id(level, room, signal):
    return f"{level}-{room}-{SUFFIX[signal]}"


def equipment(zone):
    level, room = zone.level, zone.name
    return {
        "id": f"fire_detection-{level}-{room}",
        "name": f"Fire detector {room}",
        "type": "smoke_detector",
        "category": "fire_detection",
        "level": level,
        "room": room,
        "status": "running",
        "sensors": [
            {"id": sensor_id(level, room, "temperature"), "name": "Heat detector",
             "type": "temperature", "data_type": "text", "unit": "°C",
             "value": f"{common.AMBIENT_TEMP_C:.1f}"},
            {"id": sensor_id(level, room, "smoke"), "name": "Smoke detector (photoelectric)",
             "type": "smoke_obscuration", "data_type": "text", "unit": "%/m", "value": "0.00"},
            {"id": sensor_id(level, room, "co"), "name": "CO (electrochemical)",
             "type": "co", "data_type": "text", "unit": "ppm", "value": "0"},
            {"id": sensor_id(level, room, "co2"), "name": "CO2 (NDIR)",
             "type": "co2", "data_type": "text", "unit": "ppm",
             "value": f"{common.AMBIENT_CO2_PPM:.0f}"},
        ],
        "actuators": [],
    }


class Bridge:
    def __init__(self, topo):
        self.topo = topo
        self.zones = [topo.zones[k] for k in topo.sensor_zones()]
        self.lock = threading.Lock()
        self.pending = {}      # (kind, id) -> value      (latest wins)
        self.mirrored = {}     # sensor id -> (value, time)
        self.registered = False
        self.alerts = None
        self.smoke = {}         # zone key -> latest measured smoke (%/m)
        self.smoky = set()      # zones currently drawn as coverage
        self.coverage = None    # last coverage list sent (None = resend)
        self.stats = {"sent": 0, "skipped": 0, "errors": 0}
        self.validator = messages.SensorValidator()
        self.pool = concurrent.futures.ThreadPoolExecutor(WORKERS)

    # -- registration -----------------------------------------------------

    def register(self):
        items = [equipment(z) for z in self.zones]
        created = 0
        for i in range(0, len(items), 150):
            result = common.call("POST", "/api/equipment/bulk", items[i:i + 150]) or {}
            created += int(result.get("created", 0))
        with self.lock:
            self.mirrored.clear()   # BuildSim may have lost every value
            self.coverage = None    # ... and the coverage
            self.registered = True
        print(f"registered fire-control equipment for {len(items)} zones "
              f"({created} new)", flush=True)

    def check_buildsim(self):
        """Re-register after a BuildSim restart (its state is in memory)."""
        probe = self.zones[0]
        path = "/api/sensors/" + urllib.parse.quote(
            sensor_id(probe.level, probe.name, "temperature"), safe="")
        try:
            common.call("GET", path)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                print("BuildSim lost the equipment (restart?): re-registering", flush=True)
                self.registered = False
        except Exception:  # noqa: BLE001
            pass

    # -- MQTT handlers --------------------------------------------------------

    def on_sensors(self, topic, payload):
        level, room, _ = mqtt_bus.parse_room_topic(topic)
        if not level:
            return
        payload = self.validator.check(f"{level}/{room}", payload)
        if payload is None:
            return
        now = time.monotonic()
        with self.lock:
            if "smoke" in payload:
                self.smoke[f"{level}/{room}"] = float(payload["smoke"])
            for signal, band in DEADBAND.items():
                if signal not in payload:
                    continue
                value = float(payload[signal])
                sid = sensor_id(level, room, signal)
                last = self.mirrored.get(sid)
                if last and abs(value - last[0]) < band and now - last[1] < REFRESH_SECONDS:
                    settled = (signal in SETTLE_BAND
                               and abs(value - last[0]) >= SETTLE_BAND[signal]
                               and now - last[1] >= SETTLE_SECONDS)
                    if not settled:
                        self.stats["skipped"] += 1
                        continue
                self.pending[("sensor", sid)] = value

    def on_alerts(self, topic, payload):
        if isinstance(payload, list):
            with self.lock:
                self.pending[("alerts", "all")] = payload[:MAX_ALERTS]

    def update_coverage(self):
        """Queue the coverage list when it changed (hysteresis per zone;
        values quantised so noise does not cause a PUT every refresh)."""
        with self.lock:
            smoke = dict(self.smoke)
        for key, value in smoke.items():
            if value >= COVERAGE_ON_SMOKE:
                self.smoky.add(key)
            elif value < COVERAGE_OFF_SMOKE:
                self.smoky.discard(key)
        zones = []
        for key in sorted(self.smoky):
            zone = self.topo.zones.get(key)
            if zone is not None:
                zones.append(coverage_zone(zone, round(smoke.get(key, 0.0))))
        if zones != self.coverage:
            with self.lock:
                self.pending[("coverage", "all")] = zones
            self.coverage = zones

    # -- sending ----------------------------------------------------------------

    def _send(self, kind, item_id, value):
        try:
            if kind == "sensor":
                fmt = "{:.1f}" if item_id.endswith("-temp") else (
                    "{:.2f}" if item_id.endswith("-smoke") else "{:.0f}")
                common.call("PUT", f"/api/sensors/{urllib.parse.quote(item_id, safe='')}/value",
                            {"data_type": "text", "value": fmt.format(value)})
                with self.lock:
                    self.mirrored[item_id] = (value, time.monotonic())
            elif kind == "alerts":
                common.call("PUT", "/api/alerts", value)
            elif kind == "coverage":
                common.call("PUT", "/api/coverage", value)
            self.stats["sent"] += 1
        except urllib.error.HTTPError as exc:
            self.stats["errors"] += 1
            if kind == "coverage":
                self.coverage = None    # send it again with the next refresh
            if exc.code == 404 and kind == "sensor":
                self.registered = False
            elif self.stats["errors"] % 50 == 1:
                print(f"BuildSim rejected {kind} {item_id}: {exc}", flush=True)
        except Exception as exc:  # noqa: BLE001
            self.stats["errors"] += 1
            if self.stats["errors"] % 50 == 1:
                print(f"BuildSim request failed: {exc}", flush=True)

    def flush_once(self, budget):
        with self.lock:
            # Alerts first: they matter most on screen.
            keys = sorted(self.pending, key=lambda k: {"alerts": 0, "coverage": 1}.get(k[0], 2))
            batch = [(k, self.pending.pop(k)) for k in keys[:budget]]
        futures = [self.pool.submit(self._send, k[0], k[1], v) for k, v in batch]
        concurrent.futures.wait(futures)
        return len(batch)


def main():
    common.wait_until_healthy()
    topo = topology.Topology.load(common.BASE)
    bridge = Bridge(topo)
    bridge.register()

    life = lifecycle.Lifecycle("buildsim-bridge",
                               status_topic=mqtt_bus.service_status_topic("buildsim-bridge"))
    bus = life.bus
    bus.subscribe(mqtt_bus.SENSORS_SUB, bridge.on_sensors, qos=0)
    bus.subscribe(mqtt_bus.ALERTS_TOPIC, bridge.on_alerts)
    life.start()
    life.running(f"{len(bridge.zones)} zones")

    period = 0.5
    last_check = last_log = last_coverage = time.monotonic()
    errors_seen = 0
    while not life.stopping:
        started = time.monotonic()
        life.beat()
        try:
            if not bridge.registered:
                common.wait_until_healthy()
                bridge.register()
            if started - last_coverage >= COVERAGE_SECONDS:
                bridge.update_coverage()
                last_coverage = started
            bridge.flush_once(max(1, int(MAX_RPS * period)))
            new_errors = bridge.stats["errors"] - errors_seen
            errors_seen = bridge.stats["errors"]
            if new_errors > 20:
                life.degraded(f"{new_errors} BuildSim requests failed in the last tick")
            elif new_errors == 0:
                life.ok()
            if started - last_check > 30:
                bridge.check_buildsim()
                last_check = started
            if started - last_log > 30:
                s = bridge.stats
                print(f"mirrored {s['sent']} updates, skipped {s['skipped']} within deadband, "
                      f"{s['errors']} errors, {len(bridge.pending)} queued", flush=True)
                last_log = started
        except Exception as exc:  # noqa: BLE001
            print(f"bridge tick failed: {exc}", flush=True)
        life.sleep(max(0.0, period - (time.monotonic() - started)))
    life.stop()


if __name__ == "__main__":
    main()
